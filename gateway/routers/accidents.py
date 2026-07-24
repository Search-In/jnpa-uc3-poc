"""/api/accidents — Accident lifecycle (UC-III completion, Feature 1).

Full lifecycle: detect/report -> investigate -> resolve -> close, with an
append-only timeline, vehicle/driver association, severity, a dashboard rollup,
a WebSocket ``accident`` broadcast and a control-room alert (reusing the existing
core.alert pump + notification dispatcher). RDS-backed (core.accident +
core.accident_event). Additive — no existing endpoint/table is touched.

    POST   /api/accidents                  -> report a new accident (+timeline REPORTED)
    GET    /api/accidents                   -> list (filter: status, type, vehicle_id, limit)
    GET    /api/accidents/dashboard         -> KPI rollup (open/by-severity/by-status/by-type)
    GET    /api/accidents/{id}              -> one accident + its timeline
    POST   /api/accidents/{id}/status       -> transition status (+timeline)
    POST   /api/accidents/{id}/investigation-> set investigation_status (+timeline)
    POST   /api/accidents/{id}/resolve      -> RESOLVED + resolution note (+timeline)
    POST   /api/accidents/{id}/note         -> append a timeline note
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..logging import get_logger
from ..metrics import REQUESTS
from ..notifications import dispatch_alert
from ..state import GatewayState, get_state

log = get_logger("gateway.accidents")

router = APIRouter(prefix="/api/accidents", tags=["accidents"])

_SEVERITY = {"MINOR", "MODERATE", "MAJOR", "FATAL"}
_TYPES = {"PREMISES", "ENROUTE"}
_STATUS = {"REPORTED", "INVESTIGATING", "RESOLVED", "CLOSED"}
_INVESTIGATION = {"PENDING", "IN_PROGRESS", "COMPLETED"}
# Severity that warrants a driver/control-room push.
_ALERT_SEVERITY = {"MAJOR", "FATAL"}


def _parse_ts(v: Any) -> Any:
    """asyncpg needs a real datetime (not an ISO string) for a timestamptz bind,
    even through CAST(... AS timestamptz). Convert an ISO string to datetime;
    pass datetimes/None through."""
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return v


def _iso(row: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in list(row.items()):
        if hasattr(v, "isoformat"):
            row[k] = v.isoformat()
        elif isinstance(v, str) and k in ("location", "detail"):
            try:
                row[k] = json.loads(v)
            except Exception:  # noqa: BLE001
                pass
    return row


async def _timeline(dsn: str, accident_id: int, *, action: str, old: Optional[str],
                    new: Optional[str], note: Optional[str], actor: Optional[str]) -> None:
    from jnpa_shared.db import execute
    await execute(
        """INSERT INTO core.accident_event (accident_id, action, old_status, new_status, note, actor)
           VALUES (:aid, :action, :old, :new, :note, :actor)""",
        {"aid": accident_id, "action": action, "old": old, "new": new, "note": note, "actor": actor},
        dsn=dsn,
    )


@router.post("")
async def report_accident(body: Dict[str, Any] = Body(...),
                          state: GatewayState = Depends(get_state)) -> dict:
    """Report a new accident. Body: {accident_type, severity, lat, lon, location,
    vehicle_id, plate, driver_id, description, reported_by, source}."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute, execute_returning

    a_type = str(body.get("accident_type") or "ENROUTE").upper()
    severity = str(body.get("severity") or "MINOR").upper()
    if a_type not in _TYPES:
        raise HTTPException(400, f"accident_type must be one of {sorted(_TYPES)}")
    if severity not in _SEVERITY:
        raise HTTPException(400, f"severity must be one of {sorted(_SEVERITY)}")

    row = await execute_returning(
        """INSERT INTO core.accident
             (accident_type, severity, lat, lon, location, vehicle_id, plate,
              driver_id, description, reported_by, source, occurred_at)
           VALUES (:type, :sev, :lat, :lon, CAST(:loc AS jsonb), :vid, :plate,
              :did, :desc, :by, :src, COALESCE(CAST(:occurred AS timestamptz), now()))
           RETURNING *""",
        {
            "type": a_type, "sev": severity,
            "lat": body.get("lat"), "lon": body.get("lon"),
            "loc": json.dumps(body.get("location") or {}),
            "vid": body.get("vehicle_id"), "plate": body.get("plate"),
            "did": body.get("driver_id"), "desc": body.get("description"),
            "by": body.get("reported_by"), "src": str(body.get("source") or "MANUAL").upper(),
            "occurred": _parse_ts(body.get("occurred_at")),
        },
        dsn=dsn,
    )
    if not row:
        raise HTTPException(500, "insert_failed")
    aid = int(row["id"])
    ref = f"ACC-{aid:06d}"
    await execute("UPDATE core.accident SET accident_ref = :ref WHERE id = :id",
                  {"ref": ref, "id": aid}, dsn=dsn)
    row["accident_ref"] = ref
    await _timeline(dsn, aid, action="REPORTED", old=None, new="REPORTED",
                    note=body.get("description"), actor=body.get("reported_by"))

    # Mirror to the control-room alert stream (same core.alert contract other
    # consoles read) so the accident surfaces on Alerts/Command Center, then
    # broadcast a live WS frame.
    payload = {"type": "accident", "accident_id": aid, "accident_ref": ref,
               "severity": severity, "accident_type": a_type,
               "plate": row.get("plate"), "vehicle_id": row.get("vehicle_id"),
               "lat": row.get("lat"), "lon": row.get("lon"),
               "title": f"{severity} accident ({a_type.lower()})",
               "body": body.get("description") or "Accident reported"}
    try:
        await state.ws.broadcast("accident", payload)
    except Exception as exc:  # noqa: BLE001
        log.warning("accident_ws_failed", error=str(exc))
    # Durable alert row via the audit sink (best-effort).
    try:
        from .. import audit
        await audit.persist_alert_event({
            "kind": "accident", "severity": "critical" if severity in _ALERT_SEVERITY else "warning",
            "plate": row.get("plate"), "payload": payload,
        })
    except Exception as exc:  # noqa: BLE001
        log.debug("accident_alert_persist_failed", error=str(exc))
    # Push the assigned driver when severe (best-effort; no-op if no device).
    if severity in _ALERT_SEVERITY and row.get("vehicle_id"):
        try:
            await dispatch_alert(state, row.get("vehicle_id"), kind="accident",
                                 title=payload["title"], body=payload["body"],
                                 category="emergency", extra={"accident_ref": ref})
        except Exception as exc:  # noqa: BLE001
            log.debug("accident_dispatch_failed", error=str(exc))

    REQUESTS.labels("accidents", "ok").inc()
    return {"created": True, "accident": _iso(row)}


@router.get("")
async def list_accidents(status: Optional[str] = Query(default=None),
                         accident_type: Optional[str] = Query(default=None),
                         vehicle_id: Optional[str] = Query(default=None),
                         limit: int = Query(default=100, ge=1, le=1000),
                         state: GatewayState = Depends(get_state)) -> dict:
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"count": 0, "accidents": []}
    from jnpa_shared.db import fetch_all

    where: List[str] = []
    params: Dict[str, Any] = {"limit": limit}
    if status:
        where.append("status = :status")
        params["status"] = status.upper()
    if accident_type:
        where.append("accident_type = :atype")
        params["atype"] = accident_type.upper()
    if vehicle_id:
        where.append("vehicle_id = :vid")
        params["vid"] = vehicle_id
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = await fetch_all(
        f"SELECT * FROM core.accident {clause} ORDER BY occurred_at DESC LIMIT :limit",
        params, dsn=dsn)
    REQUESTS.labels("accidents", "ok").inc()
    return {"count": len(rows), "accidents": [_iso(dict(r)) for r in rows]}


@router.get("/dashboard")
async def accidents_dashboard(state: GatewayState = Depends(get_state)) -> dict:
    """KPI rollup for the dashboard card."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"total": 0, "open": 0, "by_status": {}, "by_severity": {}, "by_type": {}}
    from jnpa_shared.db import fetch_all, fetch_one

    total = await fetch_one("SELECT count(*) AS n FROM core.accident", {}, dsn=dsn)
    open_n = await fetch_one(
        "SELECT count(*) AS n FROM core.accident WHERE status IN ('REPORTED','INVESTIGATING')",
        {}, dsn=dsn)
    by_status = await fetch_all(
        "SELECT status, count(*) AS n FROM core.accident GROUP BY status", {}, dsn=dsn)
    by_sev = await fetch_all(
        "SELECT severity, count(*) AS n FROM core.accident GROUP BY severity", {}, dsn=dsn)
    by_type = await fetch_all(
        "SELECT accident_type, count(*) AS n FROM core.accident GROUP BY accident_type", {}, dsn=dsn)
    REQUESTS.labels("accidents", "ok").inc()
    return {
        "total": int(total["n"]) if total else 0,
        "open": int(open_n["n"]) if open_n else 0,
        "by_status": {r["status"]: int(r["n"]) for r in by_status},
        "by_severity": {r["severity"]: int(r["n"]) for r in by_sev},
        "by_type": {r["accident_type"]: int(r["n"]) for r in by_type},
    }


@router.get("/{accident_id}")
async def get_accident(accident_id: int, state: GatewayState = Depends(get_state)) -> dict:
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import fetch_all, fetch_one

    row = await fetch_one("SELECT * FROM core.accident WHERE id = :id",
                          {"id": accident_id}, dsn=dsn)
    if not row:
        raise HTTPException(404, "accident_not_found")
    events = await fetch_all(
        "SELECT * FROM core.accident_event WHERE accident_id = :id ORDER BY created_at",
        {"id": accident_id}, dsn=dsn)
    return {"accident": _iso(dict(row)),
            "timeline": [_iso(dict(e)) for e in events]}


async def _load(dsn: str, accident_id: int) -> Dict[str, Any]:
    from jnpa_shared.db import fetch_one
    row = await fetch_one("SELECT * FROM core.accident WHERE id = :id",
                          {"id": accident_id}, dsn=dsn)
    if not row:
        raise HTTPException(404, "accident_not_found")
    return dict(row)


@router.post("/{accident_id}/status")
async def set_status(accident_id: int, body: Dict[str, Any] = Body(...),
                     state: GatewayState = Depends(get_state)) -> dict:
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute
    new = str(body.get("status") or "").upper()
    if new not in _STATUS:
        raise HTTPException(400, f"status must be one of {sorted(_STATUS)}")
    cur = await _load(dsn, accident_id)
    old = cur["status"]
    await execute("UPDATE core.accident SET status = :s, updated_at = now() WHERE id = :id",
                  {"s": new, "id": accident_id}, dsn=dsn)
    await _timeline(dsn, accident_id, action="STATUS_CHANGE", old=old, new=new,
                    note=body.get("note"), actor=body.get("actor"))
    try:
        await state.ws.broadcast("accident", {"type": "accident_update", "accident_id": accident_id,
                                              "status": new})
    except Exception:  # noqa: BLE001
        pass
    return {"updated": True, "accident_id": accident_id, "status": new}


@router.post("/{accident_id}/investigation")
async def set_investigation(accident_id: int, body: Dict[str, Any] = Body(...),
                            state: GatewayState = Depends(get_state)) -> dict:
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute
    new = str(body.get("investigation_status") or "").upper()
    if new not in _INVESTIGATION:
        raise HTTPException(400, f"investigation_status must be one of {sorted(_INVESTIGATION)}")
    cur = await _load(dsn, accident_id)
    # Setting investigation IN_PROGRESS also advances the case into INVESTIGATING.
    advance = cur["status"] == "REPORTED" and new in {"IN_PROGRESS", "COMPLETED"}
    await execute(
        """UPDATE core.accident
           SET investigation_status = :inv,
               status = CASE WHEN :advance THEN 'INVESTIGATING' ELSE status END,
               updated_at = now()
           WHERE id = :id""",
        {"inv": new, "advance": advance, "id": accident_id}, dsn=dsn)
    await _timeline(dsn, accident_id, action="INVESTIGATION",
                    old=cur["investigation_status"], new=new,
                    note=body.get("note"), actor=body.get("actor"))
    return {"updated": True, "accident_id": accident_id, "investigation_status": new}


@router.post("/{accident_id}/resolve")
async def resolve_accident(accident_id: int, body: Dict[str, Any] = Body(...),
                           state: GatewayState = Depends(get_state)) -> dict:
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute
    cur = await _load(dsn, accident_id)
    resolution = body.get("resolution") or "Resolved"
    await execute(
        """UPDATE core.accident
           SET status = 'RESOLVED', investigation_status = 'COMPLETED',
               resolution = :res, updated_at = now()
           WHERE id = :id""",
        {"res": resolution, "id": accident_id}, dsn=dsn)
    await _timeline(dsn, accident_id, action="RESOLVED", old=cur["status"], new="RESOLVED",
                    note=resolution, actor=body.get("actor"))
    try:
        await state.ws.broadcast("accident", {"type": "accident_update", "accident_id": accident_id,
                                              "status": "RESOLVED"})
    except Exception:  # noqa: BLE001
        pass
    return {"resolved": True, "accident_id": accident_id}


@router.post("/{accident_id}/note")
async def add_note(accident_id: int, body: Dict[str, Any] = Body(...),
                   state: GatewayState = Depends(get_state)) -> dict:
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    await _load(dsn, accident_id)
    note = body.get("note")
    if not note:
        raise HTTPException(400, "note required")
    await _timeline(dsn, accident_id, action="NOTE", old=None, new=None,
                    note=note, actor=body.get("actor"))
    return {"added": True, "accident_id": accident_id}
