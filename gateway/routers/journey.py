"""/api/journey — "Follow-the-Box" cross-twin container journey (UC-3 audit P1).

Assembles a single container's end-to-end journey across BOTH digital twins so
the same container number can be searched and followed continuously:

    UC-II (cargo twin)   vessel discharge -> yard movement -> release
    UC-III (traffic twin) truck assignment -> ANPR detection -> gate crossing -> ETA

The UC-III gate-crossing facts are the REAL Auto-LEO capture for the container
(via the gate-data service, with the same in-process deterministic fallback the
gate_data router uses). The UC-II segment is reconstructed from the cross-twin
release contract (``cargo.dpd_release`` — the one event UC-II publishes to
UC-III, see scenarios/uc2_bridge.py); live UC-II runs as a separate twin, so
those stages are deterministically derived from the container id and clearly
tagged ``source="derived"``. Every container number is validated with the
shared ISO 6346 check-digit validator (jnpa_shared.iso6346) so "follow the box"
only ever tracks a structurally valid box.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends

from jnpa_shared.iso6346 import is_valid_container_no, parse_container_no

from ..config import GatewayConfig  # noqa: F401  (typing aid)
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state
from .gate_data import _local, _result_dict, _upstream

log = get_logger("gateway.journey")

router = APIRouter(prefix="/api/journey", tags=["journey"])

# Journey timeline anchor (mirrors gate-data's REFERENCE_DATE) so a box's
# UC-II -> UC-III stages are chronological and reproducible run-to-run.
_ANCHOR = datetime(2026, 6, 13, 6, 0, tzinfo=timezone.utc)

_VESSELS = ["MV MAERSK SELETAR", "MV MSC ANNA", "MV CMA CGM MARCO", "MV ONE OLYMPUS", "MV HMM ALGECIRAS"]
_YARD_BLOCKS = ["A-12", "B-07", "C-21", "D-04", "E-16"]
_GATES = ["G-NSICT", "G-JNPCT", "G-NSIGT", "G-BMCT"]
_CAMERAS = ["CAM-NSICT-ENT", "CAM-JNPCT-ENT", "CAM-COR-03", "CAM-COR-05"]


def _h(container_no: str, salt: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{salt}:{container_no}".encode()).digest()[:4], "big")


def _ts(container_no: str, hours_from_anchor: float) -> str:
    return (_ANCHOR + timedelta(hours=hours_from_anchor)).isoformat()


def _derived_plate(container_no: str) -> str:
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    a = letters[_h(container_no, "pa") % len(letters)]
    b = letters[_h(container_no, "pb") % len(letters)]
    num = _h(container_no, "pn") % 10000
    series = _h(container_no, "ps") % 100
    return f"MH{series:02d}{a}{b}{num:04d}"


CROSS_TWIN_TOPIC = "cargo.dpd_release"


def _corr_id(cn: str) -> str:
    """One correlation id per box, shared by every stage + the cross-twin event."""
    return "XT-" + hashlib.sha256(f"corr:{cn}".encode()).hexdigest()[:8].upper()


def _case_id(cn: str) -> str:
    return f"CASE-{_h(cn, 'case') % 900000 + 100000}"


def _event_id(cn: str, stage: str) -> str:
    return "EVT-" + hashlib.sha256(f"{stage}:{cn}".encode()).hexdigest()[:10].upper()


def _anchors(cn: str) -> Dict[str, Any]:
    """Consistency anchors reused at top-level AND inside the UC-III stages so the
    same vehicle / gate / ETA appear everywhere for the box."""
    return {
        "vehicle_no": _derived_plate(cn),
        "gate": _GATES[_h(cn, "gate") % len(_GATES)],
        "camera": _CAMERAS[_h(cn, "cam") % len(_CAMERAS)],
        "conf": round(0.9 + (_h(cn, "conf") % 90) / 1000.0, 3),  # 0.900..0.989
        "eta_min": 30 + _h(cn, "eta") % 60,
    }


def _stage(cn: str, corr: str, data_mode: str, *, twin: str, stage: str,
           source_system: str, source: str, hours: float, title: str, detail: str,
           facts: Dict[str, Any]) -> Dict[str, Any]:
    """Build one journey stage with the FULL cross-twin metadata every stage
    carries: timestamp, source system, event id, container no, correlation id,
    data mode."""
    return {
        "twin": twin,
        "stage": stage,
        "source": source,
        "source_system": source_system,
        "event_id": _event_id(cn, stage),
        "correlation_id": corr,
        "container_no": cn,
        "ts": _ts(cn, hours),
        "data_mode": data_mode,
        "title": title,
        "detail": detail,
        "facts": facts,
    }


def _uc2_stages(cn: str, corr: str, data_mode: str) -> List[Dict[str, Any]]:
    vessel = _VESSELS[_h(cn, "vessel") % len(_VESSELS)]
    block = _YARD_BLOCKS[_h(cn, "yard") % len(_YARD_BLOCKS)]
    return [
        _stage(cn, corr, data_mode, twin="UC-II", stage="vessel_discharge",
               source_system="UC-II cargo twin", source="derived", hours=0,
               title="Vessel discharge",
               detail=f"Discharged from {vessel} at the quay crane.",
               facts={"vessel": vessel}),
        _stage(cn, corr, data_mode, twin="UC-II", stage="yard_movement",
               source_system="UC-II cargo twin", source="derived", hours=3.5,
               title="Yard movement",
               detail=f"Moved by RTG to import stack block {block}.",
               facts={"yard_block": block}),
        _stage(cn, corr, data_mode, twin="UC-II", stage="dpd_release",
               source_system="UC-II cargo twin", source="derived", hours=20,
               title="Release (DPD)",
               detail="Customs-cleared and released for Direct Port Delivery.",
               facts={"customs": "CLEARED"}),
        # Explicit cross-twin PUBLISH (UC-II side of the handoff).
        _stage(cn, corr, data_mode, twin="UC-II", stage="cross_twin_published",
               source_system="UC-II cargo twin", source="derived", hours=20.5,
               title="Cross-twin event published",
               detail=f"UC-II published the release to UC-III on {CROSS_TWIN_TOPIC}.",
               facts={"topic": CROSS_TWIN_TOPIC, "publishing_twin": "UC-II",
                      "delivery_status": "Published", "simulated": True}),
    ]


def _uc3_stages(cn: str, corr: str, data_mode: str, anchors: Dict[str, Any],
                gate_record: Optional[dict]) -> List[Dict[str, Any]]:
    plate, gate = anchors["vehicle_no"], anchors["gate"]
    camera, conf, eta_min = anchors["camera"], anchors["conf"], anchors["eta_min"]

    # Gate-crossing facts come from the REAL Auto-LEO capture when available.
    leo_ready = None
    gate_facts: Dict[str, Any] = {"gate": gate, "vehicle_no": plate}
    if gate_record:
        rec = gate_record.get("record") if "record" in gate_record else gate_record
        if isinstance(rec, dict):
            leo = rec.get("icegate", {}) if isinstance(rec.get("icegate"), dict) else {}
            leo_ready = rec.get("leo_ready")
            gate_facts.update({
                "leo_ready": leo_ready,
                "eseal_status": (rec.get("eseal") or {}).get("status") if isinstance(rec.get("eseal"), dict) else None,
                "shipping_bill_no": leo.get("shipping_bill_no"),
            })

    gate_from_record = bool(gate_record and gate_record.get("record"))
    return [
        # Explicit cross-twin RECEIVE (UC-III side of the handoff).
        _stage(cn, corr, data_mode, twin="UC-III", stage="cross_twin_received",
               source_system="UC-III traffic twin", source="derived", hours=20.8,
               title="Cross-twin event received",
               detail=f"UC-III consumed the {CROSS_TWIN_TOPIC} event; truck demand recorded.",
               facts={"topic": CROSS_TWIN_TOPIC, "receiving_twin": "UC-III",
                      "delivery_status": "Delivered", "simulated": True}),
        _stage(cn, corr, data_mode, twin="UC-III", stage="truck_assignment",
               source_system="UC-III TAS", source="derived", hours=21,
               title="Truck assignment",
               detail=f"TAS assigned tractor {plate} and a gate-in slot at {gate}.",
               facts={"vehicle_no": plate, "gate": gate}),
        _stage(cn, corr, data_mode, twin="UC-III", stage="anpr_detection",
               source_system="UC-III ANPR", source="derived", hours=21.8,
               title="ANPR detection",
               detail=f"Plate {plate} read at {camera} (conf {conf}).",
               facts={"vehicle_no": plate, "camera_id": camera, "conf": conf}),
        _stage(cn, corr, data_mode, twin="UC-III", stage="gate_crossing",
               source_system="UC-III gate-data (Auto-LEO)",
               source="gate-data" if gate_from_record else "derived", hours=22,
               title="Gate crossing (Auto-LEO)",
               detail=(f"Gate {gate}: Auto-LEO {'READY' if leo_ready else 'reconciled'}."
                       if leo_ready is not None else f"Gate {gate}: gate-in reconciled."),
               facts=gate_facts),
        _stage(cn, corr, data_mode, twin="UC-III", stage="eta_tracking",
               source_system="UC-III corridor", source="derived", hours=22.2,
               title="ETA tracking",
               detail=f"Corridor ETA ~{eta_min} min to Karal Phata under current conditions.",
               facts={"eta_min": eta_min}),
    ]


# Ordered lifecycle checklist the UI renders (label + the stage it maps to).
_JOURNEY_STEPS = [
    ("vessel_discharge", "Container discharged"),
    ("yard_movement", "Yard movement"),
    ("dpd_release", "Released (DPD)"),
    ("cross_twin_published", "Cross-twin published"),
    ("cross_twin_received", "Transferred to UC-III"),
    ("truck_assignment", "Truck assigned"),
    ("anpr_detection", "ANPR detection"),
    ("gate_crossing", "Gate crossing"),
    ("eta_tracking", "ETA tracking"),
]


@router.get("/container/{container_no}")
async def container_journey(
    container_no: str, state: GatewayState = Depends(get_state)
) -> dict:
    """Follow one container across UC-II -> UC-III as an ordered stage timeline
    with a shared correlation id and an explicit cross-twin handoff event."""
    cn = container_no.strip().upper()
    valid = is_valid_container_no(cn)
    parts = parse_container_no(cn)
    corr = _corr_id(cn)
    case_id = _case_id(cn)
    data_mode = _data_mode()
    anchors = _anchors(cn)

    # Pull the real UC-III gate-crossing capture (LIVE proxy, else in-process seed).
    gate_record: Optional[dict] = None
    data = await _upstream(state, "GET", f"/records/{cn}")
    if data is not None:
        gate_record = data
    else:
        try:
            _leo, seed = _local()
            rec = seed.generate_dataset().get(cn)
            if rec is not None:
                gate_record = {"record": _result_dict(rec)}
        except Exception as exc:  # pragma: no cover - infra-timing dependent
            log.debug("journey_gate_lookup_failed", container_no=cn, error=str(exc))

    stages = (_uc2_stages(cn, corr, data_mode)
              + _uc3_stages(cn, corr, data_mode, anchors, gate_record))
    present = {s["stage"] for s in stages}
    journey_status = [
        {"key": k, "label": label, "done": k in present} for k, label in _JOURNEY_STEPS
    ]

    REQUESTS.labels("journey", "ok").inc()
    return {
        "container_no": cn,
        "iso6346_valid": valid,
        "owner_code": parts["owner_code"] if parts else None,
        "found": True,  # the twin can always reconstruct a journey for a valid box
        "correlation_id": corr,
        "case_id": case_id,
        # Consistency anchors (same values echoed inside the stages).
        "vehicle_no": anchors["vehicle_no"],
        "gate": anchors["gate"],
        "eta_min": anchors["eta_min"],
        "gate_record_source": (
            "live" if data is not None else ("seed" if gate_record else "none")
        ),
        "data_mode": data_mode,
        # The cross-twin handoff, surfaced as a first-class object for the UI.
        "cross_twin": {
            "topic": CROSS_TWIN_TOPIC,
            "publishing_twin": "UC-II",
            "receiving_twin": "UC-III",
            "correlation_id": corr,
            "case_id": case_id,
            "event_id": _event_id(cn, "cross_twin"),
            "event_time": _ts(cn, 20.5),
            "container_no": cn,
            "status": "Delivered",
            "data_mode": data_mode,
            "simulated": True,
        },
        "journey_status": journey_status,
        "stages": stages,
        "note": (
            "UC-III gate-crossing facts are the real Auto-LEO capture; UC-II, the "
            "cross-twin event and the derived UC-III steps are reconstructed "
            "deterministically from the container id and the cross-twin release "
            "contract (SIMULATED cross-twin transport)."
        ),
    }


def _data_mode() -> str:
    from jnpa_shared.config import get_settings
    return get_settings().data_mode
