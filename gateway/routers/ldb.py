"""/api/ldb — Logistics Data Bank adapter (Feature 13).

Container tracking + movement history + truck port-events over the Logistics
Data Bank (LDB). Every external lookup goes through the shared integration seam
(:mod:`gateway.integrations`) so the LIVE-vs-MOCK posture is explicit and each
call is audited to core.integration_lookup. Movement history is additionally
persisted to core.ldb_movement (migration 0024) so it survives and can be
augmented with manually recorded events.

    GET  /api/ldb/container/{container_number}            -> current tracking
    GET  /api/ldb/container/{container_number}/movements  -> movement history
    POST /api/ldb/movements                               -> record a movement
    GET  /api/ldb/truck/{vehicle_number}                  -> NLDS truck port events
    GET  /api/ldb/health                                  -> configured / mode flag

LIVE truck search mirrors NLDS LDB v2:
    POST {LDB_BASE_URL}/truck/search  body {"vehiclenumber": "MH43CQ0554"}
    (public reference: https://ldb.co.in/api/ldbv2/truck/search)
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, HTTPException

from .. import integrations
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.ldb")

router = APIRouter(prefix="/api/ldb", tags=["ldb"])

_IST = ZoneInfo("Asia/Kolkata")
_PLATE_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z]{0,5}\d{4}$", re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(v: Any) -> Any:
    """asyncpg needs a real datetime for a timestamptz bind (CAST won't coerce a
    string). Convert an ISO string to datetime; pass datetimes/None through."""
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return v


def _iso(row: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize datetimes to isoformat and decode a stringified detail jsonb."""
    for k, v in list(row.items()):
        if hasattr(v, "isoformat"):
            row[k] = v.isoformat()
        elif isinstance(v, str) and k == "detail":
            try:
                row[k] = json.loads(v)
            except Exception:  # noqa: BLE001
                pass
    return row


def _fmt_ist(ms_or_iso: Any) -> Optional[str]:
    """Format LDB epoch-millis (str/int) or ISO ts as ``DD-MM-YYYY HH:MM:SS IST``."""
    if ms_or_iso is None or ms_or_iso == "":
        return None
    dt: Optional[datetime] = None
    if isinstance(ms_or_iso, (int, float)) or (isinstance(ms_or_iso, str) and ms_or_iso.isdigit()):
        try:
            dt = datetime.fromtimestamp(int(ms_or_iso) / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            dt = None
    elif isinstance(ms_or_iso, str):
        dt = _parse_ts(ms_or_iso)
    if dt is None:
        return str(ms_or_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_IST).strftime("%d-%m-%Y %H:%M:%S IST")


def _date_marker(ms_or_iso: Any) -> Optional[str]:
    label = _fmt_ist(ms_or_iso)
    return label[:10] if label and len(label) >= 10 else None


# --------------------------------------------------------------- mock builders
def _mock_container(container_number: str) -> Dict[str, Any]:
    """Deterministic LDB current-tracking record keyed off the container number."""
    return {
        "container_number": container_number,
        "status": "IN_TRANSIT",
        "current_location": "JNPA Gate-3",
        "last_event": "GATE_IN",
        "eta": (_now() + timedelta(hours=6)).isoformat(),
        "mode": "ROAD",
    }


def _mock_movements(container_number: str) -> Dict[str, Any]:
    """Deterministic 4-5 step movement chain keyed off the container number."""
    t0 = _now() - timedelta(hours=10)
    steps = [
        ("GATE_IN", "JNPA Gate-3", "NSICT", "ROAD"),
        ("YARD", "NSICT Yard Block-B", "NSICT", "ROAD"),
        ("RAIL_OUT", "JNPT Rail Terminal", "CRT", "RAIL"),
        ("VESSEL_LOAD", "Berth NSICT-2", "NSICT", "VESSEL"),
        ("DEPARTED", "Arabian Sea", "NSICT", "VESSEL"),
    ]
    movements = []
    for i, (event, location, terminal, mode) in enumerate(steps):
        movements.append({
            "ts": (t0 + timedelta(hours=2 * i)).isoformat(),
            "container_number": container_number,
            "event": event,
            "location": location,
            "terminal": terminal,
            "mode": mode,
            "detail": {"seq": i + 1},
        })
    return {"movements": movements}


def _mock_truck(vehicle_number: str) -> Dict[str, Any]:
    """NLDS-shaped truck port-events payload (MOCK when LDB_BASE_URL unset)."""
    plate = vehicle_number.strip().upper()
    digits = re.sub(r"\D", "", plate) or "0000000"
    container = f"MSMU{digits[-7:].zfill(7)}"
    t_out = _now() - timedelta(hours=2)
    t_in = _now() - timedelta(hours=20)
    terminal = "Bharat Mumbai Container Terminals (PSA)"
    events = [
        {
            "eventTime": str(int(t_out.timestamp() * 1000)),
            "eventName": "PORT OUT",
            "locName": terminal,
            "locLat": "18.938916",
            "locLong": "72.939722",
            "containerNumber": container,
            "transportMode": "TRUCK",
            "eventType": "LDB",
            "cycleType": "I",
            "orgId": "3837",
        },
        {
            "eventTime": str(int(t_in.timestamp() * 1000)),
            "eventName": "PORT IN",
            "locName": terminal,
            "locLat": "18.938916",
            "locLong": "72.939722",
            "containerNumber": container,
            "transportMode": "VESSEL",
            "eventType": "LDB",
            "cycleType": "I",
            "orgId": "3837",
        },
    ]
    return {
        "truckNumber": plate,
        "truckType": "CONTAINERIZED",
        "groupEvents": [{"timestamp": events[0]["eventTime"], "containerGroupingList": events}],
        "events": events,
    }


def _normalize_truck_payload(raw: Dict[str, Any], vehicle_number: str) -> Dict[str, Any]:
    """Unwrap NLDS ``{status, responseBody}`` and add IST display fields."""
    body = raw.get("responseBody") if isinstance(raw.get("responseBody"), dict) else raw
    if not isinstance(body, dict):
        body = {}
    plate = (body.get("truckNumber") or vehicle_number or "").strip().upper()
    events_raw: List[Dict[str, Any]] = list(body.get("events") or [])
    if not events_raw:
        for group in body.get("groupEvents") or []:
            events_raw.extend(group.get("containerGroupingList") or [])

    events: List[Dict[str, Any]] = []
    for ev in events_raw:
        if not isinstance(ev, dict):
            continue
        event_time = ev.get("eventTime") or ev.get("timestamp")
        events.append({
            **ev,
            "eventName": ev.get("eventName") or ev.get("event") or "",
            "locName": ev.get("locName") or ev.get("location") or "",
            "containerNumber": ev.get("containerNumber") or ev.get("container_number"),
            "eventTime": event_time,
            "eventTimeLabel": _fmt_ist(event_time),
            "dateMarker": _date_marker(event_time),
        })

    def _sort_key(e: Dict[str, Any]) -> int:
        t = e.get("eventTime")
        try:
            return int(t)
        except (TypeError, ValueError):
            return 0

    events.sort(key=_sort_key, reverse=True)

    terminals: Dict[str, List[Dict[str, Any]]] = {}
    for ev in events:
        terminals.setdefault(ev.get("locName") or "Unknown", []).append(ev)

    latest = events[0] if events else None
    alert = None
    if latest and latest.get("containerNumber"):
        alert = (
            f"ALERT! Trailer {plate} carrying Container No. {latest['containerNumber']}"
        )

    digits = re.sub(r"\D", "", plate) or "0000000"
    compliance = body.get("compliance") or {
        "status": "COMPLIANT",
        "owner": "JNPA DEMO FLEET" if plate != "MH43CQ0554" else "DEMO TRANSPORT LLP",
        "vehicleClass": "Goods Carriage (HMV)",
        "fitnessValidUpto": "31-03-2027" if plate == "MH43CQ0554" else "31-12-2026",
        "insuranceValidUpto": "15-11-2026" if plate == "MH43CQ0554" else "30-06-2027",
        "pucValidUpto": "01-02-2027" if plate == "MH43CQ0554" else "31-03-2027",
        "chassisNumber": (
            "MB1AA12CD3456789" if plate == "MH43CQ0554"
            else f"CH{digits[-8:].zfill(8)}"
        ),
        "engineNumber": (
            "ENG55CQ054" if plate == "MH43CQ0554"
            else f"EN{digits[-6:].zfill(6)}"
        ),
        "notes": "In-app compliance snapshot (Vahan-style). No external redirect.",
    }

    return {
        "truckNumber": plate,
        "truckType": body.get("truckType") or "CONTAINERIZED",
        "events": events,
        "groupEvents": body.get("groupEvents") or [],
        "terminals": [
            {"locName": name, "events": evs} for name, evs in terminals.items()
        ],
        "alert": alert,
        "latest": latest,
        "compliance": compliance,
    }


# --------------------------------------------------------------------- routes
@router.get("/container/{container_number}")
async def ldb_container(container_number: str,
                        state: GatewayState = Depends(get_state)) -> dict:
    """Current tracking status for a container."""
    result = await integrations.call(
        system="LDB", op="container", ref=container_number,
        request={"container_number": container_number},
        live_path=f"/container/{container_number}",
        mock_fn=lambda: _mock_container(container_number),
        dsn=state.cfg.postgres_dsn,
    )
    REQUESTS.labels("ldb", "ok").inc()
    return {"source": result["source"], "tracking": result["data"]}


@router.get("/container/{container_number}/movements")
async def ldb_movements(container_number: str,
                        state: GatewayState = Depends(get_state)) -> dict:
    """Movement history for a container.

    Reads persisted rows from core.ldb_movement first (newest first); if none
    exist, fetches the chain from the LDB adapter and persists each returned
    movement (INSERT with its source) before returning.
    """
    dsn = state.cfg.postgres_dsn

    # 1. Persisted rows first (newest first).
    if dsn:
        from jnpa_shared.db import fetch_all
        rows = await fetch_all(
            """SELECT ts, container_number, event, location, terminal, mode, source, detail
                 FROM core.ldb_movement
                WHERE container_number = :cn
                ORDER BY ts DESC""",
            {"cn": container_number}, dsn=dsn)
        if rows:
            movements = [_iso(dict(r)) for r in rows]
            REQUESTS.labels("ldb", "ok").inc()
            return {"source": "DB", "count": len(movements), "movements": movements}

    # 2. Nothing persisted -> pull from the adapter.
    result = await integrations.call(
        system="LDB", op="movements", ref=container_number,
        request={"container_number": container_number},
        live_path=f"/container/{container_number}/movements",
        mock_fn=lambda: _mock_movements(container_number),
        dsn=dsn,
    )
    movements: List[Dict[str, Any]] = list(result["data"].get("movements") or [])

    # 3. Persist each returned movement (best-effort; degrade when no DSN).
    if dsn and movements:
        from jnpa_shared.db import execute
        for m in movements:
            try:
                await execute(
                    """INSERT INTO core.ldb_movement
                         (ts, container_number, event, location, terminal, mode, source, detail)
                       VALUES (COALESCE(CAST(:ts AS timestamptz), now()), :cn, :event,
                               :location, :terminal, :mode, :source, CAST(:detail AS jsonb))""",
                    {
                        "ts": _parse_ts(m.get("ts")),
                        "cn": container_number,
                        "event": m.get("event"),
                        "location": m.get("location"),
                        "terminal": m.get("terminal"),
                        "mode": m.get("mode"),
                        "source": result["source"],
                        "detail": json.dumps(m.get("detail") or {}),
                    },
                    dsn=dsn)
            except Exception as exc:  # noqa: BLE001 - persistence is best-effort
                log.warning("ldb_movement_persist_failed",
                            container=container_number, error=str(exc))

    REQUESTS.labels("ldb", "ok").inc()
    return {"source": result["source"], "count": len(movements), "movements": movements}


@router.post("/movements")
async def record_movement(body: Dict[str, Any] = Body(...),
                          state: GatewayState = Depends(get_state)) -> dict:
    """Manually record a container movement into core.ldb_movement.

    Body: {container_number, event, location, terminal, mode, detail?}.
    """
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    container_number = body.get("container_number")
    event = body.get("event")
    if not container_number or not event:
        raise HTTPException(400, "container_number and event are required")

    from jnpa_shared.db import execute_returning
    row = await execute_returning(
        """INSERT INTO core.ldb_movement
             (container_number, event, location, terminal, mode, source, detail)
           VALUES (:cn, :event, :location, :terminal, :mode, 'MANUAL', CAST(:detail AS jsonb))
           RETURNING ts, container_number, event, location, terminal, mode, source, detail""",
        {
            "cn": container_number,
            "event": event,
            "location": body.get("location"),
            "terminal": body.get("terminal"),
            "mode": body.get("mode"),
            "detail": json.dumps(body.get("detail") or {}),
        },
        dsn=dsn)
    if not row:
        raise HTTPException(500, "insert_failed")
    REQUESTS.labels("ldb", "ok").inc()
    return {"recorded": True, "movement": _iso(dict(row))}


@router.get("/truck/{vehicle_number}")
async def ldb_truck(vehicle_number: str,
                    state: GatewayState = Depends(get_state)) -> dict:
    """NLDS-style truck port-events by vehicle / plate number.

    LIVE: ``POST {LDB_BASE_URL}/truck/search`` with ``{"vehiclenumber": "..."}``
    (same contract as https://ldb.co.in/api/ldbv2/truck/search). MOCK when
    ``LDB_BASE_URL`` is unset.

    Note: NLDS answers "Truck Details Not Found" with HTTP 500 + body
    ``{status:NOT_FOUND, code:404}``. That is a real LIVE answer (empty events),
    not a reason to invent MOCK port-events.
    """
    plate = (vehicle_number or "").strip().upper()
    if not plate:
        raise HTTPException(400, "vehicle_number_required")
    if not _PLATE_RE.match(plate):
        raise HTTPException(400, "invalid_vehicle_number_format")

    result = await integrations.call(
        system="LDB",
        op="truck_search",
        ref=plate,
        request={"vehiclenumber": plate},
        live_path="/truck/search",
        mock_fn=lambda: _mock_truck(plate),
        dsn=state.cfg.postgres_dsn,
        method="POST",
        http_client=state.http,
        # LDB uses HTTP 500 for application-level NOT_FOUND — still LIVE JSON.
        accept_json_error_bodies=True,
    )
    raw = result.get("data") or {}
    if result["source"] == "LIVE" and isinstance(raw, dict):
        if raw.get("code") not in (None, "SUC013", "200", 200) and not raw.get("responseBody"):
            log.info("ldb_truck_live_empty", plate=plate, code=raw.get("code"),
                     status=raw.get("status"))
            tracking = _normalize_truck_payload({}, plate)
            REQUESTS.labels("ldb", "ok").inc()
            return {"source": "LIVE", "tracking": tracking, "raw": raw}
        tracking = _normalize_truck_payload(raw, plate)
    else:
        tracking = _normalize_truck_payload(raw if isinstance(raw, dict) else {}, plate)

    REQUESTS.labels("ldb", "ok").inc()
    return {"source": result["source"], "tracking": tracking}


@router.get("/health")
async def ldb_health() -> dict:
    """LIVE-vs-MOCK posture for the LDB dependency."""
    return integrations.health("LDB")
