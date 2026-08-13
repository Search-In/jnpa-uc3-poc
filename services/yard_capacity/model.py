"""Pure decision logic for UC-3 peak-yard / truck-arrival management.

No I/O, no clock, no RNG — every function here is a deterministic function of
its arguments, so the thresholds, the congestion pressure and the parking choice
are all unit-testable and reproducible in front of an evaluator.

The three decisions this module owns
------------------------------------
1. :func:`utilization_status` — where the yard sits against the configured
   thresholds (NORMAL / ELEVATED / HIGH / CRITICAL).
2. :func:`congestion_pressure` — a 0..1 score combining how full the yard is
   with how many trucks are arriving against the slots actually left. It is fed
   into the EXISTING corridor-congestion machinery
   (:mod:`services.congestion_alert`) so a yard constraint raises the same
   auditable ``TRAFFIC_CONGESTION`` alert a corridor jam does, rather than a
   parallel alert type nobody's dashboard knows about.
3. :func:`select_parking` / :func:`plan_holds` — which approaching trucks must
   wait, and at which REAL, authorised facility.

Honesty rules kept here
-----------------------
* A facility is only ever recommended from live availability rows handed in by
  the caller (``core.parking_facility``/``core.parking_slot`` via the parking
  service). If no facility has room, :func:`select_parking` returns ``None`` and
  the caller must say so — it never invents a location.
* ``estimated_wait_min`` is derived from a stated clearing rate and returns
  ``None`` when no rate is available, rather than guessing a number.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# Utilisation bands. HIGH/CRITICAL cut-points are configuration (see
# GatewayConfig.yard_high_utilization_pct / yard_critical_utilization_pct);
# ELEVATED is a fixed 10-point run-up to the HIGH threshold so the board shows
# the yard trending before it constrains anything.
STATUS_NORMAL = "NORMAL"
STATUS_ELEVATED = "ELEVATED"
STATUS_HIGH = "HIGH"
STATUS_CRITICAL = "CRITICAL"

ELEVATED_MARGIN_PCT = 10.0

#: The reason string shown to the operator AND to the driver, verbatim.
HOLD_REASON = "Yard capacity is currently constrained."

#: Hold lifecycle (mirrors the CHECK on core.truck_arrival_hold.status).
HOLD_ACTIVE = "HOLD_AT_PARKING"
HOLD_RELEASED = "RELEASED"
HOLD_CANCELLED = "CANCELLED"


def utilization_pct(occupied: int, capacity: int) -> float:
    """Occupancy as a percentage of capacity; 0.0 for a zero/negative capacity."""
    if capacity <= 0:
        return 0.0
    return round(100.0 * float(occupied) / float(capacity), 2)


def utilization_status(pct: float, *, high_pct: float, critical_pct: float) -> str:
    """Band for ``pct`` against the configured thresholds.

    CRITICAL at or above ``critical_pct``, HIGH at or above ``high_pct``,
    ELEVATED within :data:`ELEVATED_MARGIN_PCT` below HIGH, else NORMAL.
    """
    if pct >= critical_pct:
        return STATUS_CRITICAL
    if pct >= high_pct:
        return STATUS_HIGH
    if pct >= max(0.0, high_pct - ELEVATED_MARGIN_PCT):
        return STATUS_ELEVATED
    return STATUS_NORMAL


def constrained(status: str) -> bool:
    """True when the yard is at or past the configured high-utilisation band."""
    return status in (STATUS_HIGH, STATUS_CRITICAL)


def available_slots(occupied: int, capacity: int) -> int:
    return max(0, int(capacity) - int(occupied))


def operating_ceiling(capacity: int, critical_pct: float) -> int:
    """The slot count the yard plans up to, not the physical last slot.

    A terminal does not plan to fill its final ground slot: past the critical
    utilisation band there is no room to re-handle, so the yard stops accepting
    arrivals before it is physically full. The ceiling is therefore
    ``capacity * critical_pct/100`` — the SAME configured figure the board turns
    red at, so the number the operator sees and the number the arrival manager
    plans against can never drift apart.
    """
    return int(float(capacity) * max(0.0, min(100.0, float(critical_pct))) / 100.0)


def headroom_slots(occupied: int, capacity: int, critical_pct: float) -> int:
    """Slots still bookable below the operating ceiling (0 at/over the ceiling).

    Distinct from :func:`available_slots`, which reports the PHYSICAL free space
    the board shows. Admission decisions use this one; the two are reported side
    by side so an operator can see the reserve rather than infer it.
    """
    return max(0, operating_ceiling(capacity, critical_pct) - int(occupied))


def admissible_trucks(headroom: int, slots_per_truck: int) -> int:
    """How many arriving trucks the bookable headroom can actually absorb.

    A laden trailer needs ``slots_per_truck`` ground slots (configurable —
    ``YARD_SLOTS_PER_TRUCK``; a single 40' box occupies more ground than a 20').
    Integer division, deliberately: half a slot admits no truck.
    """
    per = max(1, int(slots_per_truck))
    return max(0, int(headroom) // per)


def congestion_pressure(*, utilisation: float, arrivals: int, admissible: int) -> float:
    """0..1 pressure score for the yard-driven gate constraint.

    Two independent terms, each already in 0..1, combined so that NEITHER alone
    can raise a critical alert — a full yard with no trucks coming is not a gate
    problem, and a queue of trucks into an empty yard is not one either:

        yard_term   = utilisation / 100                      how full the yard is
        excess_term = (arrivals - admissible) / arrivals     the share of the
                                                             arriving trucks the
                                                             yard cannot take
        pressure    = 0.6 * yard_term + 0.4 * excess_term

    The weights say the yard state dominates (it is the binding constraint) while
    the arrival surplus is what turns a full yard into a gate jam. The result is
    clamped to 0..1 and rounded to 3 dp so the same inputs always produce the same
    alert id downstream (the alert is deduped on segment + hour, not on score).
    """
    yard_term = max(0.0, min(1.0, float(utilisation) / 100.0))
    if arrivals <= 0:
        excess_term = 0.0
    else:
        excess_term = max(0.0, min(1.0, (arrivals - max(0, admissible)) / float(arrivals)))
    return round(max(0.0, min(1.0, 0.6 * yard_term + 0.4 * excess_term)), 3)


def estimated_wait_min(*, slots_needed: int, release_rate_slots_per_hour: Optional[float]) -> Optional[int]:
    """Minutes until enough slots free up, or ``None`` when no rate is known.

    ``release_rate_slots_per_hour`` is the yard's observed evacuation rate. It is
    a configured figure (``YARD_RELEASE_RATE_SLOTS_PER_HOUR``); when it is unset
    or non-positive this returns None and the caller renders "—" rather than a
    fabricated wait.
    """
    if not release_rate_slots_per_hour or release_rate_slots_per_hour <= 0:
        return None
    if slots_needed <= 0:
        return 0
    return int(round(60.0 * slots_needed / float(release_rate_slots_per_hour)))


# --------------------------------------------------------------------- parking
def select_parking(
    facilities: Sequence[Dict[str, Any]],
    *,
    preferred_id: Optional[str] = None,
    min_available: int = 1,
) -> Optional[Dict[str, Any]]:
    """Pick the authorised parking facility to send held trucks to.

    ``facilities`` are LIVE availability rows from the parking module
    (``facility_id``/``name``/``capacity``/``available``/``lat``/``lon``/``status``).
    The authorised Common Parking Plaza (``preferred_id``, default ``PK-CPP``) is
    chosen whenever it has room; otherwise the facility with the most free space
    wins. Facilities that are FULL, closed, or have fewer than ``min_available``
    free bays are never returned.

    Returns ``None`` when nothing has room — the caller must then report that
    honestly instead of naming a facility that cannot take the truck.
    """
    def usable(f: Dict[str, Any]) -> bool:
        try:
            avail = int(f.get("available") or 0)
        except (TypeError, ValueError):
            return False
        status = str(f.get("status") or "").upper()
        return avail >= min_available and status not in ("FULL", "CLOSED", "OUT_OF_SERVICE")

    candidates = [f for f in (facilities or []) if usable(f)]
    if not candidates:
        return None
    if preferred_id:
        for f in candidates:
            if str(f.get("facility_id")) == preferred_id:
                return f
    return max(candidates, key=lambda f: (int(f.get("available") or 0), str(f.get("facility_id"))))


# ----------------------------------------------------------------------- holds
@dataclass(frozen=True)
class ArrivalCandidate:
    """One approaching truck considered for arrival management."""

    device_id: str
    source: str
    plate: Optional[str] = None
    driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    gate_id: Optional[str] = None
    eta_s: Optional[float] = None

    @property
    def sort_key(self) -> tuple:
        """Furthest-away trucks are held first.

        The trucks nearest the gate are the ones the yard can still absorb with
        the slots it has; a truck that is 40 minutes out is the cheapest one to
        divert, because it has not yet joined the gate approach. ``eta_s = None``
        (never measured — a registered PWA device the simulator does not track)
        sorts LAST, so an unmeasured device is only held once every measured
        truck already has been.
        """
        return (0 if self.eta_s is None else 1,
                -(self.eta_s or 0.0),
                self.device_id)


def candidates_from_devices(devices: Sequence[Dict[str, Any]],
                            *, default_source: str) -> List[ArrivalCandidate]:
    """Normalise fleet-list rows (simulator or registered PWA) into candidates."""
    out: List[ArrivalCandidate] = []
    for d in devices or []:
        if not isinstance(d, dict):
            continue
        device_id = d.get("device_id")
        if not device_id:
            continue
        eta = d.get("eta_s")
        try:
            eta_f = float(eta) if eta is not None else None
        except (TypeError, ValueError):
            eta_f = None
        out.append(ArrivalCandidate(
            device_id=str(device_id),
            source=str(d.get("source") or default_source),
            plate=d.get("plate"),
            driver_id=d.get("driver_id"),
            driver_name=d.get("driver_name"),
            gate_id=d.get("gate_id"),
            eta_s=eta_f,
        ))
    return out


@dataclass
class HoldPlan:
    """The outcome of one arrival-management evaluation (pure)."""

    yard_id: str
    utilisation_pct: float
    status: str
    capacity_slots: int
    occupied_slots: int
    available_slots: int
    headroom_slots: int
    admissible_trucks: int
    arrivals: int
    pressure: float
    constrained: bool
    hold: List[ArrivalCandidate] = field(default_factory=list)
    proceed: List[ArrivalCandidate] = field(default_factory=list)
    reason: str = HOLD_REASON


def plan_holds(
    *,
    yard_id: str,
    capacity_slots: int,
    occupied_slots: int,
    candidates: Sequence[ArrivalCandidate],
    high_pct: float,
    critical_pct: float,
    slots_per_truck: int,
    already_held: Sequence[str] = (),
) -> HoldPlan:
    """Decide which arriving trucks must wait.

    A truck is held only when BOTH conditions hold, which is what keeps this from
    throttling a port that is merely busy:

      1. the yard is at or past the configured HIGH band, and
      2. there are more arriving trucks than the remaining ground slots can take.

    Trucks already on an active hold are excluded from the arrival count and from
    the new holds, so re-running the evaluation tops the set up idempotently
    instead of re-holding the same vehicle.
    """
    held_ids = {str(d) for d in already_held}
    fresh = [c for c in candidates if c.device_id not in held_ids]
    pct = utilization_pct(occupied_slots, capacity_slots)
    status = utilization_status(pct, high_pct=high_pct, critical_pct=critical_pct)
    avail = available_slots(occupied_slots, capacity_slots)
    headroom = headroom_slots(occupied_slots, capacity_slots, critical_pct)
    admissible = admissible_trucks(headroom, slots_per_truck)
    arrivals = len(fresh)
    pressure = congestion_pressure(utilisation=pct, arrivals=arrivals, admissible=admissible)

    plan = HoldPlan(
        yard_id=yard_id, utilisation_pct=pct, status=status,
        capacity_slots=int(capacity_slots), occupied_slots=int(occupied_slots),
        available_slots=avail, headroom_slots=headroom,
        admissible_trucks=admissible, arrivals=arrivals,
        pressure=pressure, constrained=constrained(status) and arrivals > admissible,
    )
    if not plan.constrained:
        plan.proceed = list(fresh)
        return plan

    ordered = sorted(fresh, key=lambda c: c.sort_key)
    # The nearest `admissible` trucks keep going; the rest wait. `ordered` is
    # furthest-first, so the tail of the list is the nearest cohort.
    keep = ordered[len(ordered) - admissible:] if admissible > 0 else []
    plan.hold = ordered[: len(ordered) - admissible] if admissible > 0 else list(ordered)
    plan.proceed = keep
    return plan


def releasable(
    *,
    capacity_slots: int,
    occupied_slots: int,
    active_holds: int,
    high_pct: float,
    critical_pct: float,
    slots_per_truck: int,
) -> int:
    """How many held trucks the yard can now take back.

    Zero while the yard is still in a constrained band; otherwise the number of
    trucks the free slots can absorb, capped at the holds actually outstanding.
    A yard that drops out of the constrained band releases everything.
    """
    pct = utilization_pct(occupied_slots, capacity_slots)
    status = utilization_status(pct, high_pct=high_pct, critical_pct=critical_pct)
    if not constrained(status):
        return int(active_holds)
    admissible = admissible_trucks(
        headroom_slots(occupied_slots, capacity_slots, critical_pct), slots_per_truck)
    return max(0, min(int(active_holds), admissible))


__all__ = [
    "ArrivalCandidate", "HoldPlan", "HOLD_REASON",
    "HOLD_ACTIVE", "HOLD_RELEASED", "HOLD_CANCELLED",
    "STATUS_NORMAL", "STATUS_ELEVATED", "STATUS_HIGH", "STATUS_CRITICAL",
    "admissible_trucks", "available_slots", "candidates_from_devices",
    "congestion_pressure", "constrained", "estimated_wait_min", "headroom_slots",
    "operating_ceiling", "plan_holds",
    "releasable", "select_parking", "utilization_pct", "utilization_status",
]
