"""Deterministic empty-container supply & demand books for the JNPA UC-III PoC.

Appendix C requirement #3 needs an empty-container supply-demand optimiser that
produces a *probable allocation* across fleet owners, the shipping line, CFS
yards and the Empty Container Depot (ECD). To exercise that optimiser without a
live yard-management feed, this module generates two fully deterministic books:

  * a **supply book** — ECD + CFS depots, each holding empty-container stock by
    container type (20GP / 40GP / 40HC / REEFER), positioned by lat/lon near
    JNPA (~[18.86, 73.0]); and
  * a **demand book** — shipping-line bookings and fleet-owner requests, each
    with an origin / destination / priority and a `cargo_type` drawn from
    {container, oil_tanker, break_bulk, cement_bowser}.

Everything is derived from a fixed `SEED` and anchored geometry, so the same
inputs always yield the same books — reproducible run-to-run and host-to-host
for demos and tests. There is **no** `Date.now()` / unseeded RNG anywhere: all
"randomness" is a SHA-256 hash of `SEED` plus the record key (mirroring the
`vahan_sim.seed` approach).

Importable (the FastAPI app builds its in-memory books from `supply_book()` /
`demand_book()`) and runnable as a script to print a summary:

    python -m empty_container.seed
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

# --- Determinism anchors -----------------------------------------------------
SEED = "jnpa-uc3-empty-container-v1"

# JNPA reference point (lat, lon). All depots/demand cluster around here.
JNPA_CENTER: Tuple[float, float] = (18.86, 73.0)

# Container types the supply book is stocked in (ISO codes).
CONTAINER_TYPES: List[str] = ["20GP", "40GP", "40HC", "REEFER"]

# Cargo variants the demand book carries. `container` is the default empty-box
# move; the others are the tanker / break-bulk / cement-bowser variants called
# out in Appendix C #3.
CARGO_TYPES: List[str] = ["container", "oil_tanker", "break_bulk", "cement_bowser"]

# Each cargo variant maps to the container/equipment type it draws from supply.
CARGO_CONTAINER: Dict[str, str] = {
    "container": "40GP",      # generic dry box; refined per-demand below
    "oil_tanker": "20GP",     # ISO tank-frame footprint
    "break_bulk": "40HC",     # high-cube for odd-shaped break-bulk
    "cement_bowser": "20GP",  # bulk cement bowser on a 20ft frame
}

# Booking sources -> owner class for the "probable allocation across fleet
# owners / shipping line / CFS / ECD" requirement.
SOURCES: List[str] = ["shipping_line", "fleet_owner"]

PRIORITIES: List[str] = ["high", "normal", "low"]

# Depot catalogue: (id, name, kind, lat, lon). `kind` is ECD or CFS so the
# optimiser (and the dashboard) can distinguish depot classes. Coordinates are
# hand-placed in the JNPA hinterland near [18.86, 73.0].
DEPOT_CATALOGUE: List[Tuple[str, str, str, float, float]] = [
    ("ECD-JNPA", "JNPA Empty Container Depot", "ECD", 18.9489, 72.9492),
    ("ECD-DRONAGIRI", "Dronagiri Node ECD", "ECD", 18.8810, 72.9980),
    ("CFS-PANVEL", "Panvel CFS", "CFS", 18.8560, 73.0150),
    ("CFS-NHAVA", "Nhava Sheva CFS", "CFS", 18.9060, 72.9815),
    ("CFS-KALAMBOLI", "Kalamboli CFS", "CFS", 18.8400, 73.0285),
    ("CFS-URAN", "Uran CFS", "CFS", 18.8725, 73.0035),
]

# Demand origins (where the empty box is needed) — exporter / CFS clusters in
# JNPA's *near* hinterland. Empty-container repositioning is short-haul by
# nature (ECD/CFS to a nearby exporter, then back to the port), so these sit in
# a tight ring around JNPA rather than the deep hinterland; the optimiser's job
# is to shave the last few km/dwell off an already-short move.
DEMAND_ORIGINS: List[Tuple[str, float, float]] = [
    ("Dronagiri SEZ", 18.8950, 72.9700),
    ("Uran Industrial", 18.8780, 72.9920),
    ("JNPT SEZ", 18.9300, 72.9550),
    ("Panvel Exporter", 18.8650, 73.0250),
    ("Kalamboli Yard", 18.8450, 73.0320),
    ("Nhava Exporter", 18.9100, 72.9780),
    ("Taloja MIDC", 19.0200, 73.0700),
    ("Pushpak Node", 18.9050, 73.0450),
]

# Default fleet count of demand records and stock generation knobs.
DEFAULT_SUPPLY_DEPOTS = len(DEPOT_CATALOGUE)
DEFAULT_DEMAND_COUNT = 40


def _h(*parts: object) -> int:
    """Stable 64-bit-ish int hash from SEED + parts (not Python's salted hash)."""
    raw = SEED + "|" + "|".join(str(p) for p in parts)
    return int.from_bytes(hashlib.sha256(raw.encode()).digest()[:8], "big")


def _pick(seq, *parts) -> object:
    return seq[_h(*parts) % len(seq)]


@dataclass(frozen=True)
class Depot:
    """A supply node (ECD or CFS) with empty-container stock by type."""

    depot_id: str
    name: str
    kind: str                       # ECD | CFS
    lat: float
    lon: float
    stock: Dict[str, int]           # container_type -> available empties
    # Current yard dwell (minutes) — a proxy for how congested the depot is;
    # the optimiser folds it into the allocation cost.
    dwell_min: float = 0.0

    def total_stock(self) -> int:
        return sum(self.stock.values())


@dataclass(frozen=True)
class Demand:
    """An open requirement for an empty box / tank / bowser."""

    demand_id: str
    source: str                     # shipping_line | fleet_owner
    cargo_type: str                 # container | oil_tanker | break_bulk | cement_bowser
    container_type: str             # 20GP | 40GP | 40HC | REEFER
    quantity: int
    priority: str                   # high | normal | low
    origin: str
    origin_lat: float
    origin_lon: float
    destination: str = "JNPA"
    dest_lat: float = JNPA_CENTER[0]
    dest_lon: float = JNPA_CENTER[1]


def _stock_for(depot_id: str, kind: str) -> Dict[str, int]:
    """Deterministic per-type empty stock for a depot.

    ECDs carry deeper stock than CFS yards; REEFER stock is intentionally
    scarcer than dry boxes so some reefer demand can go unsatisfied (exercising
    the optimiser's partial-fill path).
    """
    base = 60 if kind == "ECD" else 25
    stock: Dict[str, int] = {}
    for ct in CONTAINER_TYPES:
        scarcity = 4 if ct == "REEFER" else 1
        n = base // scarcity + (_h("stock", depot_id, ct) % (base // 2 + 1))
        stock[ct] = int(n)
    return stock


def supply_book(depots: Optional[Iterable[Tuple]] = None) -> List[Depot]:
    """Build the deterministic supply book (ECD + CFS depots with stock)."""
    catalogue = list(depots) if depots is not None else DEPOT_CATALOGUE
    out: List[Depot] = []
    for depot_id, name, kind, lat, lon in catalogue:
        # ECDs run lighter dwell (they are purpose-built); CFS yards heavier.
        dwell_base = 18.0 if kind == "ECD" else 35.0
        dwell = round(dwell_base + (_h("dwell", depot_id) % 1800) / 100.0, 2)
        out.append(
            Depot(
                depot_id=depot_id,
                name=name,
                kind=kind,
                lat=lat,
                lon=lon,
                stock=_stock_for(depot_id, kind),
                dwell_min=dwell,
            )
        )
    return out


def _container_for(cargo_type: str, i: int) -> str:
    """Pick the container/equipment type a demand draws.

    Generic `container` cargo spreads across all dry types (incl. some REEFER);
    the cargo variants are pinned to their equipment footprint so the tanker /
    break-bulk / cement-bowser paths are always exercised.
    """
    if cargo_type == "container":
        return str(_pick(CONTAINER_TYPES, "ct", i))
    return CARGO_CONTAINER[cargo_type]


def demand_book(count: int = DEFAULT_DEMAND_COUNT) -> List[Demand]:
    """Build the deterministic demand book of `count` open requirements.

    The first records are pinned to guarantee at least one of every cargo
    variant (container / oil_tanker / break_bulk / cement_bowser) is present,
    then the remainder are filled deterministically.
    """
    out: List[Demand] = []
    for i in range(count):
        # Guarantee variant coverage on the first len(CARGO_TYPES) records.
        if i < len(CARGO_TYPES):
            cargo_type = CARGO_TYPES[i]
        else:
            cargo_type = str(_pick(CARGO_TYPES, "cargo", i))

        source = str(_pick(SOURCES, "src", i))
        container_type = _container_for(cargo_type, i)
        quantity = 1 + (_h("qty", i) % 4)            # 1..4 boxes
        priority = str(_pick(PRIORITIES, "prio", i))
        origin, olat, olon = DEMAND_ORIGINS[_h("origin", i) % len(DEMAND_ORIGINS)]

        out.append(
            Demand(
                demand_id=make_demand_id(i),
                source=source,
                cargo_type=cargo_type,
                container_type=container_type,
                quantity=quantity,
                priority=priority,
                origin=origin,
                origin_lat=olat,
                origin_lon=olon,
            )
        )
    return out


def make_demand_id(i: int) -> str:
    """Deterministic demand id (e.g. ``DEM-00007``)."""
    return f"DEM-{i:05d}"


def synthetic_demand(
    seq: int,
    cargo_type: str = "container",
    *,
    source: str = "fleet_owner",
    container_type: Optional[str] = None,
    quantity: int = 1,
    priority: str = "normal",
    origin: Optional[str] = None,
) -> Demand:
    """Build a single deterministic synthetic demand for scenario injection.

    `seq` is a caller-supplied sequence number so injected ids never collide
    with the seeded book and remain reproducible for a given `seq`.
    """
    if cargo_type not in CARGO_TYPES:
        raise ValueError(f"unknown cargo_type: {cargo_type!r}")
    if container_type is None:
        container_type = _container_for(cargo_type, 10_000 + seq)
    if origin is None:
        origin, olat, olon = DEMAND_ORIGINS[_h("synthorigin", seq) % len(DEMAND_ORIGINS)]
    else:
        # Look up known origins; fall back to JNPA center for an unknown label.
        match = next((o for o in DEMAND_ORIGINS if o[0] == origin), None)
        if match is not None:
            _, olat, olon = match
        else:
            olat, olon = JNPA_CENTER
    return Demand(
        demand_id=f"DEM-INJ-{seq:05d}",
        source=source,
        cargo_type=cargo_type,
        container_type=container_type,
        quantity=quantity,
        priority=priority,
        origin=origin,
        origin_lat=olat,
        origin_lon=olon,
    )


def depot_to_dict(depot: Depot) -> dict:
    d = asdict(depot)
    d["total_stock"] = depot.total_stock()
    return d


def demand_to_dict(demand: Demand) -> dict:
    return asdict(demand)


def _summary(depots: List[Depot], demands: List[Demand]) -> dict:
    by_cargo: Dict[str, int] = {c: 0 for c in CARGO_TYPES}
    for d in demands:
        by_cargo[d.cargo_type] += 1
    return {
        "seed": SEED,
        "depots": len(depots),
        "total_stock": sum(dep.total_stock() for dep in depots),
        "demand": len(demands),
        "demand_by_cargo": by_cargo,
    }


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Print the empty-container supply/demand books.")
    ap.add_argument("--demand", type=int, default=DEFAULT_DEMAND_COUNT, help="demand records")
    ap.add_argument("--json", action="store_true", help="dump full books as JSON")
    args = ap.parse_args(list(argv) if argv is not None else None)

    depots = supply_book()
    demands = demand_book(args.demand)
    if args.json:
        print(json.dumps({
            "supply": [depot_to_dict(d) for d in depots],
            "demand": [demand_to_dict(d) for d in demands],
        }, indent=2))
    else:
        print(json.dumps(_summary(depots, demands), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
