"""Vehicle & Driver Intelligence (Vahan / Sarathi) — persistence + aggregation.

Phase 2 · Track 4. Makes every RC verification and DL lookup durable and provides
the aggregate reads the Vehicle- and Driver-Intelligence dashboards need. Reuses
the framework tables (vehicle_master, drivers, alerts, violation_cases, challans,
truck_telemetry, geofence_events, api_audit_log) — the audit framework CODE is
untouched. New history tables: vehicle_verification_history,
driver_license_lookup_history (migration 0008).

Best-effort writers; idempotent DDL applied at boot.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from .logging import get_logger

log = get_logger("gateway.vehicle_intel")

_DDL = (
    # Ensure the canonical drivers table exists (init.sql defines it with the same
    # IF NOT EXISTS; older DB volumes predate it). Additive + idempotent.
    """CREATE TABLE IF NOT EXISTS jnpa.drivers (
        driver_id text PRIMARY KEY, name text NOT NULL, license_no text, mobile text,
        vehicle_no text, aadhaar_masked text, emergency_contact text,
        status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','SUSPENDED')),
        photo_url text, reference_image text, template_dim int, provider text,
        enrolled_at timestamptz NOT NULL DEFAULT now(), approved_by text,
        updated_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS jnpa.vehicle_verification_history (
        id bigserial PRIMARY KEY, vehicle_number text,
        request_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
        response_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
        verification_status text, source text,
        created_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_veh_verif_number ON jnpa.vehicle_verification_history (vehicle_number, created_at DESC)",
    """CREATE TABLE IF NOT EXISTS jnpa.driver_license_lookup_history (
        id bigserial PRIMARY KEY, dl_number text,
        request_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
        response_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
        status text, source text,
        created_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_dl_lookup_number ON jnpa.driver_license_lookup_history (dl_number, created_at DESC)",
)
_READY: Dict[str, bool] = {}


def _j(v: Any) -> str:
    try:
        return json.dumps(v if v is not None else {}, default=str)
    except Exception:  # noqa: BLE001
        return "{}"


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (datetime, date)) else v


def _row(r: Any) -> dict:
    return {k: _iso(v) for k, v in dict(r).items()}


async def ensure_intel_schema(dsn: Optional[str]) -> None:
    if not dsn or _READY.get(dsn):
        return
    from jnpa_shared.db import execute

    for stmt in _DDL:
        try:
            await execute(stmt, dsn=dsn)
        except Exception as exc:  # noqa: BLE001
            log.debug("intel_ddl_skipped", error=str(exc))
    _READY[dsn] = True


# --- write paths ------------------------------------------------------------
async def record_vehicle_verification(*, vehicle_number, request, response,
                                      status, source, dsn) -> None:
    if not dsn:
        return
    from jnpa_shared.db import execute

    try:
        await execute(
            """
            INSERT INTO jnpa.vehicle_verification_history
                (vehicle_number, request_payload, response_payload, verification_status, source)
            VALUES (:v, CAST(:req AS jsonb), CAST(:resp AS jsonb), :st, :src)
            """,
            {"v": vehicle_number, "req": _j(request), "resp": _j(response),
             "st": status, "src": source}, dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("veh_verif_write_failed", error=str(exc))


async def record_dl_lookup(*, dl_number, request, response, status, source, dsn) -> None:
    if not dsn:
        return
    from jnpa_shared.db import execute

    try:
        await execute(
            """
            INSERT INTO jnpa.driver_license_lookup_history
                (dl_number, request_payload, response_payload, status, source)
            VALUES (:d, CAST(:req AS jsonb), CAST(:resp AS jsonb), :st, :src)
            """,
            {"d": dl_number, "req": _j(request), "resp": _j(response),
             "st": status, "src": source}, dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("dl_lookup_write_failed", error=str(exc))


async def upsert_driver_from_dl(*, dl_number, record: Dict[str, Any], dsn) -> None:
    """Promote a Sarathi DL result into the canonical jnpa.drivers record."""
    if not dsn or not isinstance(record, dict):
        return
    from jnpa_shared.db import execute

    name = (record.get("name") or record.get("driver_name") or record.get("holder_name")
            or "DL Holder")
    driver_id = f"DL:{dl_number}"
    try:
        await execute(
            """
            INSERT INTO jnpa.drivers (driver_id, name, license_no, status, provider, updated_at)
            VALUES (:id, :name, :dl, 'ACTIVE', 'sarathi', now())
            ON CONFLICT (driver_id) DO UPDATE SET
                name = EXCLUDED.name, license_no = EXCLUDED.license_no, updated_at = now()
            """,
            {"id": driver_id, "name": name, "dl": dl_number}, dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("driver_upsert_skipped", error=str(exc))


def dl_status(record: Dict[str, Any]) -> str:
    """Derive VALID/EXPIRED/NOT_FOUND from a DL record's validity/expiry field."""
    if not record:
        return "NOT_FOUND"
    for key in ("valid_upto", "validity", "expiry_date", "doe", "nt_validity_to"):
        v = record.get(key)
        if v:
            try:
                d = datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
                return "VALID" if d >= date.today() else "EXPIRED"
            except Exception:  # noqa: BLE001
                continue
    return "VALID"  # record present, no parsable expiry -> treat as valid


# --- aggregate reads (dashboards) ------------------------------------------
async def vehicle_intel(plate: str, *, dsn: Optional[str]) -> dict:
    """Everything known about a vehicle: RC + tracking + violations + challans + alerts.

    The six lookups are mutually independent (different tables, all keyed by the
    plate), so they run CONCURRENTLY via asyncio.gather rather than as six serial
    round-trips — the dominant cost when the DB is a remote RDS. Latency drops from
    the SUM of the six queries to the MAX of them. Each still degrades independently:
    return_exceptions=True means one failing lookup yields its own empty default
    without failing the others (identical fallback behaviour to the prior per-try
    version). Output shape is unchanged."""
    if not dsn:
        return {}
    from jnpa_shared.db import fetch_all, fetch_one

    async def _rc():
        r = await fetch_one("SELECT * FROM jnpa.vehicle_master WHERE plate = :p", {"p": plate}, dsn=dsn)
        return _row(r) if r else None

    async def _tracking():
        rows = await fetch_all(
            "SELECT ts, lat, lon, speed_kmh FROM jnpa.truck_telemetry WHERE plate = :p ORDER BY ts DESC LIMIT 20",
            {"p": plate}, dsn=dsn)
        return [_row(r) for r in rows]

    async def _violations():
        rows = await fetch_all(
            "SELECT case_id, status, total_fine, first_detected_at FROM jnpa.violation_cases WHERE vehicle_number = :p ORDER BY first_detected_at DESC LIMIT 20",
            {"p": plate}, dsn=dsn)
        return [_row(r) for r in rows]

    async def _challans():
        rows = await fetch_all(
            "SELECT challan_no, total_fine, status, issued_at FROM jnpa.challans WHERE vehicle_number = :p ORDER BY issued_at DESC LIMIT 20",
            {"p": plate}, dsn=dsn)
        return [_row(r) for r in rows]

    async def _alerts():
        rows = await fetch_all(
            "SELECT id, kind, severity, ts, payload FROM jnpa.alerts WHERE plate = :p ORDER BY ts DESC LIMIT 20",
            {"p": plate}, dsn=dsn)
        return [_row(r) for r in rows]

    async def _history():
        rows = await fetch_all(
            "SELECT verification_status, source, created_at FROM jnpa.vehicle_verification_history WHERE vehicle_number = :p ORDER BY created_at DESC LIMIT 10",
            {"p": plate}, dsn=dsn)
        return [_row(r) for r in rows]

    rc, tracking, violations, challans, alerts, hist = await asyncio.gather(
        _rc(), _tracking(), _violations(), _challans(), _alerts(), _history(),
        return_exceptions=True,
    )
    return {
        "vehicle_number": plate,
        "rc": _default(rc, None),
        "tracking": _default(tracking, []),
        "violations": _default(violations, []),
        "challans": _default(challans, []),
        "alerts": _default(alerts, []),
        "verification_history": _default(hist, []),
    }


def _default(value: Any, fallback: Any) -> Any:
    """gather(return_exceptions=True) result -> value, or the fallback if the
    query raised (preserves per-lookup graceful degradation)."""
    if isinstance(value, BaseException):
        return fallback
    return value


async def driver_intel(driver_key: str, *, dsn: Optional[str]) -> dict:
    """Driver profile + DL history + vehicle assignment + violations + activity."""
    if not dsn:
        return {}
    from jnpa_shared.db import fetch_all, fetch_one

    out: Dict[str, Any] = {"driver_key": driver_key}
    try:
        drv = await fetch_one(
            "SELECT * FROM jnpa.drivers WHERE driver_id = :k OR license_no = :k",
            {"k": driver_key}, dsn=dsn)
        out["driver"] = _row(drv) if drv else None
    except Exception:  # noqa: BLE001
        out["driver"] = None

    # The remaining three lookups depend only on fields already resolved from the
    # driver row, so they run CONCURRENTLY (was three serial round-trips).
    driver = out.get("driver") or {}
    dl_no = driver.get("license_no") or driver_key.replace("DL:", "")
    driver_id = driver.get("driver_id") or driver_key
    vehicle_no = driver.get("vehicle_no")
    out["vehicle_no"] = vehicle_no

    async def _dl_history():
        rows = await fetch_all(
            "SELECT status, source, response_payload, created_at FROM jnpa.driver_license_lookup_history WHERE dl_number = :d ORDER BY created_at DESC LIMIT 10",
            {"d": dl_no}, dsn=dsn)
        return [_row(r) for r in rows]

    async def _activity():
        rows = await fetch_all(
            "SELECT decision, score, ts FROM jnpa.verification_logs WHERE driver_id = :k ORDER BY ts DESC LIMIT 20",
            {"k": driver_id}, dsn=dsn)
        return [_row(r) for r in rows]

    async def _violations():
        if not vehicle_no:
            return []
        rows = await fetch_all(
            "SELECT case_id, status, total_fine FROM jnpa.violation_cases WHERE vehicle_number = :p ORDER BY first_detected_at DESC LIMIT 20",
            {"p": vehicle_no}, dsn=dsn)
        return [_row(r) for r in rows]

    dlh, vlog, cases = await asyncio.gather(
        _dl_history(), _activity(), _violations(), return_exceptions=True,
    )
    out["dl_history"] = _default(dlh, [])
    out["activity"] = _default(vlog, [])
    out["violations"] = _default(cases, [])
    return out


async def verification_history(*, limit: int, dsn: Optional[str]) -> List[dict]:
    if not dsn:
        return []
    from jnpa_shared.db import fetch_all

    rows = await fetch_all(
        "SELECT id, vehicle_number, verification_status, source, created_at FROM jnpa.vehicle_verification_history ORDER BY created_at DESC LIMIT :l",
        {"l": max(1, min(int(limit), 1000))}, dsn=dsn)
    return [_row(r) for r in rows]


async def dl_history(*, limit: int, dsn: Optional[str]) -> List[dict]:
    if not dsn:
        return []
    from jnpa_shared.db import fetch_all

    rows = await fetch_all(
        "SELECT id, dl_number, status, source, created_at FROM jnpa.driver_license_lookup_history ORDER BY created_at DESC LIMIT :l",
        {"l": max(1, min(int(limit), 1000))}, dsn=dsn)
    return [_row(r) for r in rows]


__all__ = [
    "ensure_intel_schema", "record_vehicle_verification", "record_dl_lookup",
    "upsert_driver_from_dl", "dl_status", "vehicle_intel", "driver_intel",
    "verification_history", "dl_history",
]
