"""HERE Traffic Flow v7 adapter — segment speed from the flow API.

Queries the HERE Traffic Flow v7 ``flow`` endpoint for a small bounding circle
around the segment midpoint and reads the current speed (SU) and jam factor
(JF, already 0..10 on HERE's scale). With no ``HERE_API_KEY`` the base class
returns a synthetic reading so the cascade works offline.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx

from ..graph import SegmentMeta
from .base import SpeedReading, TrafficSource

_FLOW_URL = "https://data.traffic.hereapi.com/v7/flow"


class HereSource(TrafficSource):
    name = "here"

    async def _fetch(self, seg: SegmentMeta) -> Optional[SpeedReading]:
        params = {
            "locationReferencing": "shape",
            "in": f"circle:{seg.lat},{seg.lon};r=400",
            "apiKey": self.api_key,
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(_FLOW_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        try:
            flow = data["results"][0]["currentFlow"]
            speed = float(flow.get("speed", flow.get("speedUncapped")))  # m/s
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
