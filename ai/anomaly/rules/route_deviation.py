"""Route-deviation rule for trucking-app devices.

Per the bid spec: compare a truck's trucking-app GPS path to the assigned route
from ``/devices/{id}/route``. If the **cosine distance** between the travelled
direction and the assigned direction exceeds 0.4, OR the truck is **off-route**
by more than 800 m, sustained for more than 90 s -> ROUTE_DEVIATION.

The two triggers capture different failures:

  * **Cosine distance** (1 - cos θ between the mean travel vector and the mean
    assigned-route vector) catches a truck heading the *wrong way* along /
    across the corridor even while still near the polyline.
  * **Off-route distance** (min distance from the latest position to the
    assigned polyline) catches a truck that has physically left the route.

Both must be *sustained*: we require the off-route / mis-heading condition to
hold across the trailing window spanning at least ``route_hold_s`` seconds, so a
brief lane change or GPS jitter does not trip it.

The assigned route is supplied as a ``[(lat, lon), ...]`` polyline. The engine
fetches it from the trucking-app control plane (``route_lookup.fetch_route``);
tests inject it directly so the rule stays pure and infra-free.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from jnpa_shared.corridor import haversine_km
from jnpa_shared.schemas import Alert

from ..config import AnomalyConfig
from ..types import Track

KIND = "ROUTE_DEVIATION"

LatLon = Tuple[float, float]


def _local_xy(p: LatLon, lat0: float) -> Tuple[float, float]:
    """Equirectangular metres from origin lat (adequate at corridor scale)."""
    km_per_deg_lat = 111.32
    km_per_deg_lon = 111.32 * math.cos(math.radians(lat0))
    return (p[1] * km_per_deg_lon * 1000.0, p[0] * km_per_deg_lat * 1000.0)


def _mean_unit_vector(path: Sequence[LatLon]) -> Optional[Tuple[float, float]]:
    """Mean unit direction (dx, dy in metres-space) along a polyline, or None."""
    if len(path) < 2:
        return None
    lat0 = path[0][0]
    sx = sy = 0.0
    for a, b in zip(path, path[1:]):
        ax, ay = _local_xy(a, lat0)
        bx, by = _local_xy(b, lat0)
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        if norm > 1e-6:
            sx += dx / norm
            sy += dy / norm
    n = math.hypot(sx, sy)
    if n < 1e-9:
        return None
    return (sx / n, sy / n)


def cosine_distance(travelled: Sequence[LatLon], assigned: Sequence[LatLon]) -> Optional[float]:
    """1 - cos(angle) between the mean travel and mean assigned-route vectors.

    Returns None if either path is too short to define a direction. Range 0..2
    (0 = identical heading, 1 = perpendicular, 2 = exactly opposite).
    """
    u = _mean_unit_vector(travelled)
    v = _mean_unit_vector(assigned)
    if u is None or v is None:
        return None
    dot = max(-1.0, min(1.0, u[0] * v[0] + u[1] * v[1]))
    return 1.0 - dot


def _point_to_segment_m(p: LatLon, a: LatLon, b: LatLon) -> float:
    """Distance (metres) from point p to segment a-b (equirectangular)."""
    lat0 = p[0]
    px, py = _local_xy(p, lat0)
    ax, ay = _local_xy(a, lat0)
    bx, by = _local_xy(b, lat0)
    abx, aby = bx - ax, by - ay
    denom = abx * abx + aby * aby
    if denom == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * abx + (py - ay) * aby) / denom
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * abx, ay + t * aby
    return math.hypot(px - cx, py - cy)


def offroute_distance_m(point: LatLon, route: Sequence[LatLon]) -> float:
    """Minimum distance (metres) from a point to an assigned-route polyline."""
    if not route:
        return float("inf")
    if len(route) == 1:
        return haversine_km(point, route[0]) * 1000.0
    return min(
        _point_to_segment_m(point, route[i], route[i + 1])
        for i in range(len(route) - 1)
    )


def evaluate(track: Track, route: Sequence[LatLon], cfg: AnomalyConfig) -> Optional[Alert]:
    """Return a ROUTE_DEVIATION Alert if the track deviates from its assigned route.

    ``route`` is the assigned ``[(lat, lon), ...]`` polyline for the truck.
    """
    if not route or len(track.points) < 2:
        return None

    # Trailing window covering >= route_hold_s seconds.
    last_ts = track.points[-1].ts
    window = [p for p in track.points
              if (last_ts - p.ts).total_seconds() <= cfg.route_hold_s]
    if len(window) < 2:
        return None
    span_s = (window[-1].ts - window[0].ts).total_seconds()
    if span_s < cfg.route_hold_s:
        return None

    travelled = [(p.lat, p.lon) for p in window]
    cos_dist = cosine_distance(travelled, route)

    # Off-route condition must hold across the whole window (sustained), not just
    # at the final point — otherwise a single GPS spike would trip it.
    offroute_each = [offroute_distance_m((p.lat, p.lon), route) for p in window]
    offroute_now = offroute_each[-1]
    offroute_sustained = all(d > cfg.route_offroute_m for d in offroute_each)

    cosine_trip = cos_dist is not None and cos_dist > cfg.route_cosine_threshold
    offroute_trip = offroute_sustained

    if not (cosine_trip or offroute_trip):
        return None

    reasons: List[str] = []
    if cosine_trip:
        reasons.append("cosine")
    if offroute_trip:
        reasons.append("offroute")

    last = track.points[-1]
    return Alert(
        kind=KIND,
        severity="warning",
        plate=track.plate,
        payload={
            "track_id": track.track_id,
            "device_id": track.device_id,
            "reasons": reasons,
            "cosine_distance": round(cos_dist, 3) if cos_dist is not None else None,
            "cosine_threshold": cfg.route_cosine_threshold,
            "offroute_m": round(offroute_now, 1),
            "offroute_threshold_m": cfg.route_offroute_m,
            "sustained_s": round(span_s, 1),
            "lat": last.lat,
            "lon": last.lon,
            "ts": last.ts.isoformat(),
        },
    )


__all__ = [
    "KIND",
    "cosine_distance",
    "offroute_distance_m",
    "evaluate",
]
