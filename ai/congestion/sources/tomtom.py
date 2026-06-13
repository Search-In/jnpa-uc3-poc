"""TomTom Traffic Flow adapter — Flow Segment Data API.

Queries TomTom's ``flowSegmentData`` for the segment midpoint and reads the
current speed and free-flow speed; jam_factor is derived from their ratio. With
no ``TOMTOM_API_KEY`` the base class returns a synthetic reading so the cascade
works offline.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx

from ..graph import SegmentMeta
from .base import SpeedReading, TrafficSource

_FLOW_URL = (
    "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json"
)


class TomTomSource(TrafficSource):
    name = "tomtom"

    async def _fetch(self, seg: SegmentMeta) -> Optional[SpeedReading]:
        params = {"point": f"{seg.lat},{seg.lon}", "key": self.api_key, "unit": "KMPH"}
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(_FLOW_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        try:
            seg_data = data["flowSegmentData"]
            speed = float(seg_data["currentSpeed"])
            free = float(seg_data.get("freeFlowSpeed", self.free_flow_kmh)) or self.free_flow_kmh
        except (KeyError, TypeError, ValueError):
            return None
        ratio = max(0.0, min(1.0, 1.0 - speed / free))
        return SpeedReading(
            segment_id=seg.id,
            speed_kmh=round(speed, 2),
            jam_factor=round(ratio * 10.0, 3),
            source=self.name,
            ts=datetime.now(tz=timezone.utc).isoformat(),
        )


__all__ = ["TomTomSource"]
