"""RDS persistence for Empty Container Allocation (Phase 2 · Track 2).

The optimiser already produced probable allocations; this module makes the
INVENTORY, every ALLOCATION, and the MOVEMENT HISTORY durable in Postgres, and
reuses the audit-framework TABLES (core.digital_twin_event / core.decision_audit)
by writing to them directly — the audit framework CODE is not modified.

Lives inside the bind-mounted empty-container service and builds on the installed
``jnpa_shared.db`` engine. Best-effort writers; idempotent DDL (mirrors migration
0006) applied at boot; inventory materialised via server-side generate_series so
there is no client-side insert burst (kind to a memory-tight Postgres).
"""
from __future__ import annotations

import os

import json
from typing import Any, Dict, List, Optional

from jnpa_shared.logging import get_logger

log = get_logger("empty_container.persistence")

# Per (depot, container_type) inventory cap so the table stays bounded on a
# memory-tight host. Real stock above this still allocates (availability is also
# tracked by the depot books); this only bounds the materialised container rows.
_MAX_PER_TYPE = 40

_DDL = """
CREATE SCHEMA IF NOT EXISTS core;
CREATE TABLE IF NOT EXISTS core.empty_container_inventory (
    container_id text PRIMARY KEY, container_type text, location text, owner text,
    availability_status text NOT NULL DEFAULT 'AVAILABLE'
        CHECK (availability_status IN ('AVAILABLE','ALLOCATED','IN_TRANSIT','DELIVERED')),
    updated_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS idx_ec_inventory_status ON core.empty_container_inventory (availability_status, container_type);
CREATE INDEX IF NOT EXISTS idx_ec_inventory_location ON core.empty_container_inventory (location);
CREATE TABLE IF NOT EXISTS core.empty_container_allocation (
    id bigserial PRIMARY KEY, container_id text, truck_id text, trailer_id text,
    driver_id text, shipping_line text, cfs text, ecd text, allocation_reason text,
    allocated_at timestamptz NOT NULL DEFAULT now(),
    status text NOT NULL DEFAULT 'ALLOCATED'
        CHECK (status IN ('ALLOCATED','PICKED_UP','IN_TRANSIT','DELIVERED','COMPLETED','CANCELLED')));
CREATE INDEX IF NOT EXISTS idx_ec_alloc_container ON core.empty_container_allocation (container_id, allocated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ec_alloc_status ON core.empty_container_allocation (status, allocated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ec_alloc_ts ON core.empty_container_allocation (allocated_at DESC);
CREATE TABLE IF NOT EXISTS core.container_movement_history (
    id bigserial PRIMARY KEY, container_id text, allocation_id bigint,
    movement_type text NOT NULL
        CHECK (movement_type IN ('PICKUP','ALLOCATION','TRANSFER','DELIVERY','COMPLETION')),
    location text, detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS idx_container_move_container ON core.container_movement_history (container_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_container_move_type ON core.container_movement_history (movement_type, created_at DESC);
"""

_READY: Dict[str, bool] = {}


def _j(v: Any) -> str:
    try:
        return json.dumps(v if v is not None else {}, default=str)
    except Exception:  # noqa: BLE001
        return "{}"


async def _returning(sql: str, params: dict, *, dsn: str) -> Optional[dict]:
    """Run a mutating statement with RETURNING inside a COMMITTED transaction.

    jnpa_shared.db.fetch_one uses engine.connect() (no commit) and silently rolls
    back INSERT/UPDATE...RETURNING; this uses engine.begin() so the write commits."""
    from sqlalchemy import text
    from jnpa_shared.db import get_engine

    engine = get_engine(dsn)
    async with engine.begin() as conn:
        result = await conn.execute(text(sql), params or {})
        row = result.mappings().first()
        return dict(row) if row else None


async def ensure_container_schema(dsn: Optional[str]) -> None:
    if os.getenv("JNPA_RUNTIME_DDL", "0") != "1":
        # schema-v3: DDL is owned by infra/postgres/v3 migrations, never runtime.
        return
    if not dsn or _READY.get(dsn):
        return
    from jnpa_shared.db import execute

    for stmt in (s.strip() for s in _DDL.split(";")):
        if stmt:
            try:
                await execute(stmt, dsn=dsn)
            except Exception as exc:  # noqa: BLE001
                log.warning("container_ddl_skipped", error=str(exc), stmt=stmt[:50])
    _READY[dsn] = True
    log.info("container_schema_ready")


async def seed_inventory(depots: List[Any], *, dsn: Optional[str]) -> int:
    """Materialise empty-container inventory from the depot stock books.

    One server-side generate_series INSERT per (depot, container_type), bounded by
    _MAX_PER_TYPE. container_id = "<depot>-<TYPE>-NNNN". Idempotent (PK conflict
    no-ops). owner = depot.kind (ECD|CFS); location = depot_id."""
    if not dsn:
        return 0
    from jnpa_shared.db import execute, fetch_one

    for dep in depots:
        for ctype, count in (dep.stock or {}).items():
            n = min(int(count or 0), _MAX_PER_TYPE)
            if n <= 0:
                continue
            await execute(
                """
                INSERT INTO core.empty_container_inventory
                    (container_id, container_type, location, owner, availability_status)
                SELECT :prefix || lpad(gs::text, 4, '0'), :ctype, :loc, :owner, 'AVAILABLE'
                FROM generate_series(1, :n) AS gs
                ON CONFLICT (container_id) DO NOTHING
                """,
                {"prefix": f"{dep.depot_id}-{ctype}-", "ctype": ctype,
                 "loc": dep.depot_id, "owner": dep.kind, "n": n},
                dsn=dsn,
            )
    row = await fetch_one("SELECT count(*) AS n FROM core.empty_container_inventory", dsn=dsn)
    total = int(row["n"]) if row else 0
    log.info("container_inventory_seeded", depots=len(depots), containers=total)
    return total


async def available(*, container_type: Optional[str] = None, limit: int = 200,
                    dsn: Optional[str] = None) -> List[dict]:
    if not dsn:
        return []
    from jnpa_shared.db import fetch_all

    where = "WHERE availability_status = 'AVAILABLE'"
    params: Dict[str, Any] = {"limit": max(1, min(int(limit), 1000))}
    if container_type:
        where += " AND container_type = :ct"
        params["ct"] = container_type
    rows = await fetch_all(
        f"""
        SELECT container_id, container_type, location, owner, availability_status, updated_at
        FROM core.empty_container_inventory {where}
        ORDER BY container_id LIMIT :limit
        """,
        params, dsn=dsn,
    )
    return [_row(r) for r in rows]


async def available_summary(*, dsn: Optional[str]) -> dict:
    if not dsn:
        return {}
    from jnpa_shared.db import fetch_all

    rows = await fetch_all(
        """
        SELECT container_type,
               count(*) FILTER (WHERE availability_status='AVAILABLE') AS available,
               count(*) FILTER (WHERE availability_status='ALLOCATED') AS allocated,
               count(*) AS total
        FROM core.empty_container_inventory GROUP BY container_type ORDER BY container_type
        """,
        dsn=dsn,
    )
    return {"by_type": [dict(r) for r in rows]}


async def allocate_container(*, container_type: str, truck_id: Optional[str],
                             trailer_id: Optional[str], driver_id: Optional[str],
                             shipping_line: Optional[str], cargo_type: Optional[str],
                             reason: Optional[str], dsn: Optional[str]) -> dict:
    """Atomically claim an AVAILABLE container of the type; persist the allocation,
    the movement (ALLOCATION), a digital_twin_event, and a decision_audit row."""
    if not dsn:
        return {"allocated": False, "reason": "no_dsn"}
    from jnpa_shared.db import execute

    inv = await _returning(
        """
        UPDATE core.empty_container_inventory SET availability_status = 'ALLOCATED', updated_at = now()
        WHERE container_id = (
            SELECT container_id FROM core.empty_container_inventory
            WHERE availability_status = 'AVAILABLE' AND container_type = :ct
            ORDER BY container_id LIMIT 1 FOR UPDATE SKIP LOCKED)
        RETURNING container_id, container_type, location, owner
        """,
        {"ct": container_type}, dsn=dsn,
    )
    if inv is None:
        await _decision_audit(request_id=None, rule="empty-container",
                              decision="NO_STOCK", action="REJECTED",
                              inp={"container_type": container_type}, dsn=dsn)
        return {"allocated": False, "reason": "no_available_container", "container_type": container_type}

    container_id = inv["container_id"]
    ecd = inv["location"] if inv["owner"] == "ECD" else None
    cfs = inv["location"] if inv["owner"] == "CFS" else None
    alloc_reason = reason or f"Nearest available {container_type} at {inv['location']} ({inv['owner']})"
    alloc = await _returning(
        """
        INSERT INTO core.empty_container_allocation
            (container_id, truck_id, trailer_id, driver_id, shipping_line, cfs, ecd,
             allocation_reason, status)
        VALUES (:cid, :truck, :trailer, :driver, :sl, :cfs, :ecd, :reason, 'ALLOCATED')
        RETURNING id, allocated_at
        """,
        {"cid": container_id, "truck": truck_id, "trailer": trailer_id, "driver": driver_id,
         "sl": shipping_line, "cfs": cfs, "ecd": ecd, "reason": alloc_reason},
        dsn=dsn,
    )
    alloc_id = alloc["id"] if alloc else None
    await execute(
        """
        INSERT INTO core.container_movement_history (container_id, allocation_id, movement_type, location, detail)
        VALUES (:cid, :aid, 'ALLOCATION', :loc, CAST(:d AS jsonb))
        """,
        {"cid": container_id, "aid": alloc_id, "loc": inv["location"],
         "d": _j({"truck_id": truck_id, "driver_id": driver_id, "shipping_line": shipping_line})},
        dsn=dsn,
    )
    await _dt_event(
        "CONTAINER_ALLOCATION", vehicle_id=truck_id, driver_id=driver_id,
        location={"depot": inv["location"], "owner": inv["owner"]},
        payload={"allocation_id": alloc_id, "container_id": container_id,
                 "container_type": container_type, "shipping_line": shipping_line,
                 "cfs": cfs, "ecd": ecd, "reason": alloc_reason}, dsn=dsn,
    )
    await _decision_audit(
        request_id=str(alloc_id), rule="empty-container", decision="ALLOCATED",
        action="ASSIGN", inp={"container_type": container_type, "truck_id": truck_id,
                              "container_id": container_id}, dsn=dsn,
    )
    return {"allocated": True, "allocation_id": alloc_id, "container_id": container_id,
            "container_type": container_type, "truck_id": truck_id, "trailer_id": trailer_id,
            "driver_id": driver_id, "shipping_line": shipping_line, "cfs": cfs, "ecd": ecd,
            "allocation_reason": alloc_reason,
            "allocated_at": alloc["allocated_at"].isoformat() if alloc else None}


async def allocation_history(*, limit: int = 100, dsn: Optional[str] = None) -> List[dict]:
    if not dsn:
        return []
    from jnpa_shared.db import fetch_all

    rows = await fetch_all(
        """
        SELECT id, container_id, truck_id, trailer_id, driver_id, shipping_line,
               cfs, ecd, allocation_reason, allocated_at, status
        FROM core.empty_container_allocation ORDER BY allocated_at DESC LIMIT :limit
        """,
        {"limit": max(1, min(int(limit), 1000))}, dsn=dsn,
    )
    return [_row(r) for r in rows]


async def _dt_event(event_type, *, vehicle_id, driver_id, location, payload, dsn) -> None:
    from jnpa_shared.db import execute

    try:
        await execute(
            """
            INSERT INTO core.digital_twin_event (event_type, vehicle_id, driver_id, location, payload)
            VALUES (:t, :v, :d, CAST(:loc AS jsonb), CAST(:p AS jsonb))
            """,
            {"t": event_type, "v": vehicle_id, "d": driver_id, "loc": _j(location), "p": _j(payload)},
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("container_dt_event_failed", error=str(exc))


async def _decision_audit(*, request_id, rule, decision, action, inp, dsn) -> None:
    from jnpa_shared.db import execute

    try:
        await execute(
            """
            INSERT INTO core.decision_audit (request_id, input_data, rule_executed, decision, action_taken)
            VALUES (:r, CAST(:i AS jsonb), :rule, :dec, :act)
            """,
            {"r": request_id, "i": _j(inp), "rule": rule, "dec": decision, "act": action},
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("container_decision_audit_failed", error=str(exc))


def _row(r: Any) -> dict:
    from datetime import datetime

    d = dict(r)
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


__all__ = [
    "ensure_container_schema", "seed_inventory", "available", "available_summary",
    "allocate_container", "allocation_history",
]
