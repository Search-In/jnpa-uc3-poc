"""Vehicle Master (fleet registry) store — the authoritative list of registered
vehicles that a driver may be assigned to.

Background: before this module the "list of vehicles" came straight from the
truck-sim (``/devices/list``). That made the sim a hard dependency for driver
enrollment and gave operators no way to register, deactivate or annotate a
vehicle. This module introduces ``jnpa.fleet_vehicles`` as the enterprise vehicle
master: every vehicle exists here first (ACTIVE / INACTIVE / MAINTENANCE) and the
"assign vehicle" dropdown draws ONLY from here. The truck-sim is still ingested —
its devices are migrated into the master on boot (idempotent, never clobbering an
operator edit) so no existing fleet vehicle disappears.

Persistence mirrors :mod:`gateway.enrollment`: a Postgres backend with an
in-memory fallback selected per-DSN, self-provisioning its schema via an
idempotent ``_DDL`` so an already-initialised volume gains the table without an
init.sql re-run. In production an unreachable Postgres is fatal-per-request
(ProductionSafetyError -> 503), never a silent memory fallback.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from .enrollment import normalize_vehicle_no
from .logging import get_logger
from .mode import ProductionSafetyError, allow_memory_store, production_mode

log = get_logger("gateway.fleet")

# Lifecycle states (mirrors the CHECK constraint below).
ACTIVE = "ACTIVE"
INACTIVE = "INACTIVE"
MAINTENANCE = "MAINTENANCE"
STATUSES = (ACTIVE, INACTIVE, MAINTENANCE)

# Default vehicle_type for truck-sim-migrated devices (the sim models container
# trucks; operators can edit the type afterwards).
_DEFAULT_TYPE = "Container Truck"

# --- schema (idempotent; also applied at runtime so an existing volume gains the
# table without an init.sql re-run) -----------------------------------------
_DDL = """
CREATE SCHEMA IF NOT EXISTS jnpa;
CREATE TABLE IF NOT EXISTS jnpa.fleet_vehicles (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id      text NOT NULL UNIQUE,
    vehicle_number  text,
    vehicle_type    text,
    chassis_number  text,
    rfid_fastag_id  text,
    status          text NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE', 'INACTIVE', 'MAINTENANCE')),
    created_by      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fleet_vehicles_vehicle_id ON jnpa.fleet_vehicles (vehicle_id);
CREATE INDEX IF NOT EXISTS idx_fleet_vehicles_number ON jnpa.fleet_vehicles (vehicle_number);
CREATE INDEX IF NOT EXISTS idx_fleet_vehicles_status ON jnpa.fleet_vehicles (status);
"""

# in-memory fallback store (DEV ONLY — used when no Postgres DSN is reachable),
# keyed by normalised vehicle_id.
_MEM: Dict[str, dict] = {}
# Resolved backend per DSN: None (undetermined) | "db" | "mem".
_BACKEND: Dict[str, str] = {}

_COLS = ("vehicle_id, vehicle_number, vehicle_type, chassis_number, "
         "rfid_fastag_id, status, created_by, created_at, updated_at")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _backend(dsn: str) -> str:
    """Resolve (and memoise) Postgres vs in-memory, applying the schema once.

    DEV: any failure pins the in-memory backend so the demo runs without infra.
    PRODUCTION: an unavailable Postgres raises ProductionSafetyError (503)."""
    key = dsn or ""
    cached = _BACKEND.get(key)
    if cached:
        return cached
    if not key:
        if production_mode():
            raise ProductionSafetyError("postgres", "POSTGRES_DSN is not set")
        _BACKEND[key] = "mem"
        return "mem"
    try:
        from jnpa_shared.db import execute  # lazy import

        for stmt in (s.strip() for s in _DDL.split(";")):
            if stmt:
                await execute(stmt, dsn=dsn)
        _BACKEND[key] = "db"
        log.info("fleet_store_backend", backend="db")
        return "db"
    except Exception as exc:  # noqa: BLE001
        if not allow_memory_store():
            log.error("fleet_store_db_unavailable_production", error=str(exc))
            raise ProductionSafetyError("postgres", str(exc)) from exc
        _BACKEND[key] = "mem"
        log.warning("fleet_store_db_unavailable_using_memory", error=str(exc))
        return "mem"


async def ensure_backend(dsn: str) -> str:
    """Public entry point for the startup gate: surfaces a production DB failure."""
    return await _backend(dsn)


# --------------------------------------------------------------------------- shaping
def _iso(val: Any) -> Any:
    return val.isoformat() if isinstance(val, datetime) else val


def _row(row: Mapping[str, Any]) -> dict:
    d = dict(row)
    for k in ("created_at", "updated_at"):
        if k in d:
            d[k] = _iso(d[k])
    return d


def _seq_of(vehicle_id: str) -> int:
    """Numeric suffix of a ``TRK-000018`` id, or 0 if it doesn't match the pattern."""
    vid = (vehicle_id or "").strip().upper()
    if len(vid) == 10 and vid.startswith("TRK-") and vid[4:].isdigit():
        return int(vid[4:])
    return 0


def _format_vehicle_id(seq: int) -> str:
    return f"TRK-{seq:06d}"


async def next_vehicle_id(dsn: str) -> str:
    """Next Vehicle ID in the TRK sequence: max existing suffix + 1, zero-padded to
    6 digits (e.g. existing max TRK-000017 -> TRK-000018). Vehicle IDs are never
    entered by hand — the backend owns the sequence."""
    highest = 0
    if await _backend(dsn) == "db":
        from jnpa_shared.db import fetch_one

        # Zero-padded ids sort lexically the same as numerically (<= 999999), so
        # MAX(vehicle_id) over the well-formed ids gives the highest suffix.
        row = await fetch_one(
            "SELECT MAX(vehicle_id) AS m FROM jnpa.fleet_vehicles "
            "WHERE vehicle_id ~ '^TRK-[0-9]{6}$'", dsn=dsn)
        highest = _seq_of(row["m"]) if row and row.get("m") else 0
    else:
        highest = max((_seq_of(v) for v in _MEM), default=0)
    return _format_vehicle_id(highest + 1)


async def find_by_number(dsn: str, vehicle_number: str) -> Optional[dict]:
    """Return the vehicle holding this plate/number (case-insensitive), else None.
    Duplicate registration is guarded on ``vehicle_number`` (the human plate), not
    on the machine-generated ``vehicle_id``."""
    needle = (vehicle_number or "").strip().upper()
    if not needle:
        return None
    if await _backend(dsn) == "db":
        from jnpa_shared.db import fetch_one

        row = await fetch_one(
            f"SELECT {_COLS} FROM jnpa.fleet_vehicles "
            "WHERE UPPER(TRIM(vehicle_number)) = :n LIMIT 1", {"n": needle}, dsn=dsn)
        return _row(row) if row else None
    for v in _MEM.values():
        if (v.get("vehicle_number") or "").strip().upper() == needle:
            return dict(v)
    return None


# --------------------------------------------------------------------------- writes
async def add_vehicle(dsn: str, *, vehicle_id: str, vehicle_number: str = "",
                      vehicle_type: str = "", chassis_number: str = "",
                      rfid_fastag_id: str = "", status: str = ACTIVE,
                      created_by: Optional[str] = None) -> dict:
    """Register a new vehicle. Raises ValueError('exists') if the vehicle_id is taken."""
    vid = normalize_vehicle_no(vehicle_id)
    now = _now()
    if await _backend(dsn) == "db":
        from jnpa_shared.db import execute, fetch_one

        existing = await fetch_one(
            "SELECT 1 FROM jnpa.fleet_vehicles WHERE vehicle_id = :v", {"v": vid}, dsn=dsn)
        if existing:
            raise ValueError("exists")
        await execute(
            """
            INSERT INTO jnpa.fleet_vehicles
                (vehicle_id, vehicle_number, vehicle_type, chassis_number,
                 rfid_fastag_id, status, created_by, created_at, updated_at)
            VALUES (:vid, :num, :type, :chassis, :rfid, :status, :by, :now, :now)
            """,
            {"vid": vid, "num": vehicle_number or None, "type": vehicle_type or None,
             "chassis": chassis_number or None, "rfid": rfid_fastag_id or None,
             "status": status, "by": created_by, "now": now}, dsn=dsn)
        return await get_vehicle(dsn, vid) or {}
    if vid in _MEM:
        raise ValueError("exists")
    rec = {"vehicle_id": vid, "vehicle_number": vehicle_number or None,
           "vehicle_type": vehicle_type or None, "chassis_number": chassis_number or None,
           "rfid_fastag_id": rfid_fastag_id or None, "status": status,
           "created_by": created_by, "created_at": now.isoformat(),
           "updated_at": now.isoformat()}
    _MEM[vid] = rec
    return dict(rec)


async def update_vehicle(dsn: str, vehicle_id: str, *,
                         fields: Dict[str, Any]) -> Optional[dict]:
    """Patch a vehicle's editable columns. Returns the updated row (None if absent)."""
    vid = normalize_vehicle_no(vehicle_id)
    allowed = ("vehicle_number", "vehicle_type", "chassis_number",
               "rfid_fastag_id", "status")
    updates = {k: fields[k] for k in allowed if k in fields}
    if not updates:
        return await get_vehicle(dsn, vid)
    now = _now()
    if await _backend(dsn) == "db":
        from jnpa_shared.db import execute

        sets = ", ".join(f"{k} = :{k}" for k in updates)
        params = {**updates, "vid": vid, "now": now}
        n = await execute(
            f"UPDATE jnpa.fleet_vehicles SET {sets}, updated_at = :now "
            f"WHERE vehicle_id = :vid", params, dsn=dsn)
        if not n:
            return None
        return await get_vehicle(dsn, vid)
    rec = _MEM.get(vid)
    if rec is None:
        return None
    rec.update(updates)
    rec["updated_at"] = now.isoformat()
    return dict(rec)


async def sync_from_fleet(dsn: str, devices: List[Mapping[str, Any]]) -> int:
    """Migrate truck-sim devices into the vehicle master (idempotent).

    Inserts any device not already present as an ACTIVE vehicle; NEVER overwrites
    an existing row (an operator edit / deactivation always wins). Returns the
    number of newly-inserted vehicles. This is what preserves the existing fleet
    (TRK-000001, TRK-000002, …) when the master is introduced."""
    inserted = 0
    now = _now()
    if await _backend(dsn) == "db":
        from jnpa_shared.db import execute

        for dev in devices:
            vid = normalize_vehicle_no(dev.get("device_id") or dev.get("vehicle_id"))
            if not vid:
                continue
            n = await execute(
                """
                INSERT INTO jnpa.fleet_vehicles
                    (vehicle_id, vehicle_number, vehicle_type, status, created_by,
                     created_at, updated_at)
                VALUES (:vid, :num, :type, 'ACTIVE', 'system:truck-sim', :now, :now)
                ON CONFLICT (vehicle_id) DO NOTHING
                """,
                {"vid": vid, "num": (dev.get("plate") or None), "type": _DEFAULT_TYPE,
                 "now": now}, dsn=dsn)
            inserted += int(n or 0)
        if inserted:
            log.info("fleet_migrated_from_truck_sim", inserted=inserted)
        return inserted
    for dev in devices:
        vid = normalize_vehicle_no(dev.get("device_id") or dev.get("vehicle_id"))
        if not vid or vid in _MEM:
            continue
        _MEM[vid] = {"vehicle_id": vid, "vehicle_number": dev.get("plate") or None,
                     "vehicle_type": _DEFAULT_TYPE, "chassis_number": None,
                     "rfid_fastag_id": None, "status": ACTIVE,
                     "created_by": "system:truck-sim", "created_at": now.isoformat(),
                     "updated_at": now.isoformat()}
        inserted += 1
    return inserted


def _looks_like_trk_id(value: str) -> bool:
    """True for the canonical fleet Vehicle ID shape TRK-000123 (vs a plate)."""
    return len(value) == 10 and value.startswith("TRK-") and value[4:].isdigit()


async def sync_from_assignments(dsn: str) -> int:
    """Backfill the Vehicle Master from EXISTING driver assignments.

    Every assigned vehicle (``jnpa.drivers.vehicle_no_norm``) MUST exist as a
    ``jnpa.fleet_vehicles.vehicle_id`` — that is the canonical relationship the PWA
    login gate and the deployment audit depend on. The truck-sim sync only covers
    sim devices, so assignments that came from elsewhere (admin-created plates,
    non-sim TRK ids) were orphaned. This backfills a fleet row for each such
    Vehicle ID so no ACTIVE driver is left dangling.

    CRITICAL: this NEVER touches ``jnpa.drivers`` — assignments, PWA login and JWTs
    are unchanged; it only *adds* the missing fleet rows the assignments point at.
    Idempotent (skips ids that already exist). Returns the number inserted.

    For a Vehicle ID that is a plate (not TRK-shaped) the plate is also stored as
    the ``vehicle_number``; for a TRK-shaped id the driver's original ``vehicle_no``
    (if it is a plate) is used as the number, else it is left null."""
    from . import enrollment  # local import: enrollment never imports fleet

    assignments = await enrollment.all_assignments(dsn)
    inserted = 0
    for a in assignments:
        vid = normalize_vehicle_no(a.get("vehicle_no_norm"))
        if not vid or await get_vehicle(dsn, vid):
            continue
        raw = (a.get("vehicle_no") or "").strip()
        if _looks_like_trk_id(vid):
            number = "" if _looks_like_trk_id(raw.upper()) else raw
        else:
            number = vid  # the Vehicle ID *is* the plate
        try:
            await add_vehicle(dsn, vehicle_id=vid, vehicle_number=number,
                              vehicle_type=_DEFAULT_TYPE, status=ACTIVE,
                              created_by="system:assignment-backfill")
            inserted += 1
        except ValueError:
            continue  # inserted concurrently — fine
    if inserted:
        log.info("fleet_assignment_backfill", inserted=inserted)
    return inserted


async def orphan_active_drivers(dsn: str, *, active_only: bool = True) -> List[dict]:
    """Deployment audit: ACTIVE drivers whose assigned vehicle has NO matching fleet
    vehicle. Mirrors the verification query

        SELECT d.driver_id, d.name, d.vehicle_no_norm
        FROM jnpa.drivers d
        LEFT JOIN jnpa.fleet_vehicles f ON d.vehicle_no_norm = f.vehicle_id
        WHERE f.vehicle_id IS NULL;

    Returns ``[{driver_id, name, vehicle_no_norm}, …]`` (empty == healthy). Only
    considers drivers that actually hold an assignment (non-null vehicle_no_norm);
    ``active_only=False`` audits every status."""
    if await _backend(dsn) == "db":
        from jnpa_shared.db import fetch_all

        status_clause = "d.status = 'ACTIVE' AND " if active_only else ""
        rows = await fetch_all(
            f"""
            SELECT d.driver_id, d.name, d.vehicle_no_norm
            FROM jnpa.drivers d
            LEFT JOIN jnpa.fleet_vehicles f ON d.vehicle_no_norm = f.vehicle_id
            WHERE {status_clause} f.vehicle_id IS NULL
              AND d.vehicle_no_norm IS NOT NULL AND TRIM(d.vehicle_no_norm) <> ''
            ORDER BY d.driver_id
            """, dsn=dsn)
        return [dict(r) for r in rows]
    from . import enrollment

    out: List[dict] = []
    for d in enrollment._MEM_DRIVERS.values():
        if active_only and d.get("status") != ACTIVE:
            continue
        vid = (d.get("vehicle_no_norm") or "").strip()
        if vid and vid not in _MEM:
            out.append({"driver_id": d.get("driver_id"), "name": d.get("name"),
                        "vehicle_no_norm": vid})
    return out


# --------------------------------------------------------------------------- reads
async def get_vehicle(dsn: str, vehicle_id: str) -> Optional[dict]:
    vid = normalize_vehicle_no(vehicle_id)
    if not vid:
        return None
    if await _backend(dsn) == "db":
        from jnpa_shared.db import fetch_one

        row = await fetch_one(
            f"SELECT {_COLS} FROM jnpa.fleet_vehicles WHERE vehicle_id = :v",
            {"v": vid}, dsn=dsn)
        return _row(row) if row else None
    rec = _MEM.get(vid)
    return dict(rec) if rec else None


async def vehicle_exists(dsn: str, vehicle_id: str, *,
                         active_only: bool = False) -> bool:
    """True if the Vehicle ID is registered in the master (optionally ACTIVE-only)."""
    rec = await get_vehicle(dsn, vehicle_id)
    if not rec:
        return False
    return (not active_only) or rec.get("status") == ACTIVE


async def list_vehicles(dsn: str, *, q: Optional[str] = None,
                        status: Optional[str] = None,
                        limit: int = 500) -> List[dict]:
    """All master vehicles (newest first), optionally filtered by search / status."""
    needle = (q or "").strip().upper()
    st = (status or "").strip().upper() or None
    if await _backend(dsn) == "db":
        from jnpa_shared.db import fetch_all

        clauses, params = [], {"lim": limit}
        if st and st in STATUSES:
            clauses.append("status = :st")
            params["st"] = st
        if needle:
            clauses.append("(UPPER(vehicle_id) LIKE :needle OR "
                           "UPPER(COALESCE(vehicle_number, '')) LIKE :needle)")
            params["needle"] = f"%{needle}%"
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = await fetch_all(
            f"SELECT {_COLS} FROM jnpa.fleet_vehicles {where} "
            f"ORDER BY created_at DESC LIMIT :lim", params, dsn=dsn)
        return [_row(r) for r in rows]
    items = list(_MEM.values())
    if st and st in STATUSES:
        items = [v for v in items if v.get("status") == st]
    if needle:
        items = [v for v in items
                 if needle in (v.get("vehicle_id") or "").upper()
                 or needle in (v.get("vehicle_number") or "").upper()]
    items.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return [dict(v) for v in items[:limit]]


async def list_available(dsn: str, assigned: set, *, q: Optional[str] = None,
                         limit: int = 50) -> List[dict]:
    """ACTIVE master vehicles NOT already assigned to a driver / open enrollment.

    ``assigned`` is the set of normalised Vehicle IDs taken (from
    :func:`enrollment.assigned_vehicles`). This is the source for the
    ``GET /api/vehicles/available`` dropdown."""
    rows = await list_vehicles(dsn, q=q, status=ACTIVE, limit=max(limit * 4, 200))
    out: List[dict] = []
    for v in rows:
        vid = v.get("vehicle_id")
        if not vid or vid in assigned:
            continue
        out.append({"vehicle_id": vid, "plate": v.get("vehicle_number"),
                    "vehicle_type": v.get("vehicle_type"), "state": None})
        if len(out) >= limit:
            break
    return out


async def stats(dsn: str, assigned: set) -> dict:
    """Dashboard counts: total / active / assigned / available."""
    rows = await list_vehicles(dsn, limit=100000)
    total = len(rows)
    active = sum(1 for v in rows if v.get("status") == ACTIVE)
    # "assigned" = ACTIVE master vehicles that a driver/enrollment holds.
    active_ids = {v.get("vehicle_id") for v in rows if v.get("status") == ACTIVE}
    assigned_n = len(active_ids & set(assigned))
    return {"total": total, "active": active, "assigned": assigned_n,
            "available": max(active - assigned_n, 0)}


__all__ = [
    "ACTIVE", "INACTIVE", "MAINTENANCE", "STATUSES", "ensure_backend",
    "add_vehicle", "update_vehicle", "sync_from_fleet", "sync_from_assignments",
    "orphan_active_drivers", "get_vehicle", "vehicle_exists", "list_vehicles",
    "list_available", "stats", "next_vehicle_id", "find_by_number",
]
