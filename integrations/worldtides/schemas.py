"""Pydantic views over the raw WorldTides v3 response + normalisation.

WorldTides (https://www.worldtides.info/apidocs) answers height samples and
tide extremes for a coordinate::

    {"status": 200, "callCount": 2, "requestLat": 18.95, "requestLon": 72.94,
     "responseLat": 18.9167, "responseLon": 72.75, "atlas": "TPXO",
     "station": "Mumbai (Bombay)",
     "heights":  [{"dt": 1785400200, "date": "2026-07-30T14:30+0000", "height": 1.24}, ...],
     "extremes": [{"dt": 1785412345, "date": "...", "height": 2.31, "type": "High"}, ...]}

Validation is deliberately tolerant (``extra="ignore"``, every field Optional)
in the same spirit as :mod:`integrations.openweather.schemas`: a partial
answer must degrade a field to ``null``, never fail the whole request. What IS
enforced: the body is an object and, when the API-level ``status`` reports an
error, the client raises before anything downstream sees it.

``normalize()`` flattens the answer into the compact ``tide`` block the
backend consumes — the raw WorldTides shape is NEVER exposed past this module
(it survives verbatim only in ``core.weather_reading.payload``). All heights
are relative to the requested datum (the client pins ``datum=MSL`` so the
block is directly comparable with Open-Meteo's ``sea_level_height_msl``).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict

# A "current" height must come from a sample within this window of now —
# otherwise the field degrades to null rather than serving a stale sample as
# if it were current (WorldTides samples are 30-min cadence; 45 min covers a
# sample gap plus clock skew without ever bridging to a different tide phase).
MAX_SAMPLE_AGE_S = 45 * 60

TIDE_RISING = "RISING"
TIDE_FALLING = "FALLING"


class WorldTidesHeight(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dt: Optional[int] = None          # unix seconds, UTC
    height: Optional[float] = None    # metres relative to the requested datum


class WorldTidesExtreme(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dt: Optional[int] = None
    height: Optional[float] = None
    type: Optional[str] = None        # "High" | "Low"


class WorldTidesResponse(BaseModel):
    """Tolerant view over one v3 heights+extremes answer."""

    model_config = ConfigDict(extra="ignore")

    status: Optional[int] = None
    error: Optional[str] = None
    station: Optional[str] = None
    responseLat: Optional[float] = None
    responseLon: Optional[float] = None
    atlas: Optional[str] = None
    heights: List[WorldTidesHeight] = []
    extremes: List[WorldTidesExtreme] = []

    @property
    def ok(self) -> bool:
        """API-level success — WorldTides embeds its status in the body."""
        return self.status is None or self.status == 200

    # ------------------------------------------------------------- normalise
    def normalize(self, *, now_epoch: Optional[float] = None) -> Dict[str, Any]:
        """Flatten into the ``tide`` block the weather surface serves.

        Never fabricates: a field whose source data is absent (no sample near
        now, no future extreme of that kind) is ``null``.
        """
        now = now_epoch if now_epoch is not None else datetime.now(tz=timezone.utc).timestamp()

        tide_height, sample_dt = self._height_at(now)
        next_high = self._next_extreme(now, "High")
        next_low = self._next_extreme(now, "Low")

        # The upcoming extreme tells the direction: heading to a High = RISING.
        tide_state: Optional[str] = None
        upcoming = [e for e in (next_high, next_low) if e is not None]
        if upcoming:
            nearest = min(upcoming, key=lambda e: e["_dt"])
            tide_state = TIDE_RISING if nearest is next_high else TIDE_FALLING

        return {
            "tide_height": tide_height,
            "next_high_tide": _public(next_high),
            "next_low_tide": _public(next_low),
            "tide_state": tide_state,
            "station": self.station,
            "datum": "MSL",
            "observed_at": _iso(sample_dt),
            "synthetic": False,
        }

    def _height_at(self, now: float) -> tuple[Optional[float], Optional[int]]:
        """The sample nearest to now (within MAX_SAMPLE_AGE_S), else nulls."""
        best: Optional[WorldTidesHeight] = None
        best_gap = float("inf")
        for h in self.heights:
            if h.dt is None or h.height is None:
                continue
            gap = abs(h.dt - now)
            if gap < best_gap:
                best, best_gap = h, gap
        if best is None or best_gap > MAX_SAMPLE_AGE_S:
            return None, None
        return round(float(best.height), 3), best.dt

    def _next_extreme(self, now: float, kind: str) -> Optional[Dict[str, Any]]:
        """The first future extreme of ``kind`` ("High"/"Low"), else None."""
        future = [e for e in self.extremes
                  if e.dt is not None and e.dt > now
                  and (e.type or "").strip().lower() == kind.lower()]
        if not future:
            return None
        first = min(future, key=lambda e: e.dt)
        return {"time": _iso(first.dt),
                "height": round(float(first.height), 3) if first.height is not None else None,
                "_dt": first.dt}


def _public(extreme: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Strip the internal sort key before the block leaves this module."""
    if extreme is None:
        return None
    return {k: v for k, v in extreme.items() if not k.startswith("_")}


def _iso(epoch: Optional[int]) -> Optional[str]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


__all__ = ["WorldTidesResponse", "WorldTidesHeight", "WorldTidesExtreme",
           "MAX_SAMPLE_AGE_S", "TIDE_RISING", "TIDE_FALLING"]
