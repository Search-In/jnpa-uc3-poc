"""Synthetic track generation for AE bootstrap training + tests.

Two roles:

  * ``normal_tracks`` — a corpus of *normal* corridor trajectories (vehicles
    cruising down NH-348 with realistic stop/go) used to train the trajectory
    autoencoder when there is not yet enough real track history. The AE learns
    this "normal manifold" so genuinely odd behaviour reconstructs poorly.

  * the named scenario builders (``wrongway_track``, ``abandoned_track``,
    ``illegal_park_track``, ``route_deviation_track``, ``looping_track``) — single
    tracks that should each trip exactly one rule (or the AE), used by the tests
    and demo. They are deterministic given a seed.

All builders return ``Track`` objects, so they exercise the exact same engine
path as the live ByteTrack / telemetry feeds.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from jnpa_shared.corridor import WAYPOINTS, NO_PARK_ZONES, haversine_km

from .types import Track, TrackPoint, bearing_deg

LatLon = Tuple[float, float]
_REF = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)


def _step_along(a: LatLon, b: LatLon, frac: float) -> LatLon:
    return (a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac)


def _corridor_leg(start_idx: int = 4, n_legs: int = 3) -> List[LatLon]:
    """A short down-corridor polyline (toward Karal Phata) of waypoints."""
    end_idx = min(len(WAYPOINTS) - 1, start_idx + n_legs)
    return WAYPOINTS[start_idx : end_idx + 1]


def _densify(anchors: List[LatLon], n: int) -> List[LatLon]:
    """Resample a polyline into ~n evenly-spaced points."""
    if len(anchors) < 2:
        return anchors * n
    seg_lens = [haversine_km(anchors[i], anchors[i + 1]) for i in range(len(anchors) - 1)]
    total = sum(seg_lens) or 1e-6
    out: List[LatLon] = []
    for k in range(n):
        d = total * k / max(1, n - 1)
        acc = 0.0
        for i, sl in enumerate(seg_lens):
            if acc + sl >= d or i == len(seg_lens) - 1:
                frac = (d - acc) / sl if sl > 0 else 0.0
                out.append(_step_along(anchors[i], anchors[i + 1], min(1.0, frac)))
                break
            acc += sl
    return out


def _build_track(
    track_id: str,
    coords: List[LatLon],
    speeds: List[float],
    *,
    start: datetime = _REF,
    dt_s: float = 1.0,
    camera_id: Optional[str] = None,
    device_id: Optional[str] = None,
    plate: Optional[str] = None,
    headings: Optional[List[float]] = None,
) -> Track:
    """Assemble a Track from parallel coord/speed (and optional heading) series."""
    track = Track(track_id=track_id, camera_id=camera_id, device_id=device_id, plate=plate)
    for i, (lat, lon) in enumerate(coords):
        if headings is not None:
            hd = headings[i]
        elif i > 0 and (coords[i - 1] != (lat, lon)):
            hd = bearing_deg(coords[i - 1], (lat, lon))
        else:
            hd = 0.0
        track.add(TrackPoint(
            ts=start + timedelta(seconds=i * dt_s),
            lat=lat, lon=lon,
            speed_kmh=speeds[i] if i < len(speeds) else (speeds[-1] if speeds else 0.0),
            heading=hd,
        ))
    return track


# --------------------------------------------------------------------------- normal
def normal_tracks(n: int, seq_len: int = 64, seed: int = 1337) -> List[Track]:
    """A corpus of normal down-corridor trajectories for AE training."""
    rng = random.Random(seed)
    tracks: List[Track] = []
    for k in range(n):
        start_idx = rng.randint(2, len(WAYPOINTS) - 6)
        anchors = WAYPOINTS[start_idx : start_idx + rng.randint(3, 5)]
        coords = _densify(list(anchors), seq_len)
        # Cruise speed with mild noise + an occasional brief slow-down (stop/go).
        base = rng.uniform(35.0, 55.0)
        speeds = []
        for i in range(seq_len):
            v = base + rng.gauss(0, 3.0)
            if rng.random() < 0.04:
                v *= 0.3
            speeds.append(max(0.0, v))
        headings = [bearing_deg(coords[max(0, i - 1)], coords[i]) if i > 0 else
                    bearing_deg(coords[0], coords[1]) for i in range(seq_len)]
        tracks.append(_build_track(f"NORMAL-{k:04d}", coords, speeds,
                                   camera_id="CAM-COR-03", headings=headings))
    return tracks


# --------------------------------------------------------------------------- scenarios
def wrongway_track(camera_id: str = "CAM-COR-01", seed: int = 1) -> Track:
    """A vehicle driving UP the corridor (against the ~SE allowed bearing)."""
    # Go the wrong way: from a downstream waypoint back toward the port (NW).
    anchors = list(reversed(_corridor_leg(start_idx=6, n_legs=2)))
    coords = _densify(anchors, 12)
    speeds = [30.0] * len(coords)
    # 12 points @ 1s = ~11 s of sustained wrong-way (>> 2 s hold).
    return _build_track("WRONGWAY-1", coords, speeds, camera_id=camera_id,
                        plate="MH04WW0001", dt_s=1.0)


def abandoned_track(seed: int = 1) -> Track:
    """A vehicle that stops OUTSIDE every no-park zone and sits for >120 s."""
    # Pick a corridor point that is not inside any no-park zone.
    spot = _step_along(WAYPOINTS[7], WAYPOINTS[8], 0.5)
    # Approach (moving) then 150 s stationary at 2 s cadence.
    approach = _densify([WAYPOINTS[6], spot], 5)
    coords = approach + [spot] * 80          # 80 * 2 s = 160 s dwell
    speeds = [40.0, 35.0, 25.0, 12.0, 4.0] + [0.0] * 80
    return _build_track("ABANDONED-1", coords, speeds, camera_id="CAM-COR-03",
                        plate="MH04AB0001", dt_s=2.0)


def illegal_park_track(seed: int = 1) -> Track:
    """A vehicle that stops INSIDE a no-park zone and sits for >300 s."""
    zone = NO_PARK_ZONES[2]   # NPZ-YJUNCTION (mid-corridor)
    spot = zone.centroid
    approach = _densify([WAYPOINTS[3], spot], 5)
    # 180 points * 2 s = 360 s dwell (>300 s; > 5 min so WARNING escalation).
    coords = approach + [spot] * 180
    speeds = [40.0, 30.0, 18.0, 8.0, 2.0] + [0.0] * 180
    return _build_track("ILLEGALPARK-1", coords, speeds, camera_id="CAM-COR-01",
                        plate="MH04IP0001", dt_s=2.0)


def route_deviation_track(seed: int = 1) -> Tuple[Track, List[LatLon]]:
    """A truck whose GPS path leaves its assigned route by >800 m for >90 s.

    Returns ``(track, assigned_route)``.
    """
    assigned = _densify(list(_corridor_leg(start_idx=4, n_legs=4)), 20)
    # Actual path: start on-route, then veer ~1.5 km east (off-route) and hold.
    on = assigned[:4]
    veer_start = assigned[3]
    # Offset ~1.5 km east in lon.
    east_off = 1.5 / (111.32 * math.cos(math.radians(veer_start[0])))
    off_spot = (veer_start[0], veer_start[1] + east_off)
    veer = _densify([veer_start, off_spot], 6)
    hold = [off_spot] * 100                    # 100 * 1 s = 100 s off-route
    coords = on + veer + hold
    speeds = [45.0] * len(coords)
    track = _build_track("ROUTEDEV-1", coords, speeds, device_id="TRUCK-RD-1",
                         plate="MH04RD0001", dt_s=1.0)
    return track, assigned


def looping_track(seed: int = 1) -> Track:
    """A vehicle slowly looping in a circle — trips the AE, not any single rule."""
    rng = random.Random(seed)
    center = _step_along(WAYPOINTS[9], WAYPOINTS[10], 0.5)
    r_km = 0.15
    coords: List[LatLon] = []
    headings: List[float] = []
    for i in range(64):
        ang = 2 * math.pi * (i / 16.0)         # 4 full loops over 64 steps
        dlat = (r_km / 111.32) * math.sin(ang)
        dlon = (r_km / (111.32 * math.cos(math.radians(center[0])))) * math.cos(ang)
        coords.append((center[0] + dlat, center[1] + dlon))
        headings.append((math.degrees(ang) + 90.0) % 360.0)
    speeds = [8.0 + rng.gauss(0, 1.0) for _ in coords]   # crawling
    return _build_track("LOOPING-1", coords, speeds, camera_id="CAM-COR-04",
                        plate="MH04LP0001", headings=headings, dt_s=2.0)


__all__ = [
    "normal_tracks",
    "wrongway_track",
    "abandoned_track",
    "illegal_park_track",
    "route_deviation_track",
    "looping_track",
]
