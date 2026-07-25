"""JNPA gate geography + geo helpers for the trucking-app simulator.

The 4 gate coordinates mirror ``core.gate`` (infra/postgres/init.sql) and the
RFID topology, so a truck heading to ``G-NSICT`` ends up exactly where the gate
cameras/readers sit. ``port_boundary`` is a point a short way up the corridor
from the gates; the segment between it and a gate is treated as "port road"
(25 km/h) by the speed model.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Dict, List, Tuple

from jnpa_shared.corridor import haversine_km

# Gate ids + coordinates (mirror core.gate seed + rfid topology GATE_COORDS).
GATE_COORDS: Dict[str, Tuple[float, float]] = {
    "G-NSICT": (18.9489, 72.9492),
    "G-JNPCT": (18.9512, 72.9505),
    "G-NSIGT": (18.9457, 72.9531),
    "G-BMCT": (18.9420, 72.9560),
}
GATE_IDS: List[str] = list(GATE_COORDS.keys())

# The port "boundary" — roughly the Y-junction onto NH-348 (waypoint 04 of the
# shared corridor polyline). Inside this radius of a gate, roads are port roads.
PORT_BOUNDARY: Tuple[float, float] = (18.9215, 72.9705)
PORT_ROAD_RADIUS_KM: float = 3.0


@dataclass(frozen=True)
class Gate:
    id: str
    lat: float
    lon: float


def gate(gate_id: str) -> Gate:
    lat, lon = GATE_COORDS[gate_id]
    return Gate(id=gate_id, lat=lat, lon=lon)


def gate_for_index(i: int) -> str:
    """Round-robin a device index over the 4 gates (spec: round-robin gates)."""
    return GATE_IDS[i % len(GATE_IDS)]


def is_port_road(lat: float, lon: float) -> bool:
    """True if the point is inside the port-road zone (near any gate)."""
    p = (lat, lon)
    return any(haversine_km(p, c) <= PORT_ROAD_RADIUS_KM for c in GATE_COORDS.values())


def random_origin(rng: random.Random, gate_id: str, radius_km: float) -> Tuple[float, float]:
    """A random origin within ``radius_km`` of the given gate.

    Drawn on land-ish bearings only: JNPA sits on the coast, so we bias the
    bearing into the inland (eastward / north-east / south-east) half so origins
    don't fall in the Arabian Sea. Distance is sqrt-weighted for a roughly
    uniform area distribution within the disc.
    """
    glat, glon = GATE_COORDS[gate_id]
    # Inland half-plane: bearings roughly E (45°..225° clockwise from north),
    # i.e. avoid the western sea. Keep a 10 km minimum so trips aren't trivial.
    bearing_deg = rng.uniform(30.0, 210.0)
    dist_km = max(10.0, radius_km * math.sqrt(rng.random()))
    return _project(glat, glon, bearing_deg, dist_km)


def _project(lat: float, lon: float, bearing_deg: float, dist_km: float) -> Tuple[float, float]:
    """Project a point ``dist_km`` along ``bearing_deg`` from (lat, lon)."""
    r = 6371.0088
    br = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    dr = dist_km / r
    lat2 = math.asin(
        math.sin(lat1) * math.cos(dr) + math.cos(lat1) * math.sin(dr) * math.cos(br)
    )
    lon2 = lon1 + math.atan2(
        math.sin(br) * math.sin(dr) * math.cos(lat1),
        math.cos(dr) - math.sin(lat1) * math.sin(lat2),
    )
    return (round(math.degrees(lat2), 6), round(math.degrees(lon2), 6))


def initial_bearing(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Initial great-circle bearing (degrees, 0..360) from a to b."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0
