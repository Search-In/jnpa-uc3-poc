"""/api/reefer — Reefer (refrigerated container) slot availability (Feature 11).

Powered reefer slots inside a facility (default the CPP reefer yard, ``PK-CPP``):
occupancy tracking, a per-facility availability rollup, and allocate/release of a
powered slot to a container. RDS-backed (jnpa.reefer_slots, migration 0024) —
occupancy is computed from real slot state, never synthesised. Additive: no
existing endpoint/table is touched.

    GET  /api/reefer/slots         -> list slots (filter: facility_id, status)
    GET  /api/reefer/availability  -> per-facility occupancy rollup + totals
    POST /api/reefer/allocate      -> claim first free powered slot for a container
    POST /api/reefer/release       -> free a slot (by slot_code or container_number)
    POST /api/reefer/seed          -> provision demo slots (idempotent)

If the database is unavailable, reads return the empty contract and writes raise
503 so the console shows an explicit error state instead of fabricated numbers.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.reefer")

router = APIRouter(prefix="/api/reefer", tags=["reefer"])

_DEFAULT_FACILITY = "PK-CPP"
_STATUS = {"AVAILABLE", "OCCUPIED", "RESERVED", "FAULT"}


def _iso(row: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize datetimes (updated_at) to ISO strings for JSON."""
    for k, v in list(row.items()):
        if hasattr(v, "isoformat"):
            row[k] = v.isoformat()
    return row


@router.get("/slots")
async def list_slots(facility_id: Optional[str] = Query(default=None),
                     status: Optional[str] = Query(default=None),
                     state: GatewayState = Depends(get_state)) -> dict:
    """List reefer slots, optionally filtered by facility_id / status."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"count": 0, "slots": []}
    from jnpa_shared.db import fetch_all

    where: List[str] = []
    params: Dict[str, Any] = {}
    if facility_id:
        where.append("facility_id = :fid")
        params["fid"] = facility_id
    if status:
        where.append("status = :status")
        params["status"] = status.upper()
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = await fetch_all(
        f"SELECT * FROM jnpa.reefer_slots {clause} ORDER BY facility_id, slot_code",
        params, dsn=dsn)
    REQUESTS.labels("reefer", "ok").inc()
    return {"count": len(rows), "slots": [_iso(dict(r)) for r in rows]}


@router.get("/availability")
async def availability(state: GatewayState = Depends(get_state)) -> dict:
    """Per-facility occupancy rollup (total/available/occupied/reserved/fault/
    powered_available + free_pct), plus grand totals. Computed in SQL — never
    synthesises occupancy."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"facilities": [], "totals": {}}
    from jnpa_shared.db import fetch_all

    rows = await fetch_all(
        """
        SELECT facility_id,
               count(*)                                                        AS total,
               count(*) FILTER (WHERE status = 'AVAILABLE')                    AS available,
               count(*) FILTER (WHERE status = 'OCCUPIED')                     AS occupied,
               count(*) FILTER (WHERE status = 'RESERVED')                     AS reserved,
               count(*) FILTER (WHERE status = 'FAULT')                        AS fault,
               count(*) FILTER (WHERE status = 'AVAILABLE' AND powered)        AS powered_available
        FROM jnpa.reefer_slots
        GROUP BY facility_id
        ORDER BY facility_id
        """,
        {}, dsn=dsn)

    facilities: List[dict] = []
    totals = {"total": 0, "available": 0, "occupied": 0, "reserved": 0,
              "fault": 0, "powered_available": 0}
    for r in rows:
        d = dict(r)
        total = int(d.get("total") or 0)
        avail = int(d.get("available") or 0)
        facilities.append({
            "facility_id": d["facility_id"],
            "total": total,
            "available": avail,
            "occupied": int(d.get("occupied") or 0),
            "reserved": int(d.get("reserved") or 0),
            "fault": int(d.get("fault") or 0),
            "powered_available": int(d.get("powered_available") or 0),
            "free_pct": round(100.0 * avail / total, 1) if total else 0.0,
        })
        for k in totals:
            totals[k] += int(d.get(k) or 0)
    totals["free_pct"] = (round(100.0 * totals["available"] / totals["total"], 1)
                          if totals["total"] else 0.0)
    REQUESTS.labels("reefer", "ok").inc()
    return {"facilities": facilities, "totals": totals}


@router.post("/allocate")
async def allocate(body: Dict[str, Any] = Body(...),
                   state: GatewayState = Depends(get_state)) -> dict:
    """Claim the first AVAILABLE & powered slot in a facility for a container.
    Body: {facility_id?, container_number, set_temperature?}. Uses an atomic
    UPDATE ... WHERE id = (SELECT ... LIMIT 1) to avoid double-allocation races."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute_returning

    facility_id = str(body.get("facility_id") or _DEFAULT_FACILITY)
    container_number = body.get("container_number")
    if not container_number:
        raise HTTPException(400, "container_number required")
    set_temp = body.get("set_temperature")

    row = await execute_returning(
        """
        UPDATE jnpa.reefer_slots
           SET status = 'OCCUPIED',
               container_number = :cn,
               set_temperature = :st,
               updated_at = now()
         WHERE id = (
               SELECT id FROM jnpa.reefer_slots
                WHERE facility_id = :fid AND status = 'AVAILABLE' AND powered
                ORDER BY slot_code
                LIMIT 1
                FOR UPDATE SKIP LOCKED
         )
        RETURNING *
        """,
        {"cn": container_number, "st": set_temp, "fid": facility_id},
        dsn=dsn)
    if not row:
        REQUESTS.labels("reefer", "ok").inc()
        return {"allocated": False, "reason": "no_slot"}
    REQUESTS.labels("reefer", "ok").inc()
    return {"allocated": True, "slot": _iso(dict(row))}


@router.post("/release")
async def release(body: Dict[str, Any] = Body(...),
                  state: GatewayState = Depends(get_state)) -> dict:
    """Free a slot back to AVAILABLE, clearing its container/set_temperature.
    Body: {slot_code | container_number, facility_id?}."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute_returning

    slot_code = body.get("slot_code")
    container_number = body.get("container_number")
    if not slot_code and not container_number:
        raise HTTPException(400, "slot_code or container_number required")

    where: List[str] = []
    params: Dict[str, Any] = {}
    if slot_code:
        where.append("slot_code = :sc")
        params["sc"] = slot_code
    if container_number:
        where.append("container_number = :cn")
        params["cn"] = container_number
    if body.get("facility_id"):
        where.append("facility_id = :fid")
        params["fid"] = body["facility_id"]
    clause = " AND ".join(where)

    row = await execute_returning(
        f"""
        UPDATE jnpa.reefer_slots
           SET status = 'AVAILABLE',
               container_number = NULL,
               set_temperature = NULL,
               updated_at = now()
         WHERE {clause}
        RETURNING id
        """,
        params, dsn=dsn)
    REQUESTS.labels("reefer", "ok").inc()
    return {"released": bool(row)}


@router.post("/seed")
async def seed(body: Dict[str, Any] = Body(default={}),
               state: GatewayState = Depends(get_state)) -> dict:
    """Provision demo reefer slots (idempotent). Body: {facility_id?, count?=24,
    powered_ratio?=0.8}. Inserts REEFER-A01..A{count} ON CONFLICT DO NOTHING;
    the first ceil(count*ratio) are powered, the rest unpowered."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute

    facility_id = str(body.get("facility_id") or _DEFAULT_FACILITY)
    count = int(body.get("count") or 24)
    if count < 1 or count > 1000:
        raise HTTPException(400, "count must be between 1 and 1000")
    ratio = float(body.get("powered_ratio", 0.8))
    ratio = min(1.0, max(0.0, ratio))
    powered_n = math.ceil(count * ratio)

    seeded = 0
    for i in range(1, count + 1):
        slot_code = f"REEFER-A{i:02d}"
        await execute(
            """INSERT INTO jnpa.reefer_slots (facility_id, slot_code, powered, status)
               VALUES (:fid, :sc, :pw, 'AVAILABLE')
               ON CONFLICT (facility_id, slot_code) DO NOTHING""",
            {"fid": facility_id, "sc": slot_code, "pw": i <= powered_n},
            dsn=dsn)
        seeded += 1
    REQUESTS.labels("reefer", "ok").inc()
    return {"seeded": seeded, "facility_id": facility_id}
