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

# Map each flag to an Alert severity for the Customs feed.
_FLAG_SEVERITY = {
    FLAG_ESEAL_TAMPER: "critical",
    FLAG_WEIGHT_MISMATCH: "warning",
    FLAG_LEO_MISSING: "warning",
    FLAG_ID_MISMATCH: "critical",
    FLAG_RECORDS_MISSING: "critical",
}

_cfg = GateConfig.from_env()


@dataclass
class AutoLeoResult:
    """The outcome of reconciling one container's gate data for Auto-LEO."""

    container_no: str
    vehicle_plate: Optional[str]
    leo_ready: bool
    checks: Dict[str, Any] = field(default_factory=dict)
    customs_flags: List[str] = field(default_factory=list)

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
        )

    eseal = rec.eseal
    form13 = rec.form13
    weighbridge = rec.weighbridge
    icegate = rec.icegate

    # --- Container / vehicle identity match ---------------------------------
    # Every source record must agree on the container number, and the
    # weighbridge's vehicle plate is the haulage identity we carry forward. The
    # join is correct only when all four records reference the same container.
    id_match = (
        eseal.container_no == container_no
        and form13.container_no == container_no
        and weighbridge.container_no == container_no
        and icegate.container_no == container_no
    )
    vehicle_plate = weighbridge.vehicle_plate

    # --- Individual checks ---------------------------------------------------
    eseal_ok = not eseal.tamper_flag
    discrepancy_pct = _weight_discrepancy_pct(weighbridge.measured_wt_kg, form13.gross_wt_kg)
    weight_ok = discrepancy_pct <= tol
    leo_present = icegate.leo_status == "GRANTED"

    checks: Dict[str, Any] = {
        "id_match": id_match,
        "eseal_present": True,
        "eseal_tamper_flag": eseal.tamper_flag,
        "eseal_ok": eseal_ok,
        "form13_present": True,
        "weighbridge_present": True,
        "form13_gross_wt_kg": form13.gross_wt_kg,
        "weighbridge_measured_wt_kg": weighbridge.measured_wt_kg,
        "weight_discrepancy_pct": round(discrepancy_pct, 2),
        "weight_tolerance_pct": tol,
        "weight_ok": weight_ok,
        "icegate_present": True,
        "icegate_leo_status": icegate.leo_status,
        "leo_present": leo_present,
    }

    # --- Customs flags -------------------------------------------------------
    customs_flags: List[str] = []
    if not id_match:
        customs_flags.append(FLAG_ID_MISMATCH)
    if not eseal_ok:
        customs_flags.append(FLAG_ESEAL_TAMPER)
    if not weight_ok:
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

        alert = Alert(
            kind="CUSTOMS_FLAG",
            severity=_FLAG_SEVERITY.get(flag, "warning"),
            plate=result.vehicle_plate,
            payload=payload,
        )
        alerts.append(alert.model_dump(mode="json"))
    return alerts
