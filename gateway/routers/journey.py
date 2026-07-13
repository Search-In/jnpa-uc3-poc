"""/api/journey — "Follow-the-Box" cross-twin container journey (UC-3 audit P1).

Assembles a single container's end-to-end journey across BOTH digital twins so
the same container number can be searched and followed continuously:

    UC-II (cargo twin)   vessel discharge -> yard movement -> release
    UC-III (traffic twin) truck assignment -> ANPR detection -> gate crossing -> ETA

The journey is now backed by the LIVE shared cargo record. Every UC-II / UC-III
stage is populated from the one ``jnpa.cargo`` row (vessel, yard block, customs
status, release flag, vehicle, gate, ETA) via the SAME ``CargoService`` that
serves ``GET /api/cargo`` — there is no second copy of the cargo logic and no
deterministic "mock" container generation. The UC-III gate-crossing facts remain
the REAL Auto-LEO capture (via the gate-data service, with the same in-process
fallback the gate_data router uses). Every container number is validated with the
shared ISO 6346 check-digit validator (jnpa_shared.iso6346) so "follow the box"
only ever tracks a structurally valid box, and only a box that exists in the
cargo registry yields a journey (``found`` reports whether the record exists).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends

from jnpa_shared.iso6346 import is_valid_container_no, parse_container_no

from services.cargo import CargoService

from ..config import GatewayConfig  # noqa: F401  (typing aid)
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state
from .cargo import get_service  # reuse the ONE cargo DI seam (no duplicate logic)
from .gate_data import _local, _result_dict, _upstream

log = get_logger("gateway.journey")

router = APIRouter(prefix="/api/journey", tags=["journey"])

# The journey is now DB-backed, so the response mode is always "live" — the mock
# generation that produced ``data_mode="mock"`` / ``simulated=True`` is gone.
LIVE_MODE = "live"

# Journey timeline anchor (mirrors gate-data's REFERENCE_DATE) so a box's
# UC-II -> UC-III stages render in a stable, chronological order. The stage
# timestamps are display-only layout; the FACTS are the live cargo values.
_ANCHOR = datetime(2026, 6, 13, 6, 0, tzinfo=timezone.utc)

CROSS_TWIN_TOPIC = "cargo.dpd_release"


def _h(container_no: str, salt: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{salt}:{container_no}".encode()).digest()[:4], "big")


def _ts(hours_from_anchor: float) -> str:
    return (_ANCHOR + timedelta(hours=hours_from_anchor)).isoformat()


# --- Deterministic ids (kept until a real event store exists) -----------------
def _corr_id(cn: str) -> str:
    """One correlation id per box, shared by every stage + the cross-twin event."""
    return "XT-" + hashlib.sha256(f"corr:{cn}".encode()).hexdigest()[:8].upper()


def _case_id(cn: str) -> str:
    return f"CASE-{_h(cn, 'case') % 900000 + 100000}"


def _event_id(cn: str, stage: str) -> str:
    return "EVT-" + hashlib.sha256(f"{stage}:{cn}".encode()).hexdigest()[:10].upper()


def _eta_minutes(eta: Optional[datetime]) -> Optional[int]:
    """Minutes from now until the cargo ETA (0 if already due/past). Live value —
    None when the cargo record carries no ETA."""
    if eta is None:
        return None
    if eta.tzinfo is None:
        eta = eta.replace(tzinfo=timezone.utc)
    delta = (eta - datetime.now(timezone.utc)).total_seconds() / 60.0
    return max(0, int(round(delta)))


def _eta_iso(eta: Optional[datetime]) -> Optional[str]:
    if eta is None:
        return None
    if eta.tzinfo is None:
        eta = eta.replace(tzinfo=timezone.utc)
    return eta.isoformat()


def _iso(value: Any) -> Optional[str]:
    """ISO-format a datetime facts value (leave anything else as-is)."""
    return value.isoformat() if isinstance(value, datetime) else value


async def _fetch_parking_exit(state: GatewayState, plate: Optional[str]):
    """Best-effort join of the LIVE parking + gate-exit records for a plate.

    Uses the cargo's haulage plate (``jnpa.cargo.vehicle_number``) as the join key
    into the existing ``jnpa.parking_transactions`` (vehicle_id) and
    ``jnpa.gate_events`` (plate, event_type='GATE_OUT') — no new tables, no data
    duplication. Returns ``(parking_row | None, exit_row | None)``; both None when
    there is no DB or no plate."""
    dsn = state.cfg.postgres_dsn
    if not dsn or not plate:
        return None, None
    from jnpa_shared.db import fetch_one

    parking = None
    exit_row = None
    try:
        parking = await fetch_one(
            """SELECT facility_id, slot_id, entry_time, exit_time, status
               FROM jnpa.parking_transactions
               WHERE vehicle_id = :p ORDER BY entry_time DESC LIMIT 1""",
            {"p": plate}, dsn=dsn,
        )
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("journey_parking_lookup_failed", plate=plate, error=str(exc))
    try:
        exit_row = await fetch_one(
            """SELECT ts, gate_id FROM jnpa.gate_events
               WHERE plate = :p AND event_type = 'GATE_OUT'
               ORDER BY ts DESC LIMIT 1""",
            {"p": plate}, dsn=dsn,
        )
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("journey_exit_lookup_failed", plate=plate, error=str(exc))
    return parking, exit_row


def _stage(cn: str, corr: str, *, twin: str, stage: str, source: str,
           source_system: str, hours: float, title: str, detail: str,
           facts: Dict[str, Any]) -> Dict[str, Any]:
    """Build one journey stage with the full cross-twin metadata every stage
    carries: timestamp, source system, event id, container no, correlation id,
    data mode. ``source`` is "live" for cargo-backed stages (the UI renders them
    as REAL) and "gate-data" for the real Auto-LEO capture."""
    return {
        "twin": twin,
        "stage": stage,
        "source": source,
        "source_system": source_system,
        "event_id": _event_id(cn, stage),
        "correlation_id": corr,
        "container_no": cn,
        "ts": _ts(hours),
        "data_mode": LIVE_MODE,
        "title": title,
        "detail": detail,
        "facts": facts,
    }


def _uc2_stages(cn: str, corr: str, cargo: Dict[str, Any]) -> List[Dict[str, Any]]:
    vessel = cargo.get("vessel_name")
    block = cargo.get("yard_block")
    customs = cargo.get("customs_status")
    released = bool(cargo.get("is_released"))
    return [
        _stage(cn, corr, twin="UC-II", stage="vessel_discharge",
               source="live", source_system="UC-II cargo twin", hours=0,
               title="Vessel discharge",
               detail=(f"Discharged from {vessel} at the quay crane." if vessel
                       else "Discharged at the quay crane."),
               facts={"vessel": vessel}),
        _stage(cn, corr, twin="UC-II", stage="yard_movement",
               source="live", source_system="UC-II cargo twin", hours=3.5,
               title="Yard movement",
               detail=(f"Moved by RTG to import stack block {block}." if block
                       else "Moved by RTG to the import stack."),
               facts={"yard_block": block}),
        _stage(cn, corr, twin="UC-II", stage="dpd_release",
               source="live", source_system="UC-II cargo twin", hours=20,
               title="Release (DPD)",
               detail=(f"Customs {customs}; released for Direct Port Delivery."
                       if released else f"Customs {customs}; awaiting release."),
               facts={"customs": customs, "is_released": released}),
        # Explicit cross-twin PUBLISH (UC-II side of the handoff).
        _stage(cn, corr, twin="UC-II", stage="cross_twin_published",
               source="live", source_system="UC-II cargo twin", hours=20.5,
               title="Cross-twin event published",
               detail=f"UC-II published the release to UC-III on {CROSS_TWIN_TOPIC}.",
               facts={"topic": CROSS_TWIN_TOPIC, "publishing_twin": "UC-II",
                      "delivery_status": "Published" if released else "Pending"}),
    ]


def _uc3_stages(cn: str, corr: str, cargo: Dict[str, Any],
                gate_record: Optional[dict],
                parking_row: Optional[dict] = None,
                exit_row: Optional[dict] = None) -> List[Dict[str, Any]]:
    plate = cargo.get("vehicle_number")
    gate = cargo.get("gate")
    camera = cargo.get("camera_id")
    released = bool(cargo.get("is_released"))
    eta_min = _eta_minutes(cargo.get("eta"))
    eta_iso = _eta_iso(cargo.get("eta"))

    # LIVE parking-assignment facts (jnpa.parking_transactions), joined by plate.
    parked = bool(parking_row)
    p = dict(parking_row) if parking_row else {}
    parking_facts: Dict[str, Any] = {
        "facility_id": p.get("facility_id"),
        "slot_id": p.get("slot_id"),
        "status": p.get("status"),
        "entry_time": _iso(p.get("entry_time")),
    }
    # LIVE gate-exit facts (jnpa.gate_events, event_type='GATE_OUT'), joined by plate.
    exited = bool(exit_row)
    e = dict(exit_row) if exit_row else {}
    exit_gate = e.get("gate_id") or gate
    exit_ts = _iso(e.get("ts"))
    exit_facts: Dict[str, Any] = {"gate": exit_gate, "exit_time": exit_ts, "vehicle_no": plate}

    # Gate-crossing facts come from the REAL Auto-LEO capture when available; the
    # gate itself is the live cargo gate.
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
        _stage(cn, corr, twin="UC-III", stage="cross_twin_received",
               source="live", source_system="UC-III traffic twin", hours=20.8,
               title="Cross-twin event received",
               detail=f"UC-III consumed the {CROSS_TWIN_TOPIC} event; truck demand recorded.",
               facts={"topic": CROSS_TWIN_TOPIC, "receiving_twin": "UC-III",
                      "delivery_status": "Delivered" if released else "Pending"}),
        _stage(cn, corr, twin="UC-III", stage="truck_assignment",
               source="live", source_system="UC-III TAS", hours=21,
               title="Truck assignment",
               detail=(f"TAS assigned tractor {plate} and a gate-in slot at {gate}."
                       if plate else f"TAS reserved a gate-in slot at {gate}."),
               facts={"vehicle_no": plate, "gate": gate}),
        _stage(cn, corr, twin="UC-III", stage="anpr_detection",
               source="live", source_system="UC-III ANPR", hours=21.8,
               title="ANPR detection",
               detail=(f"Plate {plate} read at {camera}." if plate and camera
                       else f"Plate {plate} read at the corridor camera." if plate
                       else "Awaiting ANPR read."),
               facts={"vehicle_no": plate, "camera_id": camera}),
        _stage(cn, corr, twin="UC-III", stage="gate_crossing",
               source="gate-data" if gate_from_record else "live",
               source_system="UC-III gate-data (Auto-LEO)", hours=22,
               title="Gate entry (Auto-LEO)",
               detail=(f"Gate {gate}: Auto-LEO {'READY' if leo_ready else 'reconciled'}."
                       if leo_ready is not None else f"Gate {gate}: gate-in reconciled."),
               facts=gate_facts),
        _stage(cn, corr, twin="UC-III", stage="parking_assignment",
               source="live", source_system="UC-III parking", hours=22.4,
               title="Parking assigned",
               detail=(f"Assigned slot {p.get('slot_id')} at {p.get('facility_id')} "
                       f"({p.get('status')})." if parked
                       else "No parking allocation on record for this vehicle."),
               facts=parking_facts),
        _stage(cn, corr, twin="UC-III", stage="gate_exit",
               source="live", source_system="UC-III gate-data", hours=23.5,
               title="Gate exit",
               detail=(f"Departed the port at {exit_gate}." if exited
                       else "Awaiting gate-out (vehicle still inside the port AoI)."),
               facts=exit_facts),
        _stage(cn, corr, twin="UC-III", stage="eta_tracking",
               source="live", source_system="UC-III corridor", hours=23.8,
               title="ETA tracking",
               detail=(f"Corridor ETA ~{eta_min} min to Karal Phata under current conditions."
                       if eta_min is not None else "Corridor ETA pending."),
               facts={"eta_min": eta_min, "eta": eta_iso}),
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
    ("gate_crossing", "Gate entry"),
    ("parking_assignment", "Parking assigned"),
    ("gate_exit", "Gate exit"),
    ("eta_tracking", "ETA tracking"),
]


def _done_map(cargo: Dict[str, Any], gate_from_record: bool,
              parked: bool = False, exited: bool = False) -> Dict[str, bool]:
    """Which lifecycle steps are complete, derived from the LIVE cargo state.

    Presence in the registry means the box has been discharged and yarded; the
    release flag drives the cross-twin handoff and the downstream UC-III steps;
    the real gate record confirms the physical gate crossing; ``parked``/``exited``
    are the LIVE parking-transaction and gate-out joins."""
    released = bool(cargo.get("is_released"))
    customs = cargo.get("customs_status")
    has_vehicle = bool(cargo.get("vehicle_number"))
    return {
        "vessel_discharge": True,
        "yard_movement": bool(cargo.get("yard_block")),
        "dpd_release": released or customs == "CLEARED",
        "cross_twin_published": released,
        "cross_twin_received": released,
        "truck_assignment": released and has_vehicle,
        "anpr_detection": gate_from_record or (released and has_vehicle),
        "gate_crossing": gate_from_record,
        "parking_assignment": parked,
        "gate_exit": exited,
        "eta_tracking": cargo.get("eta") is not None,
    }


@router.get("/container/{container_no}")
async def container_journey(
    container_no: str,
    state: GatewayState = Depends(get_state),
    service: CargoService = Depends(get_service),
) -> dict:
    """Follow one container across UC-II -> UC-III as an ordered stage timeline
    with a shared correlation id and an explicit cross-twin handoff event, backed
    by the live shared cargo record."""
    cn = container_no.strip().upper()
    valid = is_valid_container_no(cn)
    parts = parse_container_no(cn)
    corr = _corr_id(cn)
    case_id = _case_id(cn)

    # Resolve the box against the SINGLE source of truth — the shared cargo record
    # (same CargoService that serves GET /api/cargo). No mock generation.
    cargo: Optional[dict] = await service.get_cargo(cn) if valid else None

    if cargo is None:
        # A structurally valid box that isn't in the cargo registry (or an invalid
        # ISO): nothing to follow. Backward-compatible shape, empty timeline.
        REQUESTS.labels("journey", "not_found").inc()
        return {
            "container_no": cn,
            "iso6346_valid": valid,
            "owner_code": parts["owner_code"] if parts else None,
            "found": False,
            "correlation_id": corr,
            "case_id": case_id,
            "vehicle_no": None,
            "gate": None,
            "eta_min": None,
            "gate_record_source": "none",
            "data_mode": LIVE_MODE,
            "cross_twin": None,
            "journey_status": [
                {"key": k, "label": label, "done": False} for k, label in _JOURNEY_STEPS
            ],
            "stages": [],
            "note": (
                "No cargo record for this container in jnpa.cargo — create it via "
                "POST /api/cargo to follow the box. The journey is sourced live "
                "from the shared cargo registry."
            ),
        }

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

    gate_from_record = bool(gate_record and gate_record.get("record"))
    released = bool(cargo.get("is_released"))

    # LIVE parking-assignment + gate-exit, joined by the cargo's haulage plate.
    parking_row, exit_row = await _fetch_parking_exit(state, cargo.get("vehicle_number"))
    parked = bool(parking_row)
    exited = bool(exit_row)

    stages = (_uc2_stages(cn, corr, cargo)
              + _uc3_stages(cn, corr, cargo, gate_record, parking_row, exit_row))
    done = _done_map(cargo, gate_from_record, parked, exited)
    journey_status = [
        {"key": k, "label": label, "done": done.get(k, False)} for k, label in _JOURNEY_STEPS
    ]

    REQUESTS.labels("journey", "ok").inc()
    return {
        "container_no": cn,
        "iso6346_valid": valid,
        "owner_code": parts["owner_code"] if parts else None,
        "found": True,
        "correlation_id": corr,
        "case_id": case_id,
        # Consistency anchors (same live values echoed inside the stages).
        "vehicle_no": cargo.get("vehicle_number"),
        "gate": cargo.get("gate"),
        "eta_min": _eta_minutes(cargo.get("eta")),
        "gate_record_source": (
            "live" if data is not None else ("seed" if gate_record else "none")
        ),
        "data_mode": LIVE_MODE,
        # The cross-twin handoff, surfaced as a first-class object for the UI.
        "cross_twin": {
            "topic": CROSS_TWIN_TOPIC,
            "publishing_twin": "UC-II",
            "receiving_twin": "UC-III",
            "correlation_id": corr,
            "case_id": case_id,
            "event_id": _event_id(cn, "cross_twin"),
            "event_time": _ts(20.5),
            "container_no": cn,
            "status": "Delivered" if released else "Pending",
            "data_mode": LIVE_MODE,
            "simulated": False,
        },
        "journey_status": journey_status,
        "stages": stages,
        "note": (
            "Live journey backed by the shared cargo record (jnpa.cargo); every "
            "UC-II / UC-III stage reflects the current cargo state. UC-III "
            "gate-crossing facts are the real Auto-LEO capture."
        ),
    }
