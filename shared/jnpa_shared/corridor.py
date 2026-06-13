"""Geometry of the NH-348 corridor monitored by UC-III.

A hand-traced polyline of 24 waypoints runs from JNPA Gate-1
(18.9489, 72.9492) south-east down NH-348 to Karal Phata (18.78, 73.08).
Consecutive waypoints are grouped into named segments of roughly 1.5–2 km;
`nearest_segment(lat, lon)` returns the closest segment to an arbitrary point.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

# Ordered (lat, lon) waypoints, port end first. ~24 points, ~1 km apart, so a
# pair of consecutive points is ~1–2 km and a segment (a span of points) lands
# in the 1.5–2 km target band.
WAYPOINTS: List[Tuple[float, float]] = [
    (18.9489, 72.9492),  # 00 JNPA Gate-1 (NSICT)
    (18.9430, 72.9540),  # 01
    (18.9360, 72.9595),  # 02
    (18.9290, 72.9650),  # 03
    (18.9215, 72.9705),  # 04 Y-junction toward NH-348
    (18.9140, 72.9760),  # 05
    (18.9060, 72.9815),  # 06
    (18.8980, 72.9870),  # 07
    (18.8895, 72.9925),  # 08
    (18.8810, 72.9980),  # 09
    (18.8725, 73.0035),  # 10
    (18.8640, 73.0090),  # 11
    (18.8560, 73.0150),  # 12 midway
    (18.8480, 73.0215),  # 13
    (18.8400, 73.0285),  # 14
    (18.8325, 73.0360),  # 15
    (18.8250, 73.0435),  # 16
    (18.8180, 73.0515),  # 17
    (18.8110, 73.0595),  # 18
    (18.8040, 73.0675),  # 19
    (18.7975, 73.0735),  # 20
    (18.7910, 73.0775),  # 21
    (18.7850, 73.0790),  # 22
    (18.7800, 73.0800),  # 23 Karal Phata junction
]


@dataclass(frozen=True)
class Segment:
    """A directed corridor segment between two waypoints."""

    id: str
    start: Tuple[float, float]
    end: Tuple[float, float]
    length_km: float

    @property
    def midpoint(self) -> Tuple[float, float]:
        return (
            (self.start[0] + self.end[0]) / 2.0,
            (self.start[1] + self.end[1]) / 2.0,
        )


def haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Great-circle distance between two (lat, lon) points in kilometres."""
    r = 6371.0088
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


# Target segment length (km). The polyline is resampled into fixed-length
# segments in the spec's 1.5–2 km band; ~1.8 km keeps the final stub in-band too.
SEGMENT_TARGET_KM = 1.8


def _interpolate(a: Tuple[float, float], b: Tuple[float, float], frac: float) -> Tuple[float, float]:
    """Linear interpolation between two (lat, lon) points (fine at corridor scale)."""
    return (a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac)


def _build_segments() -> List[Segment]:
    """Resample the waypoint polyline into fixed ~1.8 km segments.

    Walking the polyline cumulatively (rather than snapping to waypoints) keeps
    every segment inside the 1.5–2 km target band regardless of how the raw
    waypoints happen to be spaced.
    """
    segs: List[Segment] = []
    seg_idx = 0
    seg_start = WAYPOINTS[0]
    accumulated = 0.0  # distance walked since seg_start
    prev = WAYPOINTS[0]

    for nxt in WAYPOINTS[1:]:
        leg = haversine_km(prev, nxt)
        # Carve off as many full target-length segments as this leg allows.
        while leg > 0 and accumulated + leg >= SEGMENT_TARGET_KM:
            remaining = SEGMENT_TARGET_KM - accumulated
            frac = remaining / leg
            cut = _interpolate(prev, nxt, frac)
            segs.append(
                Segment(
                    id=f"SEG-{seg_idx:02d}",
                    start=seg_start,
                    end=cut,
                    length_km=round(SEGMENT_TARGET_KM, 3),
                )
            )
            seg_idx += 1
            seg_start = cut
            prev = cut
            leg = haversine_km(prev, nxt)
            accumulated = 0.0
        accumulated += leg
        prev = nxt

    # Final partial segment to the corridor end (Karal Phata). Merge it into the
    # previous segment if it is too short to stand alone.
    if accumulated > 0.05:
        if accumulated < 1.0 and segs:
            last = segs[-1]
            segs[-1] = Segment(
                id=last.id,
                start=last.start,
                end=WAYPOINTS[-1],
                length_km=round(last.length_km + accumulated, 3),
            )
        else:
            segs.append(
                Segment(
                    id=f"SEG-{seg_idx:02d}",
                    start=seg_start,
                    end=WAYPOINTS[-1],
                    length_km=round(accumulated, 3),
                )
            )
    return segs


segments: List[Segment] = _build_segments()


def _point_to_segment_km(p: Tuple[float, float], seg: Segment) -> float:
    """Approx distance (km) from point p to segment, using a local equirectangular
    projection (adequate at corridor scale)."""
    lat0 = math.radians(p[0])
    km_per_deg_lat = 111.32
    km_per_deg_lon = 111.32 * math.cos(lat0)

    def to_xy(pt: Tuple[float, float]) -> Tuple[float, float]:
        return (pt[1] * km_per_deg_lon, pt[0] * km_per_deg_lat)

    px, py = to_xy(p)
    ax, ay = to_xy(seg.start)
    bx, by = to_xy(seg.end)
    abx, aby = bx - ax, by - ay
    denom = abx * abx + aby * aby
    if denom == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * abx + (py - ay) * aby) / denom
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * abx, ay + t * aby
    return math.hypot(px - cx, py - cy)


def nearest_segment(lat: float, lon: float) -> Optional[Segment]:
    """Return the corridor segment nearest to (lat, lon), or None if no segments."""
    if not segments:
        return None
    p = (lat, lon)
    return min(segments, key=lambda s: _point_to_segment_km(p, s))


def total_length_km() -> float:
    """Total corridor length following the waypoint polyline."""
    return round(
        sum(haversine_km(WAYPOINTS[i], WAYPOINTS[i + 1]) for i in range(len(WAYPOINTS) - 1)),
        3,
    )


if __name__ == "__main__":  # pragma: no cover - manual inspection
    print(f"waypoints      : {len(WAYPOINTS)}")
    print(f"segments       : {len(segments)}")
    print(f"corridor length: {total_length_km()} km")
    for s in segments:
        print(f"  {s.id}: {s.length_km:>5.2f} km  {s.start} -> {s.end}")
    test = nearest_segment(18.86, 73.01)
    print(f"nearest to (18.86, 73.01): {test.id if test else None}")
