"""Google Maps adapter — Distance Matrix / Roads API derived segment speed.

For a corridor segment we ask the Distance Matrix API for the duration between
the segment's start and end with ``departure_time=now`` (live, traffic-aware).
Speed = segment length / duration-in-traffic. A jam_factor is derived from the
ratio of free-flow to live speed. Polled every 60 s per segment by the
SourceManager's caller; this adapter only does a single fetch.

Without ``GOOGLE_MAPS_API_KEY`` the base class returns a synthetic reading so
the cascade still works offline (see sources/base.py).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx

from jnpa_shared import corridor

from ..graph import SegmentMeta
from .base import SpeedReading, TrafficSource

_DM_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"


class GoogleSource(TrafficSource):
    name = "google"

    async def _fetch(self, seg: SegmentMeta) -> Optional[SpeedReading]:
        # We need the segment endpoints; recover them from the shared geometry.
        seg_geom = next((s for s in corridor.segments if s.id == seg.id), None)
        if seg_geom is None:
            return None
        origin = f"{seg_geom.start[0]},{seg_geom.start[1]}"
        dest = f"{seg_geom.end[0]},{seg_geom.end[1]}"
        params = {
            "origins": origin,
            "destinations": dest,
            "departure_time": "now",
            "key": self.api_key,
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(_DM_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        try:
            elem = data["rows"][0]["elements"][0]
            secs = elem.get("duration_in_traffic", elem["duration"])["value"]
        except (KeyError, IndexError, TypeError):
            return None
        if secs <= 0:
            return None
        speed = seg_geom.length_km / (secs / 3600.0)
        ratio = max(0.0, min(1.0, 1.0 - speed / self.free_flow_kmh))
        return SpeedReading(
            segment_id=seg.id,
            speed_kmh=round(speed, 2),
            jam_factor=round(ratio * 10.0, 3),
            source=self.name,
            ts=datetime.now(tz=timezone.utc).isoformat(),
        )


__all__ = ["GoogleSource"]
