"""Deterministic parking-facility inventory + occupancy model (Appendix C #1).

This is the parking-availability half of Appendix C requirement #1: a static
inventory of parking facilities *inside* the geo-fenced JNPA port area, with a
live availability count (capacity / occupied / available) per facility that the
dashboard's "parking-availability board" renders.

Everything here is a *pure function* of an inventory constant and a single
``minute_of_day`` parameter (0..1439). Occupancy is modelled as a smooth diurnal
curve seeded from a hash of the facility id, **not** wall-clock RNG, so a given
minute always yields the same board — reproducible and demo-stable.

Facilities are placed at realistic lat/lon *inside* the geo-fenced port area,
clustered near the JNPA gates (~[18.86, 73.0] down to the gate aprons at
~[18.95, 72.95]). The gate apron coordinates and the no-park-zone geometry come
from ``jnpa_shared.corridor`` so the inventory stays consistent with the rest of
the UC-III system (facilities sit *near* the gates but never inside a no-park
zone — see ``_assert_outside_no_park_zones`` at import time).
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Dict, List, Tuple

from jnpa_shared.corridor import NO_PARK_ZONES, point_in_polygon

# Minutes in a day. ``minute_of_day`` is always taken modulo this.
MINUTES_PER_DAY = 1440

# Status thresholds, expressed as the *fraction of capacity still free*:
#   AVAILABLE -> more than 20% free
#   FILLING   -> 5%..20% free
#   FULL      -> less than 5% free
AVAILABLE_FREE_FRACTION = 0.20
FILLING_FREE_FRACTION = 0.05

STATUS_AVAILABLE = "AVAILABLE"
STATUS_FILLING = "FILLING"
STATUS_FULL = "FULL"


@dataclass(frozen=True)
class Facility:
    """A single parking facility inside the geo-fenced JNPA port area."""

    id: str
    name: str
    gate_id: str
    lat: float
    lon: float
    capacity: int
    vehicle_types: Tuple[str, ...]


# ---------------------------------------------------------------------------
# Static facility inventory.
#
# Six facilities inside the geo-fenced port: a lot near each of the four gates,
# one consolidated truck-holding yard, and one Common Parking Plaza (CPP). Every
# coordinate sits inside the port area near the gates (NSICT/JNPCT aprons at
# ~[18.95, 72.95]; the holding yard / CPP further down toward [18.86, 73.0]).
# Coordinates are deliberately offset from the no-park-zone gate-apron centroids
# in jnpa_shared.corridor so a facility is *adjacent to* — never *inside* — a
# no-parking polygon.
# ---------------------------------------------------------------------------
FACILITIES: List[Facility] = [
    Facility(
        id="PK-NSICT",
        name="NSICT Gate-1 truck lot",
        gate_id="GATE-NSICT",
        lat=18.9520,
        lon=72.9511,
        capacity=120,
        vehicle_types=("HGV", "trailer"),
    ),
    Facility(
        id="PK-JNPCT",
        name="JNPCT gate lot",
        gate_id="GATE-JNPCT",
        lat=18.9490,
        lon=72.9479,
        capacity=90,
        vehicle_types=("HGV", "trailer"),
    ),
    Facility(
        id="PK-BMCT",
        name="BMCT gate lot",
        gate_id="GATE-BMCT",
        lat=18.9381,
        lon=72.9388,
        capacity=110,
        vehicle_types=("HGV", "trailer"),
    ),
    Facility(
        id="PK-NSIGT",
        name="NSIGT gate lot",
        gate_id="GATE-NSIGT",
        lat=18.9544,
        lon=72.9529,
        capacity=100,
        vehicle_types=("HGV", "trailer"),
    ),
    Facility(
        id="PK-HOLDING",
        name="Truck holding yard",
        gate_id="GATE-NSICT",
        lat=18.8950,
        lon=72.9905,
        capacity=300,
        vehicle_types=("HGV", "trailer", "tanker"),
    ),
    Facility(
        id="PK-CPP",
        name="Common Parking Plaza (CPP)",
        gate_id="CPP",
        lat=18.8640,
        lon=73.0090,
        capacity=450,
        vehicle_types=("HGV", "trailer", "tanker", "reefer"),
    ),
]

# Fast lookup id -> Facility, built once.
_BY_ID: Dict[str, Facility] = {f.id: f for f in FACILITIES}


def _assert_outside_no_park_zones() -> None:
    """Sanity-check at import: no facility sits inside a no-park polygon.

    Facilities are placed *near* the gates but must never coincide with a
    no-parking zone from ``jnpa_shared.corridor``. This keeps the inventory
    geometrically consistent with the anomaly-detection rules.
    """
    for f in FACILITIES:
        for zone in NO_PARK_ZONES:
            if point_in_polygon(f.lat, f.lon, zone.polygon):  # pragma: no cover
                raise ValueError(
                    f"facility {f.id} at ({f.lat}, {f.lon}) is inside no-park "
                    f"zone {zone.id}"
                )


_assert_outside_no_park_zones()


def _hash_unit(key: str) -> float:
    """Map a string to a stable float in [0, 1) via SHA-256 (no wall-clock RNG)."""
    h = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")
    return (h % 1_000_000) / 1_000_000.0


def occupancy(facility_id: str, minute_of_day: int) -> int:
    """Deterministic occupied-count for a facility at ``minute_of_day``.

    Models a smooth diurnal curve: low overnight, two daytime peaks (a morning
    gate-in surge and an afternoon one) bounded by capacity. The base level and
    the phase of the curve are seeded from a hash of ``facility_id`` so every
    facility has its own — but fixed — shape. Returns ``0 <= occupied <= capacity``.

    Pure function of (facility_id, minute_of_day): no RNG, no wall clock.
    """
    facility = _BY_ID.get(facility_id)
    if facility is None:
        raise KeyError(facility_id)

    cap = facility.capacity
    if cap <= 0:
        return 0

    minute = minute_of_day % MINUTES_PER_DAY
    # Fraction of the day elapsed, in [0, 1).
    day_frac = minute / MINUTES_PER_DAY

    # Per-facility deterministic seeds derived from the id hash.
    base_seed = _hash_unit(facility_id + ":base")
    phase_seed = _hash_unit(facility_id + ":phase")
    amp_seed = _hash_unit(facility_id + ":amp")

    # Baseline overnight occupancy: 8%..22% of capacity (yards never empty).
    base_fraction = 0.08 + 0.14 * base_seed

    # A primary diurnal swing (one full cycle/day) plus a smaller second
    # harmonic (two peaks/day: morning + afternoon gate surges). Phase-shifted
    # per facility so the board isn't a single synchronized wave.
    phase = 2.0 * math.pi * phase_seed
    primary = math.sin(2.0 * math.pi * day_frac - math.pi / 2.0 + phase)
    secondary = math.sin(4.0 * math.pi * day_frac + phase)

    # Amplitude of the daytime swing above the baseline: 0.85..1.05 of capacity
    # so peak gate-in surges actually drive facilities into FILLING / FULL while
    # the [0, 1] clamp below keeps occupancy bounded by capacity.
    amplitude = 0.85 + 0.20 * amp_seed

    # Combine into a [0, 1] occupancy fraction. (primary in [-1,1] -> [0,1]).
    swing = 0.7 * ((primary + 1.0) / 2.0) + 0.3 * ((secondary + 1.0) / 2.0)
    fraction = base_fraction + amplitude * swing

    # Clamp to [0, 1] then scale to capacity and round.
    fraction = max(0.0, min(1.0, fraction))
    occupied = int(round(fraction * cap))
    return max(0, min(cap, occupied))


def _status(available: int, capacity: int) -> str:
    """Classify a facility by the fraction of capacity still free."""
    if capacity <= 0:
        return STATUS_FULL
    free_fraction = available / capacity
    if free_fraction > AVAILABLE_FREE_FRACTION:
        return STATUS_AVAILABLE
    if free_fraction >= FILLING_FREE_FRACTION:
        return STATUS_FILLING
    return STATUS_FULL


def _facility_view(facility: Facility, minute_of_day: int) -> dict:
    """Build the per-facility availability row for ``minute_of_day``."""
    capacity = facility.capacity
    occupied = occupancy(facility.id, minute_of_day)
    available = capacity - occupied
    utilisation_pct = round(100.0 * occupied / capacity, 1) if capacity else 0.0
    return {
        "facility_id": facility.id,
        "name": facility.name,
        "gate_id": facility.gate_id,
        "lat": facility.lat,
        "lon": facility.lon,
        "capacity": capacity,
        "occupied": occupied,
        "available": available,
        "utilisation_pct": utilisation_pct,
        "status": _status(available, capacity),
    }


def snapshot(minute_of_day: int) -> List[dict]:
    """Live availability board for every facility at ``minute_of_day``.

    Returns a list of per-facility dicts with capacity / occupied / available,
    utilisation_pct and a status in {AVAILABLE, FILLING, FULL}. Deterministic
    for a fixed ``minute_of_day``.
    """
    return [_facility_view(f, minute_of_day) for f in FACILITIES]


def summary(minute_of_day: int) -> dict:
    """Roll-up totals for the board header at ``minute_of_day``."""
    rows = snapshot(minute_of_day)
    total_capacity = sum(r["capacity"] for r in rows)
    total_occupied = sum(r["occupied"] for r in rows)
    total_available = sum(r["available"] for r in rows)
    full_count = sum(1 for r in rows if r["status"] == STATUS_FULL)
    return {
        "total_capacity": total_capacity,
        "total_occupied": total_occupied,
        "total_available": total_available,
        "facilities": len(rows),
        "full_count": full_count,
    }


def inventory() -> List[dict]:
    """Static facility inventory (capacity + geo), independent of occupancy."""
    return [
        {
            "facility_id": f.id,
            "name": f.name,
            "gate_id": f.gate_id,
            "lat": f.lat,
            "lon": f.lon,
            "capacity": f.capacity,
            "vehicle_types": list(f.vehicle_types),
        }
        for f in FACILITIES
    ]


__all__ = [
    "Facility",
    "FACILITIES",
    "MINUTES_PER_DAY",
    "STATUS_AVAILABLE",
    "STATUS_FILLING",
    "STATUS_FULL",
    "occupancy",
    "snapshot",
    "summary",
    "inventory",
]
