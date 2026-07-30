"""HERE Traffic Flow v7 adapter — segment speed from the flow API.

Queries the HERE Traffic Flow v7 ``flow`` endpoint for a small bounding circle
around the segment midpoint and reads the current speed (SU) and jam factor
(JF, already 0..10 on HERE's scale). With no ``HERE_API_KEY`` the base class
returns a synthetic reading so the cascade works offline.

Unit contract (v7 API reference, getflow): ``currentFlow.speed`` /
``speedUncapped`` are "the average speed (in meters per second)" — hence the
``* 3.6`` conversion to km/h below.

The key rides in the query string, so a raw ``httpx`` exception message would
embed ``apiKey=…`` via the request URL. Every transport/HTTP failure is
therefore caught HERE, logged redacted (same hygiene as
``integrations.ulip.client``), and surfaced as ``None`` per the
:class:`~.base.TrafficSource` contract — the SourceManager cascade behaviour
is unchanged. One pooled ``httpx.AsyncClient`` is reused across calls.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx

from jnpa_shared.logging import get_logger

from ..graph import SegmentMeta
from .base import SpeedReading, TrafficSource

log = get_logger("congestion.sources.here")

_FLOW_URL = "https://data.traffic.hereapi.com/v7/flow"
# Unchanged per-request budget; the SourceManager applies its own tighter
# asyncio.wait_for(source_timeout_s) on top of this.
_TIMEOUT_S = 5.0


class HereSource(TrafficSource):
    name = "here"

    def __init__(self, api_key: str = "", free_flow_kmh: float = 55.0,
                 http_client: Optional[httpx.AsyncClient] = None) -> None:
        super().__init__(api_key, free_flow_kmh)
        self._client = http_client
        self._owns_client = http_client is None

    # ------------------------------------------------------------ pooled client
    def _http(self) -> httpx.AsyncClient:
        """The shared connection-pooled client (lazily created, reused)."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT_S)
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        """Release the pooled client (only if this adapter created it)."""
        if self._client is not None and self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------ secret hygiene
    def _redact(self, text: str) -> str:
        """The API key may never surface in logs or exception text."""
        return text.replace(self.api_key, "***") if self.api_key else text

    # ------------------------------------------------------------------- fetch
    async def _fetch(self, seg: SegmentMeta) -> Optional[SpeedReading]:
        params = {
            "locationReferencing": "shape",
            "in": f"circle:{seg.lat},{seg.lon};r=400",
            "apiKey": self.api_key,
        }
        try:
            resp = await self._http().get(_FLOW_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("here_flow_failed", seg=seg.id, error=self._redact(str(exc)))
            return None
        try:
            flow = data["results"][0]["currentFlow"]
            speed = float(flow.get("speed", flow.get("speedUncapped")))  # m/s (v7)
            jam = float(flow.get("jamFactor", 0.0))
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        speed_kmh = speed * 3.6
        return SpeedReading(
            segment_id=seg.id,
            speed_kmh=round(speed_kmh, 2),
            jam_factor=round(max(0.0, min(10.0, jam)), 3),
            source=self.name,
            ts=datetime.now(tz=timezone.utc).isoformat(),
        )


__all__ = ["HereSource"]
