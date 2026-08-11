"""T-01 corridor congestion heatmap (UC3-020).

UI-100/UI-102: the congestion index is observed flow over capacity, adjusted by
speed; the DATA_MODE banner "flips exactly at now"; a forecast jam probability of
0.7 or more triggers a pre-emptive reroute recommendation and CPP metering.

Three rules this module exists to keep:

1. **The banner flips exactly at now.** A slider position at or before the
   current instant reads OBSERVED; anything after it reads DERIVED and carries a
   confidence band. The boundary is computed from one comparison against a single
   `now`, so "exactly" is literal — there is no window either side of it in which
   a forecast could be presented as an observation.

2. **A forecast is never dressed as a measurement.** Past buckets are built from
   counted vehicles on the corridor; future buckets are a stated extrapolation
   whose confidence widens with the horizon. Both carry `data_mode`, and the
   widening band is the honest admission that a 2-hour forecast is worth less
   than a 15-minute one.

3. **The reroute trigger is explainable.** A segment recommends a pre-emptive
   reroute when its jam probability reaches 0.7 — and the response carries the
   probability, the threshold and the reason, so an operator can see WHY rather
   than being told to reroute.

Geometry comes from ``jnpa_shared.corridor`` (13 NH-348 segments, OSM-traced
under assumption A4). Capacity comes from assumptions.json. Neither is invented
here.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from jnpa_shared import corridor
from jnpa_shared.logging import get_logger

from .repository import CorridorHeatmapRepository

log = get_logger("services.corridor_heatmap.service")

# --- window contract ---------------------------------------------------------
PAST_HOURS = 6        # slider runs from -6 h …
FORECAST_HOURS = 2    # … to +2 h
BUCKET_MINUTES = 15   # jam probability updates each 15-minute forecast cycle

DATA_MODE_OBSERVED = "OBSERVED"
DATA_MODE_DERIVED = "DERIVED"

#: UI-102 congestion bands on the index (flow / capacity, speed-adjusted).
BAND_FREE, BAND_BUSY, BAND_HEAVY = 0.5, 0.75, 0.9

#: A forecast at or above this jam probability triggers the pre-emptive reroute
#: recommendation and CPP metering.
REROUTE_PROBABILITY_THRESHOLD = 0.7

#: The congestion model's own operating threshold, quoted with its outputs so the
#: index and the model that produced it cannot be read apart.
CONGESTION_MODEL_THRESHOLD = 0.885

#: Free-flow reference speed for the corridor, km/h. The speed adjustment is a
#: ratio against this, so a segment moving at half free-flow reads as congested
#: even when its raw flow looks modest.
FREE_FLOW_KPH = 50.0


def segment_capacity_vph() -> float:
    """The corridor's TRUCK capacity per hour, from the assumptions register.

    The denominator must match the numerator. corridor.road.per_lane_capacity_vph
    (1800 x 2 lanes = 3600) is an ALL-VEHICLE saturation flow, while the flow this
    heatmap measures is trucks from the freight simulation. Dividing truck flow by
    all-vehicle capacity scored a congested corridor at ~0.03 and painted every
    segment green — the index was arithmetically fine and completely wrong.

    vehicles.baseline_corridor_trucks_per_h is the corridor's own truck-carrying
    reference, so truck flow is compared against truck capacity.
    """
    from jnpa_shared import assumptions

    trucks_ph = assumptions.get("vehicles", "baseline_corridor_trucks_per_h")
    if trucks_ph:
        return float(trucks_ph)
    road = assumptions.get("corridor", "road") or {}
    lanes = float(road.get("lanes_per_direction") or 2)
    per_lane = float(road.get("per_lane_capacity_vph") or 1800)
    return lanes * per_lane


def congestion_index(flow_vph: float, capacity_vph: float, speed_kph: float) -> float:
    """UI-100: observed flow over capacity, adjusted by speed.

    The speed term matters: a segment can carry modest flow *because* it is
    jammed, and a pure flow/capacity ratio would score that as free-flowing —
    reading a jam as calm is the failure this adjustment prevents.
    """
    if capacity_vph <= 0:
        return 0.0
    ratio = flow_vph / capacity_vph
    speed_factor = FREE_FLOW_KPH / max(speed_kph, 1.0)
    return round(min(ratio * speed_factor, 2.0), 4)


def congestion_band(index: float) -> str:
    if index >= BAND_HEAVY:
        return "SEVERE"
    if index >= BAND_BUSY:
        return "HEAVY"
    if index >= BAND_FREE:
        return "BUSY"
    return "FREE"


def jam_probability(index: float, speed_kph: Optional[float] = None) -> float:
    """Probability the segment jams, from the index AND the speed ratio.

    The index alone is not enough in OVERSATURATED flow, and that is not a corner
    case — it is what a jam is. Once a segment locks up, fewer vehicles get
    through it, so measured flow FALLS and any flow/capacity ratio falls with it.
    A segment crawling at 5 km/h scored 0.67 on the index and would have been
    painted "busy" while it was actually stationary.

    So speed is treated as a first-class jam signal: a corridor at a tenth of
    free-flow speed is jammed regardless of how few vehicles are squeezing
    through. The reported probability is the WORSE of the two readings, because a
    jam that only one of them can see is still a jam.
    """
    from_index = 1.0 / (1.0 + math.exp(-12.0 * (index - CONGESTION_MODEL_THRESHOLD)))
    if speed_kph is None or speed_kph <= 0:
        return round(from_index, 4)
    # Speed ratio: 1.0 at free flow, → 0 as the segment stops.
    from_speed = max(0.0, min(1.0, 1.0 - (speed_kph / FREE_FLOW_KPH)))
    return round(max(from_index, from_speed), 4)


def forecast_confidence(minutes_ahead: float) -> float:
    """Confidence in a forecast, decaying with horizon. 1.0 at now."""
    if minutes_ahead <= 0:
        return 1.0
    horizon = FORECAST_HOURS * 60.0
    return round(max(0.55, 1.0 - 0.45 * (minutes_ahead / horizon)), 3)


def data_mode_at(at: datetime, now: datetime) -> str:
    """OBSERVED at or before now; DERIVED strictly after. The flip is exact."""
    return DATA_MODE_OBSERVED if at <= now else DATA_MODE_DERIVED


class CorridorHeatmapService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[CorridorHeatmapRepository] = None) -> None:
        self._repo = repository or CorridorHeatmapRepository(dsn=dsn)

    async def heatmap(self, *, offset_minutes: int = 0,
                      now: Optional[datetime] = None) -> Dict[str, Any]:
        """The 13 corridor segments at one slider position.

        ``offset_minutes`` is the slider: negative into the past, 0 at now,
        positive into the forecast. It is clamped to the -6 h … +2 h contract
        rather than silently accepted, so a caller cannot request a 12-hour
        forecast and receive one that looks equally confident.
        """
        now = now or datetime.now(timezone.utc)
        lo, hi = -PAST_HOURS * 60, FORECAST_HOURS * 60
        clamped = max(lo, min(hi, int(offset_minutes)))
        at = now + timedelta(minutes=clamped)
        mode = data_mode_at(at, now)
        confidence = forecast_confidence(max(0, clamped))
        capacity = segment_capacity_vph()

        # Observed flow/speed per segment for the bucket the slider sits in. For
        # a future bucket there is nothing to observe, so the most recent
        # observation is carried forward as the basis of the extrapolation —
        # and the result is labelled DERIVED, never OBSERVED.
        basis_at = min(at, now)
        observed = await self._repo.segment_flow(basis_at, BUCKET_MINUTES)
        by_segment = {o["segment_code"]: o for o in observed}

        segments: List[Dict[str, Any]] = []
        for seg in corridor.segments:
            obs = by_segment.get(seg.id)
            flow = float(obs["flow_vph"]) if obs else None
            speed = float(obs["speed_kph"]) if obs and obs.get("speed_kph") else FREE_FLOW_KPH
            mid = seg.midpoint

            if flow is None:
                # No observation for this segment in this bucket. Reporting a
                # zero index would read as "free-flowing", which is a claim; the
                # honest answer is that nothing was measured.
                segments.append({
                    "segment_code": seg.id,
                    "lat": mid[0], "lon": mid[1],
                    "start": [seg.start[1], seg.start[0]],
                    "end": [seg.end[1], seg.end[0]],
                    "length_km": seg.length_km,
                    "flow_vph": None, "speed_kph": None,
                    "congestion_index": None, "band": None,
                    "jam_probability": None, "reroute_recommended": False,
                    "observation": "NO_DATA",
                    "data_mode": mode,
                })
                continue

            # A forecast bucket scales the carried-forward flow by how far ahead
            # it sits; the growth is stated in `method`, not hidden in a curve.
            projected_flow = flow
            if mode == DATA_MODE_DERIVED and clamped > 0:
                projected_flow = flow * (1.0 + 0.15 * (clamped / 60.0))

            idx = congestion_index(projected_flow, capacity, speed)
            prob = jam_probability(idx, speed)
            segments.append({
                "segment_code": seg.id,
                "lat": mid[0], "lon": mid[1],
                "start": [seg.start[1], seg.start[0]],
                "end": [seg.end[1], seg.end[0]],
                "length_km": seg.length_km,
                "flow_vph": round(projected_flow, 1),
                "speed_kph": round(speed, 1),
                "congestion_index": idx,
                "band": congestion_band(idx),
                "jam_probability": prob,
                # The trigger, and enough context to explain itself.
                "reroute_recommended": prob >= REROUTE_PROBABILITY_THRESHOLD,
                "reroute_reason": (
                    f"jam probability {prob:.2f} >= {REROUTE_PROBABILITY_THRESHOLD} "
                    f"threshold — pre-emptive reroute and CPP metering"
                    if prob >= REROUTE_PROBABILITY_THRESHOLD else None),
                "observation": "COUNTED" if mode == DATA_MODE_OBSERVED else "EXTRAPOLATED",
                "data_mode": mode,
            })

        triggered = [s for s in segments if s["reroute_recommended"]]
        measured = [s for s in segments if s["congestion_index"] is not None]

        return {
            "at": at.isoformat(),
            "now": now.isoformat(),
            "offset_minutes": clamped,
            "offset_requested": int(offset_minutes),
            "clamped": clamped != int(offset_minutes),
            "data_mode": mode,
            "confidence": confidence if mode == DATA_MODE_DERIVED else 1.0,
            "segments": segments,
            "segment_count": len(segments),
            "measured_count": len(measured),
            "window": {
                "past_hours": PAST_HOURS,
                "forecast_hours": FORECAST_HOURS,
                "bucket_minutes": BUCKET_MINUTES,
                "min_offset_minutes": lo,
                "max_offset_minutes": hi,
            },
            "bands": {"free": BAND_FREE, "busy": BAND_BUSY, "heavy": BAND_HEAVY},
            "reroute": {
                "threshold": REROUTE_PROBABILITY_THRESHOLD,
                "triggered": bool(triggered),
                "segments": [s["segment_code"] for s in triggered],
                "action": ("PRE_EMPTIVE_REROUTE_AND_CPP_METERING" if triggered else None),
            },
            "method": {
                "congestion_index": ("observed flow / capacity, adjusted by "
                                     "free-flow speed / observed speed (UI-100)"),
                "capacity_vph": capacity,
                "capacity_source": ("assumptions.json vehicles.baseline_corridor_trucks_per_h "
                                    "— truck flow against truck capacity"),
                "free_flow_kph": FREE_FLOW_KPH,
                "jam_probability": (f"worse of: logistic on the index centred at "
                                    f"{CONGESTION_MODEL_THRESHOLD}, and the speed "
                                    f"deficit 1 - speed/{FREE_FLOW_KPH:.0f} — because "
                                    f"flow FALLS in a jam, so the index alone "
                                    f"under-reads oversaturated segments"),
                "forecast": ("flow carried forward from the last observed bucket and "
                             "grown 15% per hour of horizon; confidence decays with "
                             "horizon"),
                "geometry_source": ("jnpa_shared.corridor — 13 NH-348 segments, "
                                    "OSM-traced under assumption A4, not survey-grade"),
            },
            "provenance": {
                "mode": mode,
                "note": ("Segment speeds are simulated (no live GPS corpus) and the "
                         "geometry is self-digitised under assumption A4. Past "
                         "buckets are counted; anything after now is a stated "
                         "extrapolation carrying a confidence band."),
                "resolution_disclaimer": ("Segment geometry is OSM-traced, not "
                                          "survey-grade; positions are indicative to "
                                          "roughly the width of the carriageway."),
            },
        }
