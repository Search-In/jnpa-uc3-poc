"""Shared motion analysis helpers used by the rule engine.

Stationarity is the common primitive behind both the abandoned-vehicle rule and
the illegal-parking rule: a track is "stationary" when, over a trailing window,
its observations stay inside a small radius AND its mean speed is below a
threshold. ``stationary_dwell`` returns how long (seconds) the track has been
continuously stationary at its latest point, which the rules compare against
their dwell thresholds (and parking uses for duration-based escalation).
"""
from __future__ import annotations

from typing import List, Optional

from jnpa_shared.corridor import haversine_km

from .types import Track, TrackPoint


def stationary_dwell(
    track: Track,
    *,
    speed_kmh_max: float,
    radius_m: float,
) -> float:
    """Seconds the track has been continuously stationary up to its latest point.

    Walks backward from the most recent observation, growing the window while the
    point stays within ``radius_m`` of the dwell anchor (the latest point) and the
    instantaneous speed is below ``speed_kmh_max``. Returns 0.0 if the latest
    point is moving.
    """
    pts = track.points
    if len(pts) < 2:
        return 0.0
    anchor = pts[-1]
    if anchor.speed_kmh > speed_kmh_max:
        return 0.0
    radius_km = radius_m / 1000.0
    start: Optional[TrackPoint] = None
    for p in reversed(pts):
        if p.speed_kmh > speed_kmh_max:
            break
        if haversine_km((p.lat, p.lon), (anchor.lat, anchor.lon)) > radius_km:
            break
        start = p
    if start is None:
        return 0.0
    return (anchor.ts - start.ts).total_seconds()


def is_stationary(track: Track, *, speed_kmh_max: float, radius_m: float, dwell_s: float) -> bool:
    """True if the track has been stationary for at least ``dwell_s`` seconds."""
    return stationary_dwell(track, speed_kmh_max=speed_kmh_max, radius_m=radius_m) >= dwell_s


def path_length_km(path: List[tuple]) -> float:
    """Total length of a (lat, lon) polyline in kilometres."""
    return sum(haversine_km(path[i], path[i + 1]) for i in range(len(path) - 1))


__all__ = ["stationary_dwell", "is_stationary", "path_length_km"]
