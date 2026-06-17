"""Pure, transparent empty-container supply-demand matcher (Appendix C #3).

Given a supply book (ECD + CFS depots with empty-container stock) and a demand
book (shipping-line bookings + fleet-owner requests), :func:`allocate` produces
a *probable allocation* — one :class:`Allocation` record per satisfiable demand,
naming the depot, container type and cargo variant, plus the estimated
turn-round time (``est_trt_min``) that feeds the **TRT-for-empty-from-ECD** KPI.

Design goals (deliberately *not* a black box):

  * **Explainable.** The allocation cost is a plain weighted sum of three
    intuitive terms — haversine distance, current depot dwell, and demand
    priority — so an operator can read *why* a depot was chosen. The component
    terms are returned on every record.
  * **Deterministic.** No RNG, no wall-clock; identical books -> identical
    allocations. Ties break on a stable (cost, depot_id) order.
  * **Stock-aware.** Depot stock is decremented as demands are filled, so two
    demands competing for the last reefer don't both "win". Demand is processed
    high-priority-first (then by id) so the order is reproducible.

The matcher is greedy/transparent by intent: the PoC values an allocation an
operator can audit over a marginally cheaper one from an opaque solver.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, List, Sequence, Tuple

from .seed import Demand, Depot

# Haversine lives in the shared corridor module; reuse it so distance maths is
# defined once across the whole UC-III system.
from jnpa_shared.corridor import haversine_km

# --- Cost weights (explainable knobs) ---------------------------------------
# Cost = W_DISTANCE * distance_km
#      + W_DWELL    * depot_dwell_min
#      + W_PRIORITY * priority_penalty
# A *lower* cost is a better match. Priority enters as a penalty so that, all
# else equal, a depot is *more* attractive for a high-priority demand (its
# penalty is smaller), nudging scarce stock toward urgent moves.
W_DISTANCE = 1.0       # per km
W_DWELL = 0.5          # per minute of current depot dwell
W_PRIORITY = 8.0       # per priority rank step

PRIORITY_RANK: Dict[str, int] = {"high": 0, "normal": 1, "low": 2}

# TRT model (minutes): drive time from depot to demand origin assuming a mean
# road speed, plus the depot's current handling/dwell, plus a fixed gate/admin
# turn at the ECD. Tuned so a nearby low-dwell ECD lands well under the 45-min
# target and a far congested CFS lands above it.
ROAD_SPEED_KMPH = 30.0     # congested hinterland truck speed
GATE_TURN_MIN = 6.0        # fixed ECD gate + paperwork turn (automated lane)


@dataclass(frozen=True)
class Allocation:
    """One probable allocation: which depot serves which demand, and at what TRT."""

    demand_id: str
    supply_depot: str          # depot_id of the chosen ECD/CFS
    depot_kind: str            # ECD | CFS
    source: str                # shipping_line | fleet_owner (the demand's owner)
    container_type: str        # 20GP | 40GP | 40HC | REEFER
    cargo_type: str            # container | oil_tanker | break_bulk | cement_bowser
    quantity: int
    distance_km: float
    est_trt_min: float
    confidence: float          # 0..1 — how strong the match is vs alternatives
    # Explainability: the cost and its components for the chosen depot.
    cost: float
    cost_distance: float
    cost_dwell: float
    cost_priority: float

    def to_dict(self) -> dict:
        return asdict(self)


def _priority_penalty(priority: str) -> float:
    return float(PRIORITY_RANK.get(priority, 1))


def _cost_components(depot: Depot, demand: Demand) -> Tuple[float, float, float, float, float]:
    """Return (total_cost, dist_term, dwell_term, prio_term, distance_km)."""
    distance_km = haversine_km((depot.lat, depot.lon), (demand.origin_lat, demand.origin_lon))
    dist_term = W_DISTANCE * distance_km
    dwell_term = W_DWELL * depot.dwell_min
    prio_term = W_PRIORITY * _priority_penalty(demand.priority)
    total = dist_term + dwell_term + prio_term
    return total, dist_term, dwell_term, prio_term, distance_km


def est_trt_min(distance_km: float, depot: Depot) -> float:
    """Estimated empty-container turn-round time (minutes) for one allocation.

    drive_time + depot_dwell + fixed gate turn. This is the per-allocation
    sample that the TRT-empty-from-ECD KPI averages over.
    """
    drive_min = (distance_km / ROAD_SPEED_KMPH) * 60.0
    return round(drive_min + depot.dwell_min + GATE_TURN_MIN, 2)


def _confidence(best_cost: float, second_cost: float | None) -> float:
    """Confidence 0..1: how much cheaper the winner is than the runner-up.

    With a clear-cut winner (runner-up much costlier) confidence -> high; when
    the top two depots are near-tied it drops toward 0.5. With no alternative it
    is a flat 0.6 (we matched, but there was nothing to compare against).
    """
    if second_cost is None:
        return 0.6
    if second_cost <= 0:
        return 0.5
    margin = (second_cost - best_cost) / second_cost   # 0..1
    return round(0.5 + 0.5 * max(0.0, min(1.0, margin)), 3)


def _demand_sort_key(d: Demand) -> Tuple[int, str]:
    """High priority first, then stable by id — reproducible processing order."""
    return (PRIORITY_RANK.get(d.priority, 1), d.demand_id)


def allocate(supply: Sequence[Depot], demand: Sequence[Demand]) -> List[Allocation]:
    """Match every satisfiable demand to its lowest-cost depot.

    Returns one :class:`Allocation` per demand that could be served from current
    stock, in the deterministic order demands were processed. Demands with no
    depot holding the required container type (or whose stock is exhausted) are
    skipped — the caller can compare counts to see unsatisfied demand.
    """
    # Mutable working copy of stock so fills decrement availability.
    stock: Dict[str, Dict[str, int]] = {
        dep.depot_id: dict(dep.stock) for dep in supply
    }
    by_id: Dict[str, Depot] = {dep.depot_id: dep for dep in supply}

    allocations: List[Allocation] = []
    for d in sorted(demand, key=_demand_sort_key):
        # Candidate depots: those still holding >=1 of the required type.
        candidates: List[Tuple[float, float, float, float, float, Depot]] = []
        for dep in supply:
            if stock[dep.depot_id].get(d.container_type, 0) <= 0:
                continue
            total, dist_t, dwell_t, prio_t, dist_km = _cost_components(dep, d)
            candidates.append((total, dist_t, dwell_t, prio_t, dist_km, dep))

        if not candidates:
            continue  # unsatisfiable from current stock

        # Stable lowest-cost pick (ties break on depot_id).
        candidates.sort(key=lambda c: (c[0], c[5].depot_id))
        best = candidates[0]
        second_cost = candidates[1][0] if len(candidates) > 1 else None
        total, dist_t, dwell_t, prio_t, dist_km, dep = best

        # Decrement stock by the demand quantity (capped at availability).
        avail = stock[dep.depot_id][d.container_type]
        filled = min(avail, d.quantity)
        stock[dep.depot_id][d.container_type] = avail - filled

        trt = est_trt_min(dist_km, dep)
        allocations.append(
            Allocation(
                demand_id=d.demand_id,
                supply_depot=dep.depot_id,
                depot_kind=dep.kind,
                source=d.source,
                container_type=d.container_type,
                cargo_type=d.cargo_type,
                quantity=filled,
                distance_km=round(dist_km, 2),
                est_trt_min=trt,
                confidence=_confidence(total, second_cost),
                cost=round(total, 2),
                cost_distance=round(dist_t, 2),
                cost_dwell=round(dwell_t, 2),
                cost_priority=round(prio_t, 2),
            )
        )

    # Return in demand-id order for a stable, demo-friendly listing.
    allocations.sort(key=lambda a: a.demand_id)
    return allocations


def mean_est_trt(allocations: Sequence[Allocation]) -> float:
    """Mean ``est_trt_min`` over allocations (the scalar the KPI engine scores).

    Returns 0.0 for an empty allocation set so callers never divide by zero.
    """
    if not allocations:
        return 0.0
    return sum(a.est_trt_min for a in allocations) / len(allocations)
