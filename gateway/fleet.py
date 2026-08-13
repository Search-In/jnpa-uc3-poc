"""Vehicle Master (fleet registry) store — the authoritative list of registered
vehicles that a driver may be assigned to.

Background: before this module the "list of vehicles" came straight from the
truck-sim (``/devices/list``). That made the sim a hard dependency for driver
enrollment and gave operators no way to register, deactivate or annotate a
vehicle. This module introduces ``core.vehicle`` as the enterprise vehicle
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

import os

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
CREATE SCHEMA IF NOT EXISTS core;
CREATE TABLE IF NOT EXISTS core.vehicle (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id      text NOT NULL UNIQUE,
    vehicle_no      text UNIQUE,
    vehicle_type    text,
    chassis_number  text,
    rfid_fastag_id  text,
    status          text NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE', 'INACTIVE', 'MAINTENANCE')),
    created_by      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fleet_vehicles_vehicle_id ON core.vehicle (vehicle_id);
CREATE INDEX IF NOT EXISTS idx_fleet_vehicles_number ON core.vehicle (vehicle_no);
CREATE INDEX IF NOT EXISTS idx_fleet_vehicles_status ON core.vehicle (status);
"""
# ^ column is vehicle_no (the v3 runtime name every query in this module uses);
# the old vehicle_number here made the dev bootstrap diverge from runtime SQL.

# in-memory fallback store (DEV ONLY — used when no Postgres DSN is reachable),
# keyed by normalised vehicle_id.
_MEM: Dict[str, dict] = {}
# Resolved backend per DSN: None (undetermined) | "db" | "mem".
_BACKEND: Dict[str, str] = {}

_COLS = ("vehicle_id, NULLIF(vehicle_no, vehicle_id) AS vehicle_number, vehicle_type, chassis_number, "
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
        from jnpa_shared.db import execute, fetch_one  # lazy import

        if os.getenv("JNPA_RUNTIME_DDL", "0") == "1":
            for stmt in (s.strip() for s in _DDL.split(";")):
                if stmt:
                    await execute(stmt, dsn=dsn)
        else:
            # schema-v3: DDL owned by infra/postgres/v3; probe connectivity only.
            await fetch_one("SELECT 1 AS ok", dsn=dsn)
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


# First suffix of the OPERATOR-created Vehicle ID range. Everything below it
# belongs to the truck simulator, which mints TRK-000001 … TRK-{num_devices} and
# is hard-capped at max_devices=30 000 (ingest/trucking_app/trucking_app/
# config.py). 900 000 leaves that ceiling — and any plausible re-scaling of it —
# far behind, while staying inside the 6-digit id format so no existing
# consumer, index or regex changes. See :func:`next_vehicle_id`.
OPERATOR_ID_FLOOR = 900_001


def is_simulator_id(vehicle_id: str) -> bool:
    """True for a Vehicle ID in the simulator's range (suffix < the operator floor).

    Provenance test for a well-formed ``TRK-######``; anything that is not a
    canonical TRK id is not the simulator's (``_seq_of`` -> 0 would otherwise
    read as "simulator")."""
    seq = _seq_of(vehicle_id)
    return 0 < seq < OPERATOR_ID_FLOOR


def is_operator_id(vehicle_id: str) -> bool:
    """True for a Vehicle ID minted by :func:`next_vehicle_id` for an operator."""
    return _seq_of(vehicle_id) >= OPERATOR_ID_FLOOR


async def next_vehicle_id(dsn: str) -> str:
    """Next Vehicle ID for an OPERATOR-created vehicle, from the reserved range.

    Allocated as ``max(highest operator suffix, OPERATOR_ID_FLOOR - 1) + 1``, so
    the first operator vehicle is ``TRK-900001`` and every later one continues
    from the highest operator id already issued.

    WHY A SEPARATE RANGE. This used to be ``MAX(suffix over the WHOLE table) + 1``,
    which collided with the simulator. The sim mints ``TRK-000001 …
    TRK-0{num_devices}`` deterministically in memory (``TRUCK_NUM_DEVICES``,
    default 20 000) while the boot sync only imports the first ``limit=5000`` of
    them into ``core.vehicle`` (``gateway/main.py``). ``MAX`` therefore saw
    ~``TRK-005000`` and handed the next operator vehicle ``TRK-005001`` — an id
    the simulator is ALREADY using for a different truck with a different plate.
    The console would then show that operator's vehicle carrying the simulator's
    plate, and a re-route pushed to it would reach the wrong driver.

    ``OPERATOR_ID_FLOOR`` sits above the sim's hard ``max_devices`` ceiling
    (30 000), so the two namespaces cannot meet no matter how far the sim is
    scaled. Existing ids are untouched — this only changes what is minted NEXT,
    and :func:`sync_from_fleet` / :func:`sync_from_assignments` keep inserting
    the ids they are given.
    """
    highest = 0
    if await _backend(dsn) == "db":
        from jnpa_shared.db import fetch_one

        # Zero-padded ids sort lexically the same as numerically (<= 999999), so
        # MAX(vehicle_id) over the well-formed ids gives the highest suffix.
        # Restricted to the operator range: a simulator id must never advance
        # the operator sequence (nor be able to exhaust it).
        row = await fetch_one(
            "SELECT MAX(vehicle_id) AS m FROM core.vehicle "
            "WHERE vehicle_id ~ '^TRK-[0-9]{6}$' AND vehicle_id >= :floor",
            {"floor": _format_vehicle_id(OPERATOR_ID_FLOOR)}, dsn=dsn)
        highest = _seq_of(row["m"]) if row and row.get("m") else 0
    else:
        highest = max((s for s in (_seq_of(v) for v in _MEM)
                       if s >= OPERATOR_ID_FLOOR), default=0)
    return _format_vehicle_id(max(highest + 1, OPERATOR_ID_FLOOR))


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
            f"SELECT {_COLS} FROM core.vehicle "
            "WHERE UPPER(TRIM(vehicle_no)) = :n LIMIT 1", {"n": needle}, dsn=dsn)
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
            "SELECT 1 FROM core.vehicle WHERE vehicle_id = :v", {"v": vid}, dsn=dsn)
        if existing:
            raise ValueError("exists")
        await execute(
            """
            INSERT INTO core.vehicle
                (vehicle_id, vehicle_no, vehicle_type, chassis_number,
                 rfid_fastag_id, status, created_by, created_at, updated_at)
            VALUES (:vid, COALESCE(:num, :vid), :type, :chassis, :rfid, :status, :by, :now, :now)
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

        # DTO field -> core.vehicle column (vehicle_number lives as vehicle_no)
        _col = {"vehicle_number": "vehicle_no"}
        sets = ", ".join(f"{_col.get(k, k)} = :{k}" for k in updates)
        params = {**updates, "vid": vid, "now": now}
        n = await execute(
            f"UPDATE core.vehicle SET {sets}, updated_at = :now "
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
                INSERT INTO core.vehicle
                    (vehicle_id, vehicle_no, vehicle_type, status, created_by,
                     created_at, updated_at)
                VALUES (:vid, COALESCE(:num, :vid), :type, 'ACTIVE', 'system:truck-sim', :now, :now)
                ON CONFLICT (vehicle_no) DO NOTHING
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

    Every assigned vehicle (``core.driver_identity.vehicle_no_norm``) MUST exist as a
    ``core.vehicle.vehicle_id`` — that is the canonical relationship the PWA
    login gate and the deployment audit depend on. The truck-sim sync only covers
    sim devices, so assignments that came from elsewhere (admin-created plates,
    non-sim TRK ids) were orphaned. This backfills a fleet row for each such
    Vehicle ID so no ACTIVE driver is left dangling.

    CRITICAL: this NEVER touches ``core.driver_identity`` — assignments, PWA login and JWTs
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
        FROM core.driver_identity d
        LEFT JOIN core.vehicle f ON d.vehicle_no_norm = f.vehicle_id
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
            FROM core.driver_identity d
            LEFT JOIN core.vehicle f ON d.vehicle_no_norm = f.vehicle_id
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
            f"SELECT {_COLS} FROM core.vehicle WHERE vehicle_id = :v",
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
                           "UPPER(COALESCE(vehicle_no, '')) LIKE :needle)")
            params["needle"] = f"%{needle}%"
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = await fetch_all(
            f"SELECT {_COLS} FROM core.vehicle {where} "
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


# --------------------------------------------------------------- assignability
# "Free to take a NEW container job" is a DATABASE fact: it depends on
# core.container_job_assignment, not on anything the caller can compute or the
# client can filter. The predicate below is the single definition of it, built
# from the job module's own status vocabulary (services.container_job.service
# TERMINAL) so a new status can never silently make a busy truck look free.
def _open_job_predicate() -> tuple[str, dict]:
    """``NOT EXISTS (open job for this vehicle)`` + its bound parameters.

    Delegates to the job module's own definition so the truck list and the driver
    list (gateway.enrollment.list_assignable_drivers) exclude on ONE rule."""
    from services.container_job.service import open_job_not_exists

    return open_job_not_exists(
        "core.vehicle.vehicle_id", job_column="vehicle_id",
        # A registration identifies the truck the yard dispatches. core.vehicle is
        # unique on vehicle_id only, so the same plate can hold two rows; without
        # this the twin row of a busy truck reads as free.
        master_identity="core.vehicle.vehicle_no", job_identity="vehicle_no",
        # …and when the job row carries no registration of its own, resolve the
        # one it does carry — the Vehicle ID — back to a plate through the master,
        # so those jobs occupy the truck rather than just the record.
        identity_table="core.vehicle", identity_key="vehicle_id",
        identity_column="vehicle_no", identity_alias="mv")


# One physical truck = one option. The registration is the identity (a vehicle
# with no plate on file can only be itself, so it falls back to its Vehicle ID).
_VEHICLE_IDENTITY = ("NULLIF(upper(regexp_replace(coalesce(vehicle_no, ''), "
                     "'[^A-Za-z0-9]', '', 'g')), '')")
_DEDUPE_KEY = f"COALESCE({_VEHICLE_IDENTITY}, vehicle_id)"


def _assignable_where(q: Optional[str]) -> tuple[str, dict]:
    pred, params = _open_job_predicate()
    clauses = ["status = :st", "vehicle_id IS NOT NULL", pred]
    params["st"] = ACTIVE
    needle = (q or "").strip().upper()
    if needle:
        clauses.append("(UPPER(vehicle_id) LIKE :needle OR "
                       "UPPER(COALESCE(vehicle_no, '')) LIKE :needle)")
        params["needle"] = f"%{needle}%"
    return "WHERE " + " AND ".join(clauses), params


def _plate_identity(raw: Optional[str]) -> str:
    """Python twin of _VEHICLE_IDENTITY: strip everything that is not
    alphanumeric, upper-case the rest, so ``MH04 QA 9911`` and ``MH04QA9911`` are
    one truck.

    Deliberately NOT normalize_vehicle_no(), which only trims and upper-cases:
    that is the key of the one-active-driver-per-vehicle constraint
    (core.driver_identity.vehicle_no_norm) and widening it would change what that
    index means for data already written."""
    return "".join(ch for ch in (raw or "").upper() if ch.isalnum())


def _mem_identity(v: Mapping[str, Any]) -> str:
    """The physical truck a master row describes: its registration, or its
    Vehicle ID when no plate is on file. Mirrors _DEDUPE_KEY above."""
    return _plate_identity(v.get("vehicle_number")) or (v.get("vehicle_id") or "")


def _mem_assignable(occupied: set, q: Optional[str]) -> List[dict]:
    """Same two rules as the SQL path: exclude occupied trucks (by Vehicle ID OR
    registration — the caller's set may name either), then one row per truck."""
    needle = (q or "").strip().upper()
    # `occupied` carries Vehicle IDs AND normalised registrations (see
    # ContainerJobRepository.vehicles_with_open_jobs) — the same two keys the SQL
    # correlation matches on.
    busy = set(occupied or set())
    busy |= {_plate_identity(o) for o in busy if _plate_identity(o)}
    # A busy Vehicle ID names a RECORD; the truck it stands for is the plate that
    # record carries, so every OTHER record for that plate is busy too. Resolved
    # here for the same reason the SQL path resolves it through core.vehicle: the
    # caller's set may name only the id.
    busy |= {_mem_identity(v) for v in _MEM.values()
             if v.get("vehicle_id") in busy and _mem_identity(v)}
    out, seen = [], set()
    for v in _MEM.values():
        vid = v.get("vehicle_id")
        identity = _mem_identity(v)
        if not vid or v.get("status") != ACTIVE:
            continue
        if vid in busy or (identity and identity in busy):
            continue
        if needle and needle not in vid.upper() \
                and needle not in (v.get("vehicle_number") or "").upper():
            continue
        if identity in seen:
            continue
        seen.add(identity)
        out.append(dict(v))
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out


async def list_assignable(dsn: str, *, q: Optional[str] = None, limit: int = 50,
                          driver_map: Optional[Mapping[str, Mapping[str, Any]]] = None,
                          occupied: Optional[set] = None) -> List[dict]:
    """ACTIVE master vehicles with NO open container job — the Assign-Job dropdown.

    The exclusion is a JOIN, not a second round-trip subtracted in Python: the
    LIMIT therefore applies to vehicles that are genuinely assignable, so the
    page can never be padded with busy trucks nor truncated by them. There is no
    "if the job spine is unreachable, assume everything is free" path — an error
    propagates rather than fabricating availability.

    ``occupied`` is used ONLY by the in-memory (demo/test) backend, which has no
    job table to join against.

    ``driver_map`` (normalised Vehicle ID -> {driver_id, name}) enriches each row
    with its bound driver so the console can auto-select it (BUG-4). A vehicle
    with no driver is still listed — the driver requirement is enforced by
    assignment validation, not by hiding trucks (see list_available)."""
    dm = dict(driver_map or {})
    if await _backend(dsn) == "db":
        from jnpa_shared.db import fetch_all

        where, params = _assignable_where(q)
        params["lim"] = limit
        # DISTINCT ON collapses duplicate master rows for one truck BEFORE the
        # LIMIT, so the page holds `limit` distinct trucks rather than `limit`
        # rows that may name the same one twice.
        rows = [_row(r) for r in await fetch_all(
            f"SELECT * FROM (SELECT DISTINCT ON ({_DEDUPE_KEY}) {_COLS} "
            f"FROM core.vehicle {where} "
            f"ORDER BY {_DEDUPE_KEY}, created_at DESC) v "
            "ORDER BY created_at DESC LIMIT :lim", params, dsn=dsn)]
    else:
        rows = _mem_assignable(occupied or set(), q)[:limit]
    return [{"vehicle_id": v["vehicle_id"], "plate": v.get("vehicle_number"),
             "vehicle_number": v.get("vehicle_number"),
             "vehicle_type": v.get("vehicle_type"), "state": None,
             "driver_id": (dm.get(v["vehicle_id"]) or {}).get("driver_id"),
             "driver_name": (dm.get(v["vehicle_id"]) or {}).get("name"),
             # The PERSON bound to this truck, not just their record: the driver
             # list carries one record per licence, so the console matches the
             # binding to it by licence and never by Driver ID alone.
             "driver_licence": (dm.get(v["vehicle_id"]) or {}).get("license_no")}
            for v in rows]


async def count_assignable(dsn: str, *, q: Optional[str] = None,
                           occupied: Optional[set] = None) -> int:
    """How many vehicles are actually assignable right now — the number the
    "Vehicle (N available)" label must show. Counted in the database so it is not
    capped by the page ``limit`` the dropdown happens to request."""
    if await _backend(dsn) == "db":
        from jnpa_shared.db import fetch_one

        where, params = _assignable_where(q)
        row = await fetch_one(
            f"SELECT count(DISTINCT {_DEDUPE_KEY}) AS n FROM core.vehicle {where}",
            params, dsn=dsn)
        return int((row or {}).get("n") or 0)
    return len(_mem_assignable(occupied or set(), q))


async def list_available(dsn: str, assigned: set, *, q: Optional[str] = None,
                         limit: int = 50,
                         driver_map: Optional[Mapping[str, Mapping[str, Any]]] = None) -> List[dict]:
    """ACTIVE master vehicles that are free to take a NEW container job.

    ``assigned`` is the set of Vehicle IDs to exclude. As of the BUG-1 fix the
    caller passes the vehicles holding an OPEN JOB
    (``status NOT IN ('COMPLETED','CANCELLED')``) — NOT the vehicles that have a
    driver enrollment. Filtering on driver enrollment deadlocked the demo: the
    only trucks a driver can sign into the PWA with are exactly the driver-bound
    ones, and those were the ones being hidden from the dropdown.

    ``driver_map`` (normalised Vehicle ID -> {driver_id, name}, from
    :func:`enrollment.active_driver_vehicle_map`) enriches each row with its bound
    driver so the Control Room can auto-select the correct driver instead of
    leaving ``driver_id`` NULL (BUG-4). Vehicles WITHOUT a driver are still
    returned — the driver requirement is enforced at assignment validation, not by
    hiding trucks — but they carry ``driver_id: None`` so the UI can flag them."""
    rows = await list_vehicles(dsn, q=q, status=ACTIVE, limit=max(limit * 4, 200))
    dm = dict(driver_map or {})
    out: List[dict] = []
    for v in rows:
        vid = v.get("vehicle_id")
        if not vid or vid in assigned:
            continue
        holder = dm.get(vid) or {}
        out.append({"vehicle_id": vid, "plate": v.get("vehicle_number"),
                    # vehicle_number is the explicit alias; `plate` is kept for
                    # backward compatibility with the existing dropdown clients.
                    "vehicle_number": v.get("vehicle_number"),
                    "vehicle_type": v.get("vehicle_type"), "state": None,
                    "driver_id": holder.get("driver_id"),
                    "driver_name": holder.get("name")})
        if len(out) >= limit:
            break
    return out


async def stats(dsn: str, assigned: set,
                open_job_vehicles: Optional[set] = None) -> dict:
    """Dashboard counts.

    BUG-5: "assigned" used to mean "a driver holds this truck" while operators
    read it as "this truck is on a job" — two different numbers under one label.
    Both are now reported explicitly and the ambiguous keys are kept so no
    existing client breaks:

        driver_assigned_count  vehicles with an ACTIVE driver binding
        open_job_count         vehicles currently on a non-terminal job
        assigned               == driver_assigned_count  (legacy alias)
        available              == active - open_job_count

    ``available`` counts what the /available dropdown actually returns, so the
    KPI tile and the dropdown can no longer disagree (they did before: 8 assigned
    vs 0 open jobs)."""
    rows = await list_vehicles(dsn, limit=100000)
    total = len(rows)
    active = sum(1 for v in rows if v.get("status") == ACTIVE)
    active_ids = {v.get("vehicle_id") for v in rows if v.get("status") == ACTIVE}
    driver_assigned_n = len(active_ids & set(assigned))
    open_job_n = len(active_ids & set(open_job_vehicles or set()))
    return {"total": total, "active": active,
            "driver_assigned_count": driver_assigned_n,
            "open_job_count": open_job_n,
            "assigned": driver_assigned_n,          # legacy alias — do not remove
            "available": max(active - open_job_n, 0)}


__all__ = [
    "ACTIVE", "INACTIVE", "MAINTENANCE", "STATUSES", "ensure_backend",
    "add_vehicle", "update_vehicle", "sync_from_fleet", "sync_from_assignments",
    "orphan_active_drivers", "get_vehicle", "vehicle_exists", "list_vehicles",
    "list_available", "stats", "next_vehicle_id", "find_by_number",
    "OPERATOR_ID_FLOOR", "is_simulator_id", "is_operator_id",
]
