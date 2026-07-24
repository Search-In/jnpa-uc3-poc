"""/api/rms-tas — RMS-TAS Terminal Appointment System (Feature 14).

The NEW persisted appointment surface for the RMS Terminal Appointment System.
Unlike the legacy in-memory ``/api/tas/*`` mock (gateway/tas_mock.py, left
untouched), every slot and booking here is durable in RDS
(core.tas_appointment / core.tas_booking, migration 0024 §14) so availability,
capacity and booking status survive a restart and are auditable.

LIVE-vs-MOCK posture goes through the shared integration seam
(:mod:`gateway.integrations`): ``/sync`` pulls real slots when ``RMS_TAS_BASE_URL``
is configured, otherwise a deterministic mock list — never a silent hardcode.

    GET  /api/rms-tas/slots                      -> appointment slots + availability
    POST /api/rms-tas/seed                       -> provision demo hourly windows
    POST /api/rms-tas/book                       -> book a slot (capacity-checked, tx)
    POST /api/rms-tas/booking/{booking_id}/status-> update a booking status
    GET  /api/rms-tas/booking/{booking_id}       -> one booking + its appointment
    GET  /api/rms-tas/bookings                   -> recent bookings (filter vehicle_id)
    POST /api/rms-tas/sync                        -> pull live slots (LIVE/MOCK) + upsert
    GET  /api/rms-tas/health                      -> integration health (LIVE/MOCK)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query

from .. import integrations
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.rms_tas")

router = APIRouter(prefix="/api/rms-tas", tags=["rms-tas"])

_BOOKING_STATUS = {"BOOKED", "CANCELLED", "COMPLETED", "NO_SHOW"}
_DEFAULT_GATE = "G-NSICT"


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
    """Serialize datetime columns to ISO strings (dict-in-place)."""
    for k, v in list(row.items()):
        if hasattr(v, "isoformat"):
            row[k] = v.isoformat()
    return row


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _parse_date(date_str: str) -> datetime:
    """Parse a YYYY-MM-DD string into a tz-aware (UTC) midnight datetime."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise HTTPException(400, "date must be YYYY-MM-DD")
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _slot_code(gate_id: str, date_str: str, hour: int) -> str:
    return f"{gate_id}-{date_str}-{hour:02d}00"


# --------------------------------------------------------------------- slots
@router.get("/slots")
async def list_slots(gate_id: Optional[str] = Query(default=None),
                     date: Optional[str] = Query(default=None),
                     limit: int = Query(default=100, ge=1, le=1000),
                     state: GatewayState = Depends(get_state)) -> dict:
    """List appointment slots with availability (available = capacity - booked)."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"count": 0, "slots": []}
    from jnpa_shared.db import fetch_all

    where: List[str] = []
    params: Dict[str, Any] = {"limit": limit}
    if gate_id:
        where.append("gate_id = :gate_id")
        params["gate_id"] = gate_id
    if date:
        where.append("window_start::date = CAST(:d AS date)")
        params["d"] = date
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = await fetch_all(
        f"""SELECT slot_code, gate_id, window_start, window_end,
                   capacity, booked, (capacity - booked) AS available, status
              FROM core.tas_appointment {clause}
          ORDER BY window_start LIMIT :limit""",
        params, dsn=dsn)
    REQUESTS.labels("rms_tas", "ok").inc()
    return {"count": len(rows), "slots": [_iso(dict(r)) for r in rows]}


@router.post("/seed")
async def seed_slots(body: Dict[str, Any] = Body(default={}),
                     state: GatewayState = Depends(get_state)) -> dict:
    """Provision demo appointments: hourly windows 09:00..(09+slots_per_day):00
    for a date. Body: {gate_id?, date?, slots_per_day?, capacity?}."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute

    gate_id = str(body.get("gate_id") or _DEFAULT_GATE)
    date_str = str(body.get("date") or _today_utc())
    base = _parse_date(date_str)
    try:
        slots_per_day = int(body.get("slots_per_day") or 8)
        capacity = int(body.get("capacity") or 10)
    except (TypeError, ValueError):
        raise HTTPException(400, "slots_per_day and capacity must be integers")
    if slots_per_day < 1 or slots_per_day > 24:
        raise HTTPException(400, "slots_per_day must be between 1 and 24")

    seeded = 0
    for h in range(9, 9 + slots_per_day):
        window_start = base.replace(hour=h % 24) + timedelta(days=h // 24)
        window_end = window_start + timedelta(hours=1)
        n = await execute(
            """INSERT INTO core.tas_appointment
                   (slot_code, gate_id, window_start, window_end, capacity, booked, status, source)
               VALUES (:sc, :gate, CAST(:ws AS timestamptz), CAST(:we AS timestamptz),
                       :cap, 0, 'OPEN', 'LOCAL')
               ON CONFLICT (slot_code) DO NOTHING""",
            {"sc": _slot_code(gate_id, date_str, h), "gate": gate_id,
             "ws": window_start, "we": window_end,
             "cap": capacity},
            dsn=dsn)
        seeded += int(n or 0)
    REQUESTS.labels("rms_tas", "ok").inc()
    return {"seeded": seeded, "gate_id": gate_id, "date": date_str}


# --------------------------------------------------------------------- booking
@router.post("/book")
async def book_slot(body: Dict[str, Any] = Body(...),
                    state: GatewayState = Depends(get_state)) -> dict:
    """Book an appointment slot. Body: {slot_code, vehicle_id, driver_id?}.
    Transactional + row-locked: rejects when the slot is full/closed/missing."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    slot_code = (body.get("slot_code") or "").strip()
    vehicle_id = (body.get("vehicle_id") or "").strip()
    driver_id = body.get("driver_id")
    if not slot_code:
        raise HTTPException(400, "slot_code required")
    if not vehicle_id:
        raise HTTPException(400, "vehicle_id required")

    from sqlalchemy import text
    from jnpa_shared.db import get_engine
    engine = get_engine(dsn)
    async with engine.begin() as conn:
        appt = (await conn.execute(
            text("""SELECT id, capacity, booked, status
                      FROM core.tas_appointment
                     WHERE slot_code = :sc FOR UPDATE"""),
            {"sc": slot_code})).mappings().first()
        if not appt:
            return {"booked": False, "reason": "slot_not_found"}
        if appt["status"] == "CLOSED":
            return {"booked": False, "reason": "slot_closed"}
        if int(appt["booked"]) >= int(appt["capacity"]):
            return {"booked": False, "reason": "slot_full"}

        booking = (await conn.execute(
            text("""INSERT INTO core.tas_booking
                        (appointment_id, slot_code, vehicle_id, driver_id, status)
                    VALUES (:aid, :sc, :vid, :did, 'BOOKED')
                    RETURNING id"""),
            {"aid": appt["id"], "sc": slot_code, "vid": vehicle_id,
             "did": driver_id})).mappings().first()
        new_booked = int(appt["booked"]) + 1
        new_status = "FULL" if new_booked >= int(appt["capacity"]) else "OPEN"
        await conn.execute(
            text("""UPDATE core.tas_appointment
                       SET booked = :b, status = :s, updated_at = now()
                     WHERE id = :id"""),
            {"b": new_booked, "s": new_status, "id": appt["id"]})
        booking_id = int(booking["id"])

    # Best-effort live WS frame (a socket outage never fails the booking).
    try:
        await state.ws.broadcast("tas", {"type": "tas_booking", "booking_id": booking_id,
                                         "slot_code": slot_code, "vehicle_id": vehicle_id,
                                         "status": new_status})
    except Exception as exc:  # noqa: BLE001
        log.debug("tas_ws_failed", error=str(exc))

    REQUESTS.labels("rms_tas", "ok").inc()
    return {"booked": True, "booking_id": booking_id, "slot_code": slot_code}


@router.post("/booking/{booking_id}/status")
async def set_booking_status(booking_id: int = Path(...), body: Dict[str, Any] = Body(...),
                             state: GatewayState = Depends(get_state)) -> dict:
    """Update a booking status. CANCELLED frees a seat (decrements the
    appointment.booked and reopens a FULL slot); COMPLETED/NO_SHOW just update."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    new = str(body.get("status") or "").upper()
    if new not in _BOOKING_STATUS:
        raise HTTPException(400, f"status must be one of {sorted(_BOOKING_STATUS)}")

    from sqlalchemy import text
    from jnpa_shared.db import get_engine
    engine = get_engine(dsn)
    async with engine.begin() as conn:
        booking = (await conn.execute(
            text("SELECT * FROM core.tas_booking WHERE id = :id FOR UPDATE"),
            {"id": booking_id})).mappings().first()
        if not booking:
            raise HTTPException(404, "booking_not_found")
        old = booking["status"]
        # Cancelling an active booking frees a seat on its appointment.
        if new == "CANCELLED" and old != "CANCELLED":
            await conn.execute(
                text("""UPDATE core.tas_appointment
                           SET booked = GREATEST(booked - 1, 0),
                               status = CASE WHEN status = 'FULL' THEN 'OPEN' ELSE status END,
                               updated_at = now()
                         WHERE id = :aid"""),
                {"aid": booking["appointment_id"]})
        await conn.execute(
            text("UPDATE core.tas_booking SET status = :s, updated_at = now() WHERE id = :id"),
            {"s": new, "id": booking_id})
        updated = (await conn.execute(
            text("SELECT * FROM core.tas_booking WHERE id = :id"),
            {"id": booking_id})).mappings().first()

    REQUESTS.labels("rms_tas", "ok").inc()
    return {"updated": True, "booking_id": booking_id, "old_status": old,
            "booking": _iso(dict(updated))}


@router.get("/booking/{booking_id}")
async def get_booking(booking_id: int = Path(...),
                      state: GatewayState = Depends(get_state)) -> dict:
    """One booking joined with its appointment."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import fetch_one
    booking = await fetch_one("SELECT * FROM core.tas_booking WHERE id = :id",
                              {"id": booking_id}, dsn=dsn)
    if not booking:
        raise HTTPException(404, "booking_not_found")
    appt = await fetch_one("SELECT * FROM core.tas_appointment WHERE id = :id",
                           {"id": booking["appointment_id"]}, dsn=dsn)
    REQUESTS.labels("rms_tas", "ok").inc()
    return {"booking": _iso(dict(booking)),
            "appointment": _iso(dict(appt)) if appt else None}


@router.get("/bookings")
async def list_bookings(vehicle_id: Optional[str] = Query(default=None),
                        limit: int = Query(default=100, ge=1, le=1000),
                        state: GatewayState = Depends(get_state)) -> dict:
    """Recent bookings (optionally filtered by vehicle_id)."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"count": 0, "bookings": []}
    from jnpa_shared.db import fetch_all
    where = ""
    params: Dict[str, Any] = {"limit": limit}
    if vehicle_id:
        where = "WHERE vehicle_id = :vid"
        params["vid"] = vehicle_id
    rows = await fetch_all(
        f"SELECT * FROM core.tas_booking {where} ORDER BY booked_at DESC LIMIT :limit",
        params, dsn=dsn)
    REQUESTS.labels("rms_tas", "ok").inc()
    return {"count": len(rows), "bookings": [_iso(dict(r)) for r in rows]}


# ------------------------------------------------------------------ live sync
def _mock_slots(gate_id: str) -> Dict[str, Any]:
    """Deterministic slot list for the RMS-TAS mock branch: hourly windows
    09:00..17:00 today for the gate."""
    base = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    date_str = base.date().isoformat()
    slots = []
    for h in range(9, 17):
        ws = base.replace(hour=h)
        slots.append({
            "slot_code": _slot_code(gate_id, date_str, h),
            "gate_id": gate_id,
            "window_start": ws.isoformat(),
            "window_end": (ws + timedelta(hours=1)).isoformat(),
            "capacity": 10,
        })
    return {"gate_id": gate_id, "slots": slots}


@router.post("/sync")
async def sync_slots(body: Dict[str, Any] = Body(default={}),
                     state: GatewayState = Depends(get_state)) -> dict:
    """Pull live appointment slots through the integration seam and upsert them.
    LIVE when RMS_TAS_BASE_URL is configured, otherwise a deterministic MOCK."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    gate_id = str(body.get("gate_id") or _DEFAULT_GATE)

    result = await integrations.call(
        system="RMS_TAS", op="slots", ref=gate_id,
        request={"gate_id": gate_id},
        live_path="/slots",
        mock_fn=lambda: _mock_slots(gate_id),
        dsn=dsn,
    )
    source = result["source"]
    data = result["data"]
    slots = data.get("slots") if isinstance(data, dict) else data
    if not isinstance(slots, list):
        slots = []

    from jnpa_shared.db import execute
    synced = 0
    for s in slots:
        if not isinstance(s, dict):
            continue
        slot_code = s.get("slot_code")
        window_start = s.get("window_start")
        window_end = s.get("window_end")
        if not slot_code or not window_start or not window_end:
            continue
        await execute(
            """INSERT INTO core.tas_appointment
                   (slot_code, gate_id, window_start, window_end, capacity, booked, status, source)
               VALUES (:sc, :gate, CAST(:ws AS timestamptz), CAST(:we AS timestamptz),
                       COALESCE(:cap, 10), 0, 'OPEN', :src)
               ON CONFLICT (slot_code) DO UPDATE SET
                   gate_id = EXCLUDED.gate_id,
                   window_start = EXCLUDED.window_start,
                   window_end = EXCLUDED.window_end,
                   capacity = EXCLUDED.capacity,
                   source = EXCLUDED.source,
                   updated_at = now()""",
            {"sc": slot_code, "gate": s.get("gate_id") or gate_id,
             "ws": _parse_ts(window_start), "we": _parse_ts(window_end),
             "cap": s.get("capacity"), "src": source},
            dsn=dsn)
        synced += 1
    REQUESTS.labels("rms_tas", "ok").inc()
    return {"source": source, "synced": synced}


@router.get("/health")
async def rms_tas_health() -> dict:
    """LIVE-vs-MOCK posture for the RMS-TAS dependency."""
    return integrations.health("RMS_TAS")


__all__ = ["router"]
