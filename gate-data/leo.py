"""Auto-LEO (Let Export Order) reconciliation for the JNPA UC-III PoC.

Implements the join + checks behind Appendix C requirements #4 and #5: the gate
captures four independent source records per export container/vehicle pair
(e-seal, Form 13, weighbridge, ICEGATE); this module joins them by
container_no / vehicle plate, performs the container-vehicle identity match,
and decides whether the container is clear for an automated Let Export Order.

Everything here is a *pure function* of the seeded dataset — no I/O, no clock,
no RNG — so the reconciliation is deterministic and unit-testable without a
running server.

A container is ``leo_ready`` only when every check passes:
    * e-seal present and not tampered          (else ESEAL_TAMPER)
    * weighbridge present, weight within tol.   (else WEIGHT_MISMATCH)
    * ICEGATE LEO present and GRANTED           (else LEO_MISSING)
    * container/vehicle identity records join    (else ID_MISMATCH)

Each failed check raises a *Customs flag*, surfaced to the dashboard's Customs
feed and shaped (via :func:`customs_alerts`) as a ``jnpa_shared`` ``Alert``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from jnpa_shared.schemas import Alert

from .config import GateConfig
from . import seed as seed_mod
from .seed import GateRecord

# Customs flag string constants.
FLAG_ESEAL_TAMPER = "ESEAL_TAMPER"
FLAG_WEIGHT_MISMATCH = "WEIGHT_MISMATCH"
FLAG_LEO_MISSING = "LEO_MISSING"
FLAG_ID_MISMATCH = "ID_MISMATCH"
FLAG_RECORDS_MISSING = "RECORDS_MISSING"
#: X4 — the weighbridge failed, so there is no weight to reconcile at all. This
#: is NOT the same condition as WEIGHT_MISMATCH (a weight that disagrees): a
#: missing weight blocks the LEO for a different reason and is remedied by
#: rerouting the truck to an alternate weighbridge and notifying customs.
FLAG_WEIGHT_MISSING = "WEIGHT_MISSING"

# Map each flag to an Alert severity for the Customs feed.
_FLAG_SEVERITY = {
    FLAG_ESEAL_TAMPER: "critical",
    FLAG_WEIGHT_MISMATCH: "warning",
    FLAG_LEO_MISSING: "warning",
    FLAG_ID_MISMATCH: "critical",
    FLAG_RECORDS_MISSING: "critical",
    FLAG_WEIGHT_MISSING: "warning",
}

# --- per-source join state ---------------------------------------------------
# Each of the four evidence streams reports its own state, so the board can say
# WHICH stream failed rather than only that the LEO is blocked. These are the
# three states a source can be in, and they are never collapsed into a boolean:
#   MATCH    — the record is present and agrees with the join key / tolerance
#   MISMATCH — the record is present but disagrees
#   MISSING  — no record was captured at all
SOURCE_MATCH = "MATCH"
SOURCE_MISMATCH = "MISMATCH"
SOURCE_MISSING = "MISSING"

#: The four evidence streams joined per export truck (tender UC3-R5).
SOURCES = ("eseal", "form13", "weighbridge", "icegate")

_cfg = GateConfig.from_env()


@dataclass
class AutoLeoResult:
    """The outcome of reconciling one container's gate data for Auto-LEO."""

    container_no: str
    vehicle_plate: Optional[str]
    leo_ready: bool
    checks: Dict[str, Any] = field(default_factory=dict)
    customs_flags: List[str] = field(default_factory=list)
    #: Per-source join state: {"eseal": "MATCH", "weighbridge": "MISSING", ...}.
    #: Additive — existing consumers that only read ``checks`` are unaffected.
    sources: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _weight_discrepancy_pct(measured_wt_kg: int, gross_wt_kg: int) -> float:
    """Relative discrepancy between weighbridge and Form-13 weight, in percent."""
    if not gross_wt_kg:
        return 0.0
    return abs(measured_wt_kg - gross_wt_kg) / gross_wt_kg * 100.0


def reconcile(
    container_no: str,
    dataset: Optional[Dict[str, GateRecord]] = None,
    weight_tolerance_pct: Optional[float] = None,
) -> AutoLeoResult:
    """Join the four source records for ``container_no`` and run the LEO checks.

    Pure function: given the same dataset and tolerance it always returns the
    same :class:`AutoLeoResult`. ``dataset`` defaults to the deterministic seed
    corpus; ``weight_tolerance_pct`` defaults to the service config (2%).
    """
    dataset = dataset if dataset is not None else seed_mod.generate_dataset()
    tol = weight_tolerance_pct if weight_tolerance_pct is not None else _cfg.weight_tolerance_pct

    rec = dataset.get(container_no)
    if rec is None:
        # No captured records at all for this container.
        return AutoLeoResult(
            container_no=container_no,
            vehicle_plate=None,
            leo_ready=False,
            checks={"records_present": False},
            customs_flags=[FLAG_RECORDS_MISSING],
            sources={s: SOURCE_MISSING for s in SOURCES},
        )

    # A stream may be absent for this container: the weighbridge failed (X4), or
    # ICEGATE has not filed yet. Absent is NOT the same as disagreeing, and it is
    # never treated as a pass — each is its own state and its own flag.
    eseal = getattr(rec, "eseal", None)
    form13 = getattr(rec, "form13", None)
    weighbridge = getattr(rec, "weighbridge", None)
    icegate = getattr(rec, "icegate", None)

    present = {
        "eseal": eseal is not None,
        "form13": form13 is not None,
        "weighbridge": weighbridge is not None,
        "icegate": icegate is not None,
    }

    # --- Container / vehicle identity match ---------------------------------
    # Every PRESENT source must agree on the container number. A stream that was
    # never captured cannot disagree, so it is excluded from the identity test
    # and reported as MISSING instead — otherwise an absent record would be
    # indistinguishable from a wrong one.
    id_parts = {name: getattr(r, "container_no", None)
                for name, r in (("eseal", eseal), ("form13", form13),
                                ("weighbridge", weighbridge), ("icegate", icegate))
                if r is not None}
    mismatched = {n: v for n, v in id_parts.items() if v != container_no}
    id_match = not mismatched
    vehicle_plate = getattr(weighbridge, "vehicle_plate", None)

    # --- Individual checks ---------------------------------------------------
    eseal_ok = (not eseal.tamper_flag) if eseal is not None else False

    # Weight reconciliation needs BOTH the declared (Form 13) and the measured
    # (weighbridge) figure. With either absent there is no discrepancy to compute
    # and none is invented: the result is a missing weight, not a passing one.
    can_weigh = weighbridge is not None and form13 is not None
    discrepancy_pct = (
        _weight_discrepancy_pct(weighbridge.measured_wt_kg, form13.gross_wt_kg)
        if can_weigh else None
    )
    weight_ok = (discrepancy_pct is not None and discrepancy_pct <= tol)
    leo_present = (icegate.leo_status == "GRANTED") if icegate is not None else False

    checks: Dict[str, Any] = {
        "id_match": id_match,
        "id_mismatched_sources": sorted(mismatched),
        "eseal_present": present["eseal"],
        "eseal_tamper_flag": getattr(eseal, "tamper_flag", None),
        "eseal_ok": eseal_ok,
        "form13_present": present["form13"],
        "weighbridge_present": present["weighbridge"],
        "form13_gross_wt_kg": getattr(form13, "gross_wt_kg", None),
        "weighbridge_measured_wt_kg": getattr(weighbridge, "measured_wt_kg", None),
        "weight_discrepancy_pct": round(discrepancy_pct, 2) if discrepancy_pct is not None else None,
        "weight_tolerance_pct": tol,
        "weight_ok": weight_ok,
        "icegate_present": present["icegate"],
        "icegate_leo_status": getattr(icegate, "leo_status", None),
        "leo_present": leo_present,
    }

    # --- Per-source join state ----------------------------------------------
    def _state(name: str, ok: bool) -> str:
        if not present[name]:
            return SOURCE_MISSING
        if name in mismatched:
            return SOURCE_MISMATCH
        return SOURCE_MATCH if ok else SOURCE_MISMATCH

    sources = {
        "eseal": _state("eseal", eseal_ok),
        # Form 13 is the declaration the others are checked against; present and
        # on the right container is all it can be asked to be.
        "form13": _state("form13", True),
        "weighbridge": _state("weighbridge", weight_ok),
        "icegate": _state("icegate", leo_present),
    }

    # --- Customs flags -------------------------------------------------------
    customs_flags: List[str] = []
    if not id_match:
        customs_flags.append(FLAG_ID_MISMATCH)
    if eseal is None:
        customs_flags.append(FLAG_RECORDS_MISSING)
    elif not eseal_ok:
        customs_flags.append(FLAG_ESEAL_TAMPER)
    if not can_weigh:
        # X4: no weight to reconcile. Distinct from a weight that disagrees.
        customs_flags.append(FLAG_WEIGHT_MISSING)
    elif not weight_ok:
        customs_flags.append(FLAG_WEIGHT_MISMATCH)
    if not leo_present:
        customs_flags.append(FLAG_LEO_MISSING)

    leo_ready = not customs_flags

    return AutoLeoResult(
        container_no=container_no,
        vehicle_plate=vehicle_plate,
        leo_ready=leo_ready,
        checks=checks,
        customs_flags=customs_flags,
        sources=sources,
    )


def reconcile_all(
    dataset: Optional[Dict[str, GateRecord]] = None,
    weight_tolerance_pct: Optional[float] = None,
) -> List[AutoLeoResult]:
    """Reconcile every container in the dataset (the Auto-LEO queue feed).

    Results are sorted by container number so the queue order is deterministic.
    """
    dataset = dataset if dataset is not None else seed_mod.generate_dataset()
    return [
        reconcile(cn, dataset=dataset, weight_tolerance_pct=weight_tolerance_pct)
        for cn in sorted(dataset)
    ]


def customs_alerts(result: AutoLeoResult) -> List[dict]:
    """Shape a reconciliation result's Customs flags as ``jnpa_shared`` Alerts.

    Each flag becomes one Alert dict with ``kind="CUSTOMS_FLAG"``, a per-flag
    ``severity`` and a payload carrying the container/vehicle identity and the
    relevant check detail — ready to drop onto the dashboard's Customs feed.
    """
    alerts: List[dict] = []
    for flag in result.customs_flags:
        payload: Dict[str, Any] = {
            "flag": flag,
            "container_no": result.container_no,
            "vehicle_plate": result.vehicle_plate,
            "leo_ready": result.leo_ready,
        }
        # Attach the specific check detail that triggered the flag.
        if flag == FLAG_WEIGHT_MISMATCH:
            payload["weight_discrepancy_pct"] = result.checks.get("weight_discrepancy_pct")
            payload["form13_gross_wt_kg"] = result.checks.get("form13_gross_wt_kg")
            payload["weighbridge_measured_wt_kg"] = result.checks.get(
                "weighbridge_measured_wt_kg"
            )
        elif flag == FLAG_LEO_MISSING:
            payload["icegate_leo_status"] = result.checks.get("icegate_leo_status")
        elif flag == FLAG_WEIGHT_MISSING:
            # X4: the remedy is operational, so the alert carries it. Customs is
            # notified because an export leaving without a verified weight is a
            # customs matter, not just a yard one.
            payload["weighbridge_present"] = result.checks.get("weighbridge_present")
            payload["form13_present"] = result.checks.get("form13_present")
            payload["remedy"] = "REROUTE_TO_ALTERNATE_WEIGHBRIDGE"
            payload["customs_notified"] = True

        alert = Alert(
            kind="CUSTOMS_FLAG",
            severity=_FLAG_SEVERITY.get(flag, "warning"),
            plate=result.vehicle_plate,
            payload=payload,
        )
        alerts.append(alert.model_dump(mode="json"))
    return alerts
