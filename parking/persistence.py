"""RDS persistence for Parking Management (Phase 2 · Track 2).

Replaces the simulated sine-curve occupancy with a real inventory + slot state +
entry/exit transactions + a parking-event log, all in Postgres (single source of
truth). Reuses the existing audit-framework TABLES (jnpa.digital_twin_events /
jnpa.notifications) by writing to them directly — the audit framework CODE is not
modified.

Lives inside the bind-mounted parking service and builds on the installed
``jnpa_shared.db`` engine. Every writer is best-effort; the DDL is idempotent
(mirrors migration 0005) and applied at boot.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from jnpa_shared.logging import get_logger

log = get_logger("parking.persistence")

_DDL = """
CREATE SCHEMA IF NOT EXISTS jnpa;
CREATE TABLE IF NOT EXISTS jnpa.parking_facilities (
    id text PRIMARY KEY, facility_name text NOT NULL,
    location jsonb NOT NULL DEFAULT '{}'::jsonb, capacity integer NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'OPEN', created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS jnpa.parking_slots (
    id bigserial PRIMARY KEY,
    facility_id text NOT NULL REFERENCES jnpa.parking_facilities(id) ON DELETE CASCADE,
    slot_number text NOT NULL,
    availability_status text NOT NULL DEFAULT 'AVAILABLE'
        CHECK (availability_status IN ('AVAILABLE','OCCUPIED','RESERVED','OUT_OF_SERVICE')),
    vehicle_id text, updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (facility_id, slot_number));
CREATE INDEX IF NOT EXISTS idx_parking_slots_facility ON jnpa.parking_slots (facility_id, availability_status);
CREATE INDEX IF NOT EXISTS idx_parking_slots_vehicle ON jnpa.parking_slots (vehicle_id);
CREATE TABLE IF NOT EXISTS jnpa.parking_transactions (
    id bigserial PRIMARY KEY, vehicle_id text, driver_id text, facility_id text,
    slot_id bigint, entry_time timestamptz NOT NULL DEFAULT now(), exit_time timestamptz,
    duration interval, status text NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE','COMPLETED','EXPIRED')),
    created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS idx_parking_txn_vehicle ON jnpa.parking_transactions (vehicle_id, entry_time DESC);
CREATE INDEX IF NOT EXISTS idx_parking_txn_status ON jnpa.parking_transactions (status, entry_time DESC);
CREATE INDEX IF NOT EXISTS idx_parking_txn_facility ON jnpa.parking_transactions (facility_id, entry_time DESC);
CREATE TABLE IF NOT EXISTS jnpa.parking_events (
    id bigserial PRIMARY KEY, event_type text NOT NULL
        CHECK (event_type IN ('ALLOCATION','RELEASE','OVERFLOW','ILLEGAL_PARKING','NO_PARKING_VIOLATION')),
    vehicle_id text, driver_id text, facility_id text, slot_id bigint,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS idx_parking_events_type ON jnpa.parking_events (event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_parking_events_vehicle ON jnpa.parking_events (vehicle_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_parking_events_ts ON jnpa.parking_events (created_at DESC);
"""

_READY: Dict[str, bool] = {}


def _j(v: Any) -> str:
    try:
        return json.dumps(v if v is not None else {}, default=str)
    except Exception:  # noqa: BLE001
        return "{}"


async def _returning(sql: str, params: dict, *, dsn: str) -> Optional[dict]:
    """Run a mutating statement with RETURNING inside a COMMITTED transaction.

    jnpa_shared.db.fetch_one uses engine.connect() (no commit) — fine for SELECTs
    but it silently rolls back INSERT/UPDATE...RETURNING. This helper uses
    engine.begin() so the write actually commits AND the RETURNING row is read."""
    from sqlalchemy import text
    from jnpa_shared.db import get_engine

    engine = get_engine(dsn)
    async with engine.begin() as conn:
        result = await conn.execute(text(sql), params or {})
        row = result.mappings().first()
        return dict(row) if row else None


async def ensure_parking_schema(dsn: Optional[str]) -> None:
    if not dsn or _READY.get(dsn):
        return
    from jnpa_shared.db import execute

    for stmt in (s.strip() for s in _DDL.split(";")):
        if stmt:
            try:
                await execute(stmt, dsn=dsn)
            except Exception as exc:  # noqa: BLE001
                log.warning("parking_ddl_skipped", error=str(exc), stmt=stmt[:50])
    _READY[dsn] = True
    log.info("parking_schema_ready")


async def seed_inventory(facilities: List[Any], *, dsn: Optional[str]) -> int:
    """Upsert facilities + materialise their slots via generate_series (one cheap
    server-side statement per facility — no client-side insert burst). Idempotent.
    Returns the total slot count."""
    if not dsn:
        return 0
    from jnpa_shared.db import execute, fetch_one

    for f in facilities:
        await execute(
            """
            INSERT INTO jnpa.parking_facilities (id, facility_name, location, capacity, status)
            VALUES (:id, :name, CAST(:loc AS jsonb), :cap, 'OPEN')
            ON CONFLICT (id) DO UPDATE SET facility_name = EXCLUDED.facility_name,
                location = EXCLUDED.location, capacity = EXCLUDED.capacity
            """,
            {"id": f.id, "name": f.name,
             "loc": _j({"lat": f.lat, "lon": f.lon, "gate_id": f.gate_id}),
             "cap": f.capacity},
            dsn=dsn,
        )
        # Materialise slots 1..capacity server-side; ON CONFLICT keeps it idempotent.
        await execute(
            """
            INSERT INTO jnpa.parking_slots (facility_id, slot_number, availability_status)
            SELECT :fid, :prefix || gs, 'AVAILABLE'
            FROM generate_series(1, :cap) AS gs
            ON CONFLICT (facility_id, slot_number) DO NOTHING
            """,
            {"fid": f.id, "prefix": f.id + "-", "cap": f.capacity},
            dsn=dsn,
        )
    row = await fetch_one("SELECT count(*) AS n FROM jnpa.parking_slots", dsn=dsn)
    total = int(row["n"]) if row else 0
    log.info("parking_inventory_seeded", facilities=len(facilities), slots=total)
    return total


async def availability(*, dsn: Optional[str]) -> List[dict]:
    """Per-facility capacity/occupied/available — computed from real slot state."""
    if not dsn:
        return []
    from jnpa_shared.db import fetch_all

    try:
        rows = await fetch_all(
            """
            SELECT f.id AS facility_id, f.facility_name, f.location, f.capacity, f.status,
                   count(s.*) FILTER (WHERE s.availability_status = 'OCCUPIED') AS occupied,
                   count(s.*) FILTER (WHERE s.availability_status = 'AVAILABLE') AS available
            FROM jnpa.parking_facilities f
            LEFT JOIN jnpa.parking_slots s ON s.facility_id = f.id
            GROUP BY f.id, f.facility_name, f.location, f.capacity, f.status
            ORDER BY f.id
            """,
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001 - DB unreachable/unseeded → degrade to unavailable
        log.debug("parking_availability_unavailable", error=str(exc))
        return []
    out = []
    for r in rows:
        d = dict(r)
        cap = int(d.get("capacity") or 0)
        avail = int(d.get("available") or 0)
        occ = int(d.get("occupied") or 0)
        d["status"] = "FULL" if avail == 0 and cap > 0 else d.get("status") or "OPEN"
        d["free_pct"] = round(100.0 * avail / cap, 1) if cap else 0.0
        out.append(d)
    return out


async def summary(*, dsn: Optional[str]) -> dict:
    rows = await availability(dsn=dsn)
    cap = sum(int(r["capacity"]) for r in rows)
    occ = sum(int(r["occupied"]) for r in rows)
    avail = sum(int(r["available"]) for r in rows)
    return {"capacity": cap, "occupied": occ, "available": avail,
            "facilities": len(rows), "full": sum(1 for r in rows if r["status"] == "FULL")}


async def facilities_inventory(*, dsn: Optional[str]) -> List[dict]:
    if not dsn:
        return []
    from jnpa_shared.db import fetch_all

    try:
        rows = await fetch_all(
            "SELECT id, facility_name, location, capacity, status FROM jnpa.parking_facilities ORDER BY id",
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001 - DB unreachable/unseeded → fall back to static seed
        log.debug("parking_inventory_unavailable", error=str(exc))
        return []
    return [dict(r) for r in rows]


async def _dt_event(event_type: str, *, vehicle_id, driver_id, location, payload, dsn) -> None:
    """Mirror a parking event into the shared jnpa.digital_twin_events timeline."""
    from jnpa_shared.db import execute

    try:
        await execute(
            """
            INSERT INTO jnpa.digital_twin_events (event_type, vehicle_id, driver_id, location, payload)
            VALUES (:t, :v, :d, CAST(:loc AS jsonb), CAST(:p AS jsonb))
            """,
            {"t": event_type, "v": vehicle_id, "d": driver_id, "loc": _j(location), "p": _j(payload)},
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("parking_dt_event_failed", error=str(exc))


async def _notify(*, receiver, message, event_id, provider, dsn) -> None:
    """Log a driver notification into the shared jnpa.notifications trail."""
    from jnpa_shared.db import execute

    try:
        await execute(
            """
            INSERT INTO jnpa.notifications (event_id, channel, receiver, message, delivery_status, provider_response)
            VALUES (:e, 'push', :r, :m, 'SENT', CAST(:p AS jsonb))
            """,
            {"e": str(event_id) if event_id is not None else None, "r": receiver,
             "m": message, "p": _j(provider)},
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("parking_notify_failed", error=str(exc))


async def _parking_event(event_type, *, vehicle_id, driver_id, facility_id, slot_id, detail, dsn) -> None:
    from jnpa_shared.db import execute

    try:
        await execute(
            """
            INSERT INTO jnpa.parking_events (event_type, vehicle_id, driver_id, facility_id, slot_id, detail)
            VALUES (:t, :v, :d, :f, :s, CAST(:x AS jsonb))
            """,
            {"t": event_type, "v": vehicle_id, "d": driver_id, "f": facility_id,
             "s": slot_id, "x": _j(detail)},
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("parking_event_failed", error=str(exc))


async def allocate(*, facility_id: str, vehicle_id: str, driver_id: Optional[str],
                   dsn: Optional[str]) -> dict:
    """Atomically grab a free slot, open a transaction, log the event + notify.

    Uses ``FOR UPDATE SKIP LOCKED`` so concurrent allocations never hand out the
    same slot. Returns the allocation result (or an OVERFLOW result if full).
    """
    if not dsn:
        return {"allocated": False, "reason": "no_dsn"}

    # Atomic claim of one AVAILABLE slot in the facility (committed).
    row = await _returning(
        """
        UPDATE jnpa.parking_slots SET availability_status = 'OCCUPIED',
               vehicle_id = :v, updated_at = now()
        WHERE id = (
            SELECT id FROM jnpa.parking_slots
            WHERE facility_id = :f AND availability_status = 'AVAILABLE'
            ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
        RETURNING id, slot_number
        """,
        {"v": vehicle_id, "f": facility_id},
        dsn=dsn,
    )
    if row is None:
        # Facility full -> OVERFLOW event (still auditable).
        await _parking_event("OVERFLOW", vehicle_id=vehicle_id, driver_id=driver_id,
                             facility_id=facility_id, slot_id=None,
                             detail={"reason": "facility_full"}, dsn=dsn)
        await _dt_event("PARKING_OVERFLOW", vehicle_id=vehicle_id, driver_id=driver_id,
                        location={"facility_id": facility_id},
                        payload={"reason": "facility_full"}, dsn=dsn)
        return {"allocated": False, "reason": "facility_full", "facility_id": facility_id}

    slot_id, slot_number = row["id"], row["slot_number"]
    txn = await _returning(
        """
        INSERT INTO jnpa.parking_transactions (vehicle_id, driver_id, facility_id, slot_id, status)
        VALUES (:v, :d, :f, :s, 'ACTIVE') RETURNING id, entry_time
        """,
        {"v": vehicle_id, "d": driver_id, "f": facility_id, "s": slot_id},
        dsn=dsn,
    )
    txn_id = txn["id"] if txn else None
    detail = {"slot_number": slot_number, "slot_id": slot_id, "transaction_id": txn_id}
    await _parking_event("ALLOCATION", vehicle_id=vehicle_id, driver_id=driver_id,
                         facility_id=facility_id, slot_id=slot_id, detail=detail, dsn=dsn)
    await _dt_event("PARKING_ALLOCATION", vehicle_id=vehicle_id, driver_id=driver_id,
                    location={"facility_id": facility_id, "slot": slot_number},
                    payload=detail, dsn=dsn)
    await _notify(receiver=driver_id or vehicle_id,
                  message=f"Parking slot {slot_number} allocated at {facility_id}",
                  event_id=txn_id, provider={"channel": "push", "kind": "slot_allocated"}, dsn=dsn)
    return {"allocated": True, "facility_id": facility_id, "slot_number": slot_number,
            "slot_id": slot_id, "transaction_id": txn_id,
            "entry_time": txn["entry_time"].isoformat() if txn else None}


async def release(*, vehicle_id: str, dsn: Optional[str]) -> dict:
    """Close the vehicle's active transaction, free its slot, log RELEASE."""
    if not dsn:
        return {"released": False, "reason": "no_dsn"}
    from jnpa_shared.db import execute

    txn = await _returning(
        """
        UPDATE jnpa.parking_transactions
        SET exit_time = now(), duration = now() - entry_time, status = 'COMPLETED'
        WHERE id = (SELECT id FROM jnpa.parking_transactions
                    WHERE vehicle_id = :v AND status = 'ACTIVE'
                    ORDER BY entry_time DESC LIMIT 1)
        RETURNING id, facility_id, slot_id, entry_time,
                  EXTRACT(EPOCH FROM (now() - entry_time)) AS dur_s
        """,
        {"v": vehicle_id},
        dsn=dsn,
    )
    if txn is None:
        return {"released": False, "reason": "no_active_transaction", "vehicle_id": vehicle_id}
    await execute(
        "UPDATE jnpa.parking_slots SET availability_status = 'AVAILABLE', vehicle_id = NULL, updated_at = now() WHERE id = :s",
        {"s": txn["slot_id"]}, dsn=dsn,
    )
    detail = {"transaction_id": txn["id"], "slot_id": txn["slot_id"],
              "duration_s": int(txn["dur_s"]) if txn["dur_s"] is not None else None}
    await _parking_event("RELEASE", vehicle_id=vehicle_id, driver_id=None,
                         facility_id=txn["facility_id"], slot_id=txn["slot_id"],
                         detail=detail, dsn=dsn)
    await _dt_event("PARKING_RELEASE", vehicle_id=vehicle_id, driver_id=None,
                    location={"facility_id": txn["facility_id"]}, payload=detail, dsn=dsn)
    return {"released": True, "vehicle_id": vehicle_id, **detail,
            "facility_id": txn["facility_id"]}


async def raise_violation(*, event_type: str, vehicle_id: str, facility_id: Optional[str],
                          detail: dict, dsn: Optional[str]) -> dict:
    """Record an ILLEGAL_PARKING / NO_PARKING_VIOLATION event (+ timeline)."""
    et = event_type if event_type in ("ILLEGAL_PARKING", "NO_PARKING_VIOLATION") else "ILLEGAL_PARKING"
    await _parking_event(et, vehicle_id=vehicle_id, driver_id=None,
                         facility_id=facility_id, slot_id=None, detail=detail, dsn=dsn)
    await _dt_event("PARKING_VIOLATION", vehicle_id=vehicle_id, driver_id=None,
                    location={"facility_id": facility_id},
                    payload={"violation": et, **detail}, dsn=dsn)
    return {"recorded": True, "event_type": et, "vehicle_id": vehicle_id}


async def history(*, vehicle_id: Optional[str] = None, limit: int = 100,
                  dsn: Optional[str] = None) -> List[dict]:
    """Entry/exit transaction history (audit + dashboard)."""
    if not dsn:
        return []
    from jnpa_shared.db import fetch_all

    where = "WHERE vehicle_id = :v" if vehicle_id else ""
    params: Dict[str, Any] = {"limit": max(1, min(int(limit), 1000))}
    if vehicle_id:
        params["v"] = vehicle_id
    rows = await fetch_all(
        f"""
        SELECT id, vehicle_id, driver_id, facility_id, slot_id, entry_time, exit_time,
               EXTRACT(EPOCH FROM duration) AS duration_s, status
        FROM jnpa.parking_transactions {where}
        ORDER BY entry_time DESC LIMIT :limit
        """,
        params, dsn=dsn,
    )
    from datetime import datetime

    out = []
    for r in rows:
        d = dict(r)
        for k in ("entry_time", "exit_time"):
            if isinstance(d.get(k), datetime):
                d[k] = d[k].isoformat()
        out.append(d)
    return out


async def violations(*, limit: int = 100, dsn: Optional[str] = None) -> List[dict]:
    if not dsn:
        return []
    from jnpa_shared.db import fetch_all

    rows = await fetch_all(
        """
        SELECT id, event_type, vehicle_id, facility_id, detail, created_at
        FROM jnpa.parking_events
        WHERE event_type IN ('ILLEGAL_PARKING','NO_PARKING_VIOLATION','OVERFLOW')
        ORDER BY created_at DESC LIMIT :limit
        """,
        {"limit": max(1, min(int(limit), 1000))}, dsn=dsn,
    )
    from datetime import datetime

    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("created_at"), datetime):
            d["created_at"] = d["created_at"].isoformat()
        out.append(d)
    return out


__all__ = [
    "ensure_parking_schema", "seed_inventory", "availability", "summary",
    "facilities_inventory", "allocate", "release", "raise_violation",
    "history", "violations",
]
