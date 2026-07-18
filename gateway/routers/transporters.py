"""/api/transporters — Transporter registry + blacklist (UC-III completion, Feature 2).

A first-class transporter entity (the audit found only a per-vehicle
vehicle_master.blacklist_status column with no enforcement). Adds:
  * transporter master + vehicle/driver mapping,
  * blacklist lifecycle (blacklist -> lift) with reason/severity + timeline,
  * vehicle & driver VALIDATION endpoints the gate/enforcement flow can call,
  * search, and a control-room notification on blacklist.

RDS-backed (jnpa.transporters / transporter_vehicles / transporter_blacklist).
Additive — the legacy vehicle_master.blacklist_status column is left untouched;
blacklisting a transporter ALSO stamps its mapped vehicles' vehicle_master row
when present (best-effort) so existing read paths stay consistent.

    POST /api/transporters                       -> create transporter
    GET  /api/transporters?q=&status=&limit=      -> list/search
    GET  /api/transporters/blacklist              -> active blacklist
    GET  /api/transporters/{id}                   -> one + vehicles + blacklist history
    POST /api/transporters/{id}/vehicles          -> map a vehicle (+optional driver)
    POST /api/transporters/{id}/blacklist         -> blacklist (reason, severity)
    POST /api/transporters/{id}/lift              -> lift active blacklist
    GET  /api/transporters/validate/vehicle/{plate}
    GET  /api/transporters/validate/driver/{driver_id}
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..logging import get_logger
from ..metrics import REQUESTS
from ..notifications import dispatch_alert
from ..state import GatewayState, get_state

log = get_logger("gateway.transporters")

router = APIRouter(prefix="/api/transporters", tags=["transporters"])

_SEVERITY = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


def _norm_plate(plate: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (plate or "").upper())


def _iso(row: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in list(row.items()):
        if hasattr(v, "isoformat"):
            row[k] = v.isoformat()
        elif isinstance(v, str) and k in ("contact",):
            try:
                row[k] = json.loads(v)
            except Exception:  # noqa: BLE001
                pass
    return row


# --- create / list -----------------------------------------------------------
@router.post("")
async def create_transporter(body: Dict[str, Any] = Body(...),
                             state: GatewayState = Depends(get_state)) -> dict:
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute_returning
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    # Backward compatible: legacy callers send only code/name/gstin/contact/status.
    # Transport Master callers may additionally send the extended fields below;
    # all are optional and default to NULL so nothing existing breaks.
    row = await execute_returning(
        """INSERT INTO jnpa.transporters
             (code, name, gstin, contact, status, source_company_id, source_user_id,
              contact_person, designation, email, mobile, address, doc_type, doc_file)
           VALUES (:code, :name, :gstin, CAST(:contact AS jsonb),
                   COALESCE(:status, 'ACTIVE'), :source_company_id, :source_user_id,
                   :contact_person, :designation, :email, :mobile, :address,
                   :doc_type, :doc_file)
           RETURNING *""",
        {"code": body.get("code"), "name": name, "gstin": body.get("gstin"),
         "contact": json.dumps(body.get("contact") or {}),
         "status": (body.get("status") or "ACTIVE"),
         "source_company_id": body.get("source_company_id"),
         "source_user_id": body.get("source_user_id"),
         "contact_person": body.get("contact_person"),
         "designation": body.get("designation"),
         "email": body.get("email"), "mobile": body.get("mobile"),
         "address": body.get("address"), "doc_type": body.get("doc_type"),
         "doc_file": body.get("doc_file")},
        dsn=dsn)
    REQUESTS.labels("transporters", "ok").inc()
    return {"created": True, "transporter": _iso(dict(row)) if row else None}


@router.get("")
async def list_transporters(q: Optional[str] = Query(default=None),
                            status: Optional[str] = Query(default=None),
                            limit: int = Query(default=100, ge=1, le=1000),
                            state: GatewayState = Depends(get_state)) -> dict:
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"count": 0, "transporters": []}
    from jnpa_shared.db import fetch_all
    where: List[str] = []
    params: Dict[str, Any] = {"limit": limit}
    if q:
        where.append(
            "(lower(name) LIKE :q OR lower(coalesce(code,'')) LIKE :q "
            "OR lower(coalesce(gstin,'')) LIKE :q "
            "OR lower(coalesce(contact_person,'')) LIKE :q "
            "OR lower(coalesce(email,'')) LIKE :q "
            "OR coalesce(mobile,'') LIKE :q "
            "OR coalesce(source_company_id::text,'') LIKE :q)")
        params["q"] = f"%{q.lower()}%"
    if status:
        where.append("status = :status")
        params["status"] = status.upper()
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = await fetch_all(
        f"""SELECT t.*,
              (SELECT count(*) FROM jnpa.transporter_vehicles v WHERE v.transporter_id = t.id) AS vehicle_count,
              EXISTS(SELECT 1 FROM jnpa.transporter_blacklist b
                     WHERE b.transporter_id = t.id AND b.status = 'ACTIVE') AS blacklisted
            FROM jnpa.transporters t {clause}
            ORDER BY t.created_at DESC LIMIT :limit""",
        params, dsn=dsn)
    REQUESTS.labels("transporters", "ok").inc()
    return {"count": len(rows), "transporters": [_iso(dict(r)) for r in rows]}


@router.get("/blacklist")
async def active_blacklist(limit: int = Query(default=200, ge=1, le=1000),
                           state: GatewayState = Depends(get_state)) -> dict:
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"count": 0, "blacklist": []}
    from jnpa_shared.db import fetch_all
    rows = await fetch_all(
        """SELECT b.*, t.name AS transporter_name, t.code AS transporter_code
           FROM jnpa.transporter_blacklist b
           JOIN jnpa.transporters t ON t.id = b.transporter_id
           WHERE b.status = 'ACTIVE'
           ORDER BY b.blacklisted_at DESC LIMIT :limit""",
        {"limit": limit}, dsn=dsn)
    return {"count": len(rows), "blacklist": [_iso(dict(r)) for r in rows]}


@router.get("/validate/vehicle/{plate}")
async def validate_vehicle(plate: str, state: GatewayState = Depends(get_state)) -> dict:
    """Is this vehicle operated by a currently-blacklisted transporter?
    The gate/enforcement flow calls this to DENY entry (the missing enforcement
    action the audit flagged)."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"plate": plate, "blacklisted": False, "reason": None, "decision": "ALLOW",
                "source": "unavailable"}
    from jnpa_shared.db import fetch_one
    norm = _norm_plate(plate)
    row = await fetch_one(
        """SELECT t.id AS transporter_id, t.name AS transporter_name,
                  b.reason, b.severity, b.blacklisted_at
           FROM jnpa.transporter_vehicles v
           JOIN jnpa.transporters t ON t.id = v.transporter_id
           JOIN jnpa.transporter_blacklist b
             ON b.transporter_id = t.id AND b.status = 'ACTIVE'
           WHERE v.vehicle_no_norm = :norm
           ORDER BY b.blacklisted_at DESC LIMIT 1""",
        {"norm": norm}, dsn=dsn)
    REQUESTS.labels("transporters", "ok").inc()
    if row:
        return {"plate": plate, "blacklisted": True, "decision": "DENY",
                "transporter_id": int(row["transporter_id"]),
                "transporter_name": row["transporter_name"],
                "reason": row["reason"], "severity": row["severity"],
                "blacklisted_at": row["blacklisted_at"].isoformat()
                if hasattr(row["blacklisted_at"], "isoformat") else row["blacklisted_at"],
                "source": "rds"}
    return {"plate": plate, "blacklisted": False, "decision": "ALLOW", "reason": None, "source": "rds"}


@router.get("/validate/driver/{driver_id}")
async def validate_driver(driver_id: str, state: GatewayState = Depends(get_state)) -> dict:
    """Is this driver mapped to a currently-blacklisted transporter?"""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"driver_id": driver_id, "blacklisted": False, "decision": "ALLOW", "source": "unavailable"}
    from jnpa_shared.db import fetch_one
    row = await fetch_one(
        """SELECT t.id AS transporter_id, t.name AS transporter_name, b.reason, b.severity
           FROM jnpa.transporter_vehicles v
           JOIN jnpa.transporters t ON t.id = v.transporter_id
           JOIN jnpa.transporter_blacklist b
             ON b.transporter_id = t.id AND b.status = 'ACTIVE'
           WHERE v.driver_id = :did
           ORDER BY b.blacklisted_at DESC LIMIT 1""",
        {"did": driver_id}, dsn=dsn)
    if row:
        return {"driver_id": driver_id, "blacklisted": True, "decision": "DENY",
                "transporter_id": int(row["transporter_id"]),
                "transporter_name": row["transporter_name"],
                "reason": row["reason"], "severity": row["severity"], "source": "rds"}
    return {"driver_id": driver_id, "blacklisted": False, "decision": "ALLOW", "source": "rds"}


@router.get("/{transporter_id}")
async def get_transporter(transporter_id: int, state: GatewayState = Depends(get_state)) -> dict:
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import fetch_all, fetch_one
    row = await fetch_one("SELECT * FROM jnpa.transporters WHERE id = :id",
                          {"id": transporter_id}, dsn=dsn)
    if not row:
        raise HTTPException(404, "transporter_not_found")
    vehicles = await fetch_all(
        "SELECT * FROM jnpa.transporter_vehicles WHERE transporter_id = :id ORDER BY created_at",
        {"id": transporter_id}, dsn=dsn)
    blacklist = await fetch_all(
        "SELECT * FROM jnpa.transporter_blacklist WHERE transporter_id = :id ORDER BY blacklisted_at DESC",
        {"id": transporter_id}, dsn=dsn)
    return {"transporter": _iso(dict(row)),
            "vehicles": [_iso(dict(v)) for v in vehicles],
            "blacklist_history": [_iso(dict(b)) for b in blacklist]}


@router.post("/{transporter_id}/vehicles")
async def map_vehicle(transporter_id: int, body: Dict[str, Any] = Body(...),
                      state: GatewayState = Depends(get_state)) -> dict:
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute, fetch_one
    t = await fetch_one("SELECT id FROM jnpa.transporters WHERE id = :id",
                        {"id": transporter_id}, dsn=dsn)
    if not t:
        raise HTTPException(404, "transporter_not_found")
    vno = (body.get("vehicle_no") or "").strip()
    if not vno:
        raise HTTPException(400, "vehicle_no required")
    await execute(
        """INSERT INTO jnpa.transporter_vehicles (transporter_id, vehicle_no, vehicle_no_norm, driver_id)
           VALUES (:tid, :vno, :norm, :did)
           ON CONFLICT (transporter_id, vehicle_no_norm)
           DO UPDATE SET driver_id = COALESCE(EXCLUDED.driver_id, jnpa.transporter_vehicles.driver_id)""",
        {"tid": transporter_id, "vno": vno, "norm": _norm_plate(vno), "did": body.get("driver_id")},
        dsn=dsn)
    return {"mapped": True, "transporter_id": transporter_id, "vehicle_no": vno}


@router.post("/{transporter_id}/blacklist")
async def blacklist(transporter_id: int, body: Dict[str, Any] = Body(...),
                    state: GatewayState = Depends(get_state)) -> dict:
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute, execute_returning, fetch_all, fetch_one
    t = await fetch_one("SELECT * FROM jnpa.transporters WHERE id = :id",
                        {"id": transporter_id}, dsn=dsn)
    if not t:
        raise HTTPException(404, "transporter_not_found")
    reason = (body.get("reason") or "").strip()
    if not reason:
        raise HTTPException(400, "reason required")
    severity = str(body.get("severity") or "HIGH").upper()
    if severity not in _SEVERITY:
        raise HTTPException(400, f"severity must be one of {sorted(_SEVERITY)}")
    # Close any existing ACTIVE blacklist first (single active record invariant).
    await execute(
        "UPDATE jnpa.transporter_blacklist SET status = 'LIFTED', lifted_at = now() WHERE transporter_id = :id AND status = 'ACTIVE'",
        {"id": transporter_id}, dsn=dsn)
    bl = await execute_returning(
        """INSERT INTO jnpa.transporter_blacklist (transporter_id, reason, severity, blacklisted_by)
           VALUES (:id, :reason, :sev, :by) RETURNING *""",
        {"id": transporter_id, "reason": reason, "sev": severity, "by": body.get("actor")},
        dsn=dsn)
    await execute("UPDATE jnpa.transporters SET status = 'BLACKLISTED', updated_at = now() WHERE id = :id",
                  {"id": transporter_id}, dsn=dsn)
    # Best-effort: stamp mapped vehicles' legacy vehicle_master.blacklist_status so
    # existing read paths (reports, vehicle-intel) stay consistent. Never fails hard.
    try:
        await execute(
            """UPDATE jnpa.vehicle_master vm SET blacklist_status = 'BLACKLISTED'
               FROM jnpa.transporter_vehicles v
               WHERE v.transporter_id = :id
                 AND regexp_replace(upper(vm.plate), '[^A-Z0-9]', '', 'g') = v.vehicle_no_norm""",
            {"id": transporter_id}, dsn=dsn)
    except Exception as exc:  # noqa: BLE001 - vehicle_master column optional
        log.debug("vehicle_master_stamp_skipped", error=str(exc))

    payload = {"type": "transporter_blacklist", "transporter_id": transporter_id,
               "transporter_name": t["name"], "reason": reason, "severity": severity,
               "title": f"Transporter blacklisted: {t['name']}", "body": reason}
    try:
        await state.ws.broadcast("alert", payload)
    except Exception:  # noqa: BLE001
        pass
    try:
        from .. import audit
        await audit.persist_alert_event({"kind": "transporter_blacklist",
                                         "severity": "warning", "payload": payload})
    except Exception as exc:  # noqa: BLE001
        log.debug("blacklist_alert_persist_failed", error=str(exc))
    REQUESTS.labels("transporters", "ok").inc()
    return {"blacklisted": True, "transporter_id": transporter_id,
            "record": _iso(dict(bl)) if bl else None}


@router.post("/{transporter_id}/lift")
async def lift_blacklist(transporter_id: int, body: Dict[str, Any] = Body(default={}),
                         state: GatewayState = Depends(get_state)) -> dict:
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute
    n = await execute(
        """UPDATE jnpa.transporter_blacklist
           SET status = 'LIFTED', lifted_at = now(), lifted_by = :by
           WHERE transporter_id = :id AND status = 'ACTIVE'""",
        {"id": transporter_id, "by": (body or {}).get("actor")}, dsn=dsn)
    if n:
        await execute("UPDATE jnpa.transporters SET status = 'ACTIVE', updated_at = now() WHERE id = :id",
                      {"id": transporter_id}, dsn=dsn)
        try:
            await execute(
                """UPDATE jnpa.vehicle_master vm SET blacklist_status = 'CLEAR'
                   FROM jnpa.transporter_vehicles v
                   WHERE v.transporter_id = :id
                     AND regexp_replace(upper(vm.plate), '[^A-Z0-9]', '', 'g') = v.vehicle_no_norm""",
                {"id": transporter_id}, dsn=dsn)
        except Exception:  # noqa: BLE001
            pass
    return {"lifted": bool(n), "transporter_id": transporter_id}
