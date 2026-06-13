"""Shared types + base class for traffic-speed adapters.

A :class:`TrafficSource` knows how to fetch the current vehicle speed for one
corridor segment from an external provider. When the provider key is not
configured, the adapter runs in a deterministic *synthetic* mode that mimics the
corridor's commute physics (so the PoC's SourceManager + scheduler work end to
end without paid API keys, and the live path is a one-line key swap).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..graph import SegmentMeta

_IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class SpeedReading:
    """A single segment speed observation from a provider (or cache)."""

    segment_id: str
    speed_kmh: float
    jam_factor: float
    source: str
    ts: str                      # ISO-8601 UTC
    stale: bool = False

    def as_dict(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "speed_kmh": round(self.speed_kmh, 2),
            "jam_factor": round(self.jam_factor, 3),
            "source": self.source,
            "ts": self.ts,
            "stale": self.stale,
        }


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def synthetic_speed(seg: SegmentMeta, free_flow_kmh: float = 55.0) -> SpeedReading:
    """Deterministic, time-of-day-aware fallback speed for a segment.

    Mirrors synthetic.py's commute shape (peaks ~09:00 inbound / ~18:45
    outbound) so adapters without an API key still return plausible, *varying*
    speeds — enough to exercise the cascade, cache, and the prediction loop.
    """
    now = datetime.now(tz=timezone.utc).astimezone(_IST)
    mins = now.hour * 60 + now.minute
    pos = 0.0  # 0 = port end .. 1 = junction end (use seg position proxy)
    # SegmentMeta has no explicit position; derive from id ordinal where possible.
    try:
        pos = int(seg.id.split("-")[-1])
    except Exception:  # noqa: BLE001
        pos = 0

    inbound = math.exp(-0.5 * ((mins - 9 * 60) / 75.0) ** 2)
    outbound = math.exp(-0.5 * ((mins - (18 * 60 + 45)) / 80.0) ** 2)
    load = 0.3 + 0.9 * inbound + 0.9 * outbound
    if seg.signalised:
        load += 0.4
    if seg.lane_count <= 2:
        load += 0.3
    jam = 10.0 / (1.0 + math.exp(-(load - 1.6) * 1.6))
    jam = max(0.0, min(10.0, jam))
    speed = free_flow_kmh * (1.0 - 0.85 * (jam / 10.0))
    return SpeedReading(
        segment_id=seg.id,
        speed_kmh=round(speed, 2),
        jam_factor=round(jam, 3),
        source="synthetic",
        ts=_now_iso(),
        stale=False,
    )


class TrafficSource:
    """Base adapter. Subclasses set ``name`` and implement ``_fetch``."""

    name: str = "base"

    def __init__(self, api_key: str = "", free_flow_kmh: float = 55.0) -> None:
        self.api_key = api_key or ""
        self.free_flow_kmh = free_flow_kmh

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def get_segment_speed(self, seg: SegmentMeta) -> Optional[SpeedReading]:
        """Return the current speed for ``seg`` or None on failure.

        With no API key configured, returns a deterministic synthetic reading
        tagged with this source's name (keeps the cascade + scheduler working
        offline). With a key, delegates to ``_fetch`` (the live HTTP call).
        """
        if not self.configured:
            r = synthetic_speed(seg, self.free_flow_kmh)
            r.source = self.name
            return r
        return await self._fetch(seg)

    async def _fetch(self, seg: SegmentMeta) -> Optional[SpeedReading]:  # pragma: no cover
        raise NotImplementedError


__all__ = ["SpeedReading", "TrafficSource", "synthetic_speed"]
