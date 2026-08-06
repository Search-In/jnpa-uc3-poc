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

import os

import asyncio
import json
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from .logging import get_logger

log = get_logger("gateway.vehicle_intel")

_DDL = (
    # Ensure the canonical drivers table exists (init.sql defines it with the same
    # IF NOT EXISTS; older DB volumes predate it). Additive + idempotent.
    """CREATE TABLE IF NOT EXISTS core.driver_identity (
        driver_id text PRIMARY KEY, name text NOT NULL, license_no text, mobile text,
        vehicle_no text, aadhaar_masked text, emergency_contact text,
        status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','SUSPENDED')),
        photo_url text, reference_image text, template_dim int, provider text,
        enrolled_at timestamptz NOT NULL DEFAULT now(), approved_by text,
        updated_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS core.vehicle_verification_history (
        id bigserial PRIMARY KEY, vehicle_number text,
        request_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
        response_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
        verification_status text, source text,
        created_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_veh_verif_number ON core.vehicle_verification_history (vehicle_number, created_at DESC)",
    """CREATE TABLE IF NOT EXISTS core.driver_license_lookup_history (
        id bigserial PRIMARY KEY, dl_number text,
        request_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
        response_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
        status text, source text,
        created_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_dl_lookup_number ON core.driver_license_lookup_history (dl_number, created_at DESC)",
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
    if os.getenv("JNPA_RUNTIME_DDL", "0") != "1":
        # schema-v3: DDL is owned by infra/postgres/v3 migrations, never runtime.
        return
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
            INSERT INTO core.vehicle_verification_history
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
            INSERT INTO core.driver_license_lookup_history
                (dl_number, request_payload, response_payload, status, source)
            VALUES (:d, CAST(:req AS jsonb), CAST(:resp AS jsonb), :st, :src)
            """,
            {"d": dl_number, "req": _j(request), "resp": _j(response),
             "st": status, "src": source}, dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("dl_lookup_write_failed", error=str(exc))


async def upsert_driver_from_dl(*, dl_number, record: Dict[str, Any], dsn) -> None:
    """Promote a Sarathi DL result into the canonical core.driver_identity record."""
    if not dsn or not isinstance(record, dict):
        return
    from jnpa_shared.db import execute

    name = (record.get("name") or record.get("driver_name") or record.get("holder_name")
            or "DL Holder")
    driver_id = f"DL:{dl_number}"
    try:
        await execute(
            """
            INSERT INTO core.driver_identity (driver_id, name, license_no, status, provider, updated_at)
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
        r = await fetch_one("SELECT * FROM core.vehicle_rc WHERE plate = :p", {"p": plate}, dsn=dsn)
        return _row(r) if r else None

    async def _tracking():
        rows = await fetch_all(
            "SELECT ts, lat, lon, speed_kmh FROM core.truck_telemetry WHERE plate = :p ORDER BY ts DESC LIMIT 20",
            {"p": plate}, dsn=dsn)
        return [_row(r) for r in rows]

    async def _violations():
        rows = await fetch_all(
            "SELECT case_id, status, total_fine, first_detected_at FROM core.violation_case WHERE vehicle_number = :p ORDER BY first_detected_at DESC LIMIT 20",
            {"p": plate}, dsn=dsn)
        return [_row(r) for r in rows]

    async def _challans():
        rows = await fetch_all(
            "SELECT challan_no, total_fine, status, issued_at FROM core.challan WHERE vehicle_number = :p ORDER BY issued_at DESC LIMIT 20",
            {"p": plate}, dsn=dsn)
        return [_row(r) for r in rows]

    async def _alerts():
        rows = await fetch_all(
            "SELECT id, kind, severity, ts, payload FROM core.alert WHERE plate = :p ORDER BY ts DESC LIMIT 20",
            {"p": plate}, dsn=dsn)
        return [_row(r) for r in rows]

    async def _history():
        rows = await fetch_all(
            "SELECT verification_status, source, created_at FROM core.vehicle_verification_history WHERE vehicle_number = :p ORDER BY created_at DESC LIMIT 10",
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


# ---------------------------------------------------------------------------
# Vehicle 360 — the operator's single screen for one registration number.
#
# The spine is vehicle -> assigned driver -> transport company, then everything
# that hangs off those three: licence/PDP, enrollment, RC compliance, alerts and
# the operational timeline. It is an AGGREGATE over tables that already exist
# (core.vehicle, core.driver_identity, core.driver, core.pdp, core.transporter*,
# core.vehicle_rc, core.gate_event, core.container_job_assignment) and it reuses
# vehicle_intel() verbatim for the enforcement/telemetry half — no new storage,
# no duplicated records, and the /vehicle-intel contract is untouched.
#
# Why one endpoint rather than four client calls: the spine is CHAINED (you
# cannot look up the driver's licence or the transporter until the vehicle row
# resolves), so composing it in the browser costs three serial round-trips over
# the corridor link. Here the independent lookups run concurrently and only the
# licence/enrollment leg waits on the driver, which is a single extra hop.
# ---------------------------------------------------------------------------

# Plate matching is done SQL-side on both operands: these columns are populated
# by several writers (some upper-only, some punctuation-stripped), so comparing
# normalised-to-normalised is the only form that matches every row.
def _plate_norm(plate: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (plate or "").upper())


def _norm_sql(col: str) -> str:
    return f"regexp_replace(upper(coalesce({col},'')), '[^A-Z0-9]', '', 'g')"


# Days before expiry at which a compliance document starts reading as EXPIRING.
_EXPIRY_WARN_DAYS = 30


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _validity(valid_to: Any) -> Dict[str, Any]:
    """VALID / EXPIRING / EXPIRED / NOT_AVAILABLE for a document expiry date."""
    d = _as_date(valid_to)
    if d is None:
        return {"status": "NOT_AVAILABLE", "valid_to": None}
    today = date.today()
    if d < today:
        status = "EXPIRED"
    elif (d - today).days <= _EXPIRY_WARN_DAYS:
        status = "EXPIRING"
    else:
        status = "VALID"
    return {"status": status, "valid_to": d.isoformat(), "days_left": (d - today).days}


_OPEN_JOB_STATES = ("ASSIGNED", "ACCEPTED", "AT_GATE", "IN_YARD", "PICKED_UP", "DROPPED")


def _empty_360(plate: str) -> dict:
    """Shape-stable envelope so the UI renders "Not Available" rather than
    breaking when the gateway has no database."""
    return {
        "plate": plate,
        "found": False,
        "vehicle": None,
        "driver": None,
        "transporter": None,
        "compliance": {},
        "alerts": [],
        "timeline": [],
        "intel": {"rc": None, "tracking": [], "violations": [],
                  "challans": [], "verification_history": []},
    }


async def vehicle_360(plate: str, *, dsn: Optional[str]) -> dict:
    """Complete operational view of one vehicle: master row, assigned driver,
    licence/PDP, transport company, compliance, alerts and lifecycle timeline."""
    if not dsn:
        return _empty_360(plate)
    from jnpa_shared.db import fetch_all, fetch_one

    norm = _plate_norm(plate)

    async def _vehicle():
        return await fetch_one(
            f"""SELECT vehicle_id, vehicle_no, vehicle_type, chassis_number, rfid_fastag_id,
                       status, created_by, created_at, updated_at
                FROM core.vehicle
                WHERE {_norm_sql('vehicle_no')} = :n OR {_norm_sql('vehicle_id')} = :n
                ORDER BY (status = 'ACTIVE') DESC, created_at DESC
                LIMIT 1""",
            {"n": norm}, dsn=dsn)

    async def _driver():
        # Matched on the plate OR on the master row's vehicle_id, because an
        # enrollment may hold either identifier (sim devices carry TRK-000123).
        return await fetch_one(
            f"""WITH veh AS (
                    SELECT vehicle_id, vehicle_no FROM core.vehicle
                    WHERE {_norm_sql('vehicle_no')} = :n OR {_norm_sql('vehicle_id')} = :n
                    LIMIT 1
                )
                SELECT d.driver_id, d.name, d.license_no, d.mobile, d.vehicle_no, d.status,
                       d.photo_url, d.aadhaar_masked, d.emergency_contact, d.provider,
                       d.enrolled_at, d.updated_at
                FROM core.driver_identity d
                WHERE {_norm_sql('d.vehicle_no')} IN (
                        :n,
                        coalesce((SELECT {_norm_sql('vehicle_id')} FROM veh), :n),
                        coalesce((SELECT {_norm_sql('vehicle_no')} FROM veh), :n))
                ORDER BY (d.status = 'ACTIVE') DESC, d.updated_at DESC
                LIMIT 1""",
            {"n": norm}, dsn=dsn)

    async def _transporter():
        return await fetch_one(
            """SELECT t.id AS transporter_id, t.company_name, t.code, t.status, t.gstin, t.contact,
                      tv.driver_id AS mapped_driver_id, tv.vehicle_no AS mapped_vehicle_no,
                      tv.created_at AS mapped_at,
                      b.reason AS blacklist_reason, b.severity AS blacklist_severity,
                      b.blacklisted_at
               FROM core.transporter_vehicle tv
               JOIN core.transporter t ON t.id = tv.transporter_id
               LEFT JOIN core.transporter_blacklist b
                      ON b.transporter_id = t.id AND b.status = 'ACTIVE'
               WHERE tv.vehicle_no_norm = :n
               ORDER BY b.blacklisted_at DESC NULLS LAST, tv.created_at DESC
               LIMIT 1""",
            {"n": norm}, dsn=dsn)

    async def _gates():
        rows = await fetch_all(
            f"""SELECT ts, gate_id, event_type, trip_id
                FROM core.gate_event
                WHERE {_norm_sql('plate')} = :n
                ORDER BY ts DESC LIMIT 10""",
            {"n": norm}, dsn=dsn)
        return [_row(r) for r in rows]

    async def _jobs():
        rows = await fetch_all(
            f"""SELECT id, container_number, group_code, move_type, status, terminal, gate,
                       document_type, document_reference, driver_id, driver_licence,
                       vehicle_no, vehicle_id, transporter_id,
                       assigned_at, accepted_at, completed_at
                FROM core.container_job_assignment
                WHERE {_norm_sql('vehicle_no')} = :n OR {_norm_sql('vehicle_id')} = :n
                ORDER BY assigned_at DESC LIMIT 10""",
            {"n": norm}, dsn=dsn)
        return [_row(r) for r in rows]

    intel, veh, drv, trn, gates, jobs = await asyncio.gather(
        vehicle_intel(plate, dsn=dsn), _vehicle(), _driver(), _transporter(), _gates(), _jobs(),
        return_exceptions=True,
    )
    intel = _default(intel, {})
    veh = _row(_default(veh, None)) if not isinstance(veh, BaseException) and veh else None
    drv = _row(_default(drv, None)) if not isinstance(drv, BaseException) and drv else None
    trn = _row(_default(trn, None)) if not isinstance(trn, BaseException) and trn else None
    gates = _default(gates, [])
    jobs = _default(jobs, [])

    # Second leg — depends on the driver resolved above, so it cannot be folded
    # into the gather. Licence master + PDP + enrollment + last verification.
    licence: Optional[dict] = None
    enrollment_row: Optional[dict] = None
    verification: Optional[dict] = None
    if drv:
        lic_norm = _plate_norm(str(drv.get("license_no") or ""))

        async def _licence():
            if not lic_norm:
                return None
            return await fetch_one(
                """SELECT dm.licence_number, dm.licence_type, dm.licence_valid_to,
                          dm.latest_pdp_number, dm.date_of_birth, dm.driver_name,
                          dm.company_name, dm.status AS master_status, dm.photo_url,
                          dm.transporter_id,
                          t.company_name AS transporter_name, t.code AS transporter_code,
                          t.status AS transporter_status,
                          p.active AS pdp_active, p.valid_until AS pdp_valid_until
                   FROM core.driver dm
                   LEFT JOIN core.transporter t ON t.id = dm.transporter_id
                   LEFT JOIN LATERAL (
                       SELECT active, valid_until FROM core.pdp
                       WHERE pdp_number = dm.latest_pdp_number
                       ORDER BY accepted_at DESC NULLS LAST LIMIT 1
                   ) p ON true
                   WHERE dm.licence_no_norm = :ln
                   LIMIT 1""",
                {"ln": lic_norm}, dsn=dsn)

        async def _enrollment():
            return await fetch_one(
                """SELECT status, consent, submitted_at, reviewed_at, rejection_reason, source
                   FROM core.driver_enrollment WHERE driver_id = :d LIMIT 1""",
                {"d": drv.get("driver_id")}, dsn=dsn)

        async def _verification():
            return await fetch_one(
                """SELECT decision, score, matched, provider, ts
                   FROM core.verification_log WHERE driver_id = :d
                   ORDER BY ts DESC LIMIT 1""",
                {"d": drv.get("driver_id")}, dsn=dsn)

        lic_r, enr_r, ver_r = await asyncio.gather(
            _licence(), _enrollment(), _verification(), return_exceptions=True)
        licence = _row(lic_r) if not isinstance(lic_r, BaseException) and lic_r else None
        enrollment_row = _row(enr_r) if not isinstance(enr_r, BaseException) and enr_r else None
        verification = _row(ver_r) if not isinstance(ver_r, BaseException) and ver_r else None

    return _shape_360(plate, intel, veh, drv, trn, gates, jobs,
                      licence, enrollment_row, verification)


def _shape_360(plate, intel, veh, drv, trn, gates, jobs,
               licence, enrollment_row, verification) -> dict:
    """Assemble the response. Pure — every value comes from a row read above, and
    a missing row becomes null rather than a fabricated placeholder."""
    rc = intel.get("rc") or {}
    alerts = intel.get("alerts") or []

    open_job = next((j for j in jobs if str(j.get("status")) in _OPEN_JOB_STATES), None)
    if open_job:
        assignment_status = str(open_job.get("status"))
    elif drv:
        assignment_status = "DRIVER_ASSIGNED"
    else:
        assignment_status = "UNASSIGNED"

    # Blacklist is the stricter of the two sources: the RC flag and an ACTIVE
    # transporter blacklist (a clean RC behind a banned operator is still a DENY).
    rc_blacklist = str(rc.get("blacklist_status") or "").strip()
    transporter_banned = bool(trn and trn.get("blacklisted_at"))
    if transporter_banned:
        blacklist = {"status": "BLACKLISTED", "source": "transporter",
                     "reason": trn.get("blacklist_reason"),
                     "severity": trn.get("blacklist_severity"),
                     "since": trn.get("blacklisted_at")}
    elif rc_blacklist and rc_blacklist.upper() not in ("CLEAR", "NONE", "NO", "CLEAN"):
        blacklist = {"status": rc_blacklist.upper(), "source": "rc", "reason": None}
    elif rc_blacklist:
        blacklist = {"status": "CLEAR", "source": "rc", "reason": None}
    else:
        blacklist = {"status": "NOT_AVAILABLE", "source": None, "reason": None}

    pdp_status = "NOT_AVAILABLE"
    if licence:
        pdp_valid = _validity(licence.get("pdp_valid_until") or licence.get("licence_valid_to"))
        if licence.get("pdp_active") is False:
            pdp_status = "CANCELLED"
        elif licence.get("latest_pdp_number") or licence.get("pdp_active"):
            pdp_status = pdp_valid["status"]

    vehicle = {
        "number": (veh or {}).get("vehicle_no") or intel.get("vehicle_number") or plate,
        "id": (veh or {}).get("vehicle_id"),
        "status": (veh or {}).get("status"),
        "class": rc.get("vehicle_class"),
        "fuel": rc.get("fuel_type"),
        "type": (veh or {}).get("vehicle_type"),
        "chassis_number": (veh or {}).get("chassis_number"),
        "rfid_fastag_id": (veh or {}).get("rfid_fastag_id"),
        "registered_at": (veh or {}).get("created_at"),
        "assignment_status": assignment_status,
        "in_master": veh is not None,
    } if (veh or rc or intel.get("vehicle_number")) else None

    driver = {
        "id": drv.get("driver_id"),
        "name": drv.get("name") or (licence or {}).get("driver_name"),
        "photo": drv.get("photo_url") or (licence or {}).get("photo_url"),
        "mobile": drv.get("mobile"),
        "dob": (licence or {}).get("date_of_birth"),
        "status": drv.get("status"),
        "enrollment_status": (enrollment_row or {}).get("status") or (
            "ENROLLED" if drv.get("driver_id") else None),
        "enrolled_at": drv.get("enrolled_at"),
        "license": {
            "number": drv.get("license_no") or (licence or {}).get("licence_number"),
            "type": (licence or {}).get("licence_type"),
            "valid_until": (licence or {}).get("licence_valid_to"),
            "validity": _validity((licence or {}).get("licence_valid_to")),
            "pdp_number": (licence or {}).get("latest_pdp_number"),
            "pdp_status": pdp_status,
            "pdp_valid_until": (licence or {}).get("pdp_valid_until"),
            "verification_status": (verification or {}).get("decision"),
            "verified_at": (verification or {}).get("ts"),
            "verification_score": (verification or {}).get("score"),
            "in_master": licence is not None,
        },
    } if drv else None

    # The vehicle->transporter mapping wins; the driver's own employer is the
    # fallback so a vehicle missing from core.transporter_vehicle still shows a
    # company instead of a blank card.
    if trn:
        transporter = {
            "id": trn.get("transporter_id"),
            "name": trn.get("company_name"),
            "code": trn.get("code"),
            "status": "BLACKLISTED" if transporter_banned else trn.get("status"),
            "gstin": trn.get("gstin"),
            "contact": trn.get("contact"),
            "blacklisted": transporter_banned,
            "blacklist_reason": trn.get("blacklist_reason"),
            "mapped_at": trn.get("mapped_at"),
            "source": "vehicle_mapping",
        }
    elif licence and licence.get("transporter_id"):
        transporter = {
            "id": licence.get("transporter_id"),
            "name": licence.get("transporter_name") or licence.get("company_name"),
            "code": licence.get("transporter_code"),
            "status": licence.get("transporter_status"),
            "gstin": None, "contact": None,
            "blacklisted": licence.get("transporter_status") == "BLACKLISTED",
            "blacklist_reason": None, "mapped_at": None,
            "source": "driver_employer",
        }
    elif licence and licence.get("company_name"):
        transporter = {"id": None, "name": licence.get("company_name"), "code": None,
                       "status": None, "gstin": None, "contact": None,
                       "blacklisted": False, "blacklist_reason": None, "mapped_at": None,
                       "source": "driver_company"}
    else:
        transporter = None

    compliance = {
        "rc": {"status": "ON_RECORD" if rc else "NOT_AVAILABLE",
               "registration_date": rc.get("registration_date"),
               "rc_type": rc.get("rc_type"),
               "rto_code": rc.get("rto_code"),
               "state": rc.get("state"),
               "owner": rc.get("owner_name_masked"),
               "provisional": rc.get("provisional")},
        "insurance": _validity(rc.get("insurance_valid_to")),
        "puc": _validity(rc.get("puc_valid_to")),
        "fitness": _validity(rc.get("fitness_valid_to")),
        "blacklist": blacklist,
        "fastag": {"status": rc.get("fastag_status") or "NOT_AVAILABLE"},
    }

    return {
        "plate": plate,
        "found": bool(veh or rc or drv or trn),
        "vehicle": vehicle,
        "driver": driver,
        "transporter": transporter,
        "compliance": compliance,
        "alerts": alerts,
        "timeline": _build_360_timeline(veh, drv, trn, enrollment_row, gates, jobs,
                                        assignment_status),
        # The enforcement/telemetry half, unchanged from /vehicle-intel so the
        # existing panels keep working off one response.
        "intel": {
            "rc": intel.get("rc"),
            "tracking": intel.get("tracking") or [],
            "violations": intel.get("violations") or [],
            "challans": intel.get("challans") or [],
            "verification_history": intel.get("verification_history") or [],
        },
        "jobs": jobs,
        "gate_events": gates,
    }


def _build_360_timeline(veh, drv, trn, enrollment_row, gates, jobs, assignment_status) -> List[dict]:
    """Vehicle lifecycle, oldest first. Only stages with a real timestamp are
    emitted — an absent stage is simply not in the list."""
    out: List[dict] = []

    def add(stage: str, label: str, ts: Any, detail: Optional[str] = None) -> None:
        if ts:
            out.append({"stage": stage, "label": label, "ts": ts, "detail": detail})

    if veh:
        add("VEHICLE_REGISTERED", "Vehicle Registered", veh.get("created_at"),
            veh.get("vehicle_id"))
    if drv:
        add("DRIVER_ENROLLED", "Driver Enrolled",
            (enrollment_row or {}).get("submitted_at") or drv.get("enrolled_at"),
            drv.get("name"))
    if trn:
        add("TRANSPORTER_MAPPED", "Transporter Mapped", trn.get("mapped_at"),
            trn.get("company_name"))
    for j in jobs:
        add("JOB_ASSIGNED", "Driver Assigned to Job", j.get("assigned_at"),
            " · ".join(str(x) for x in (j.get("move_type"), j.get("container_number")) if x))
    for g in gates:
        add("GATE_EVENT", _GATE_LABELS.get(str(g.get("event_type")), "Gate Event"),
            g.get("ts"), g.get("gate_id"))
    for j in jobs:
        add("CARGO_MOVEMENT", "Cargo Movement", j.get("completed_at") or j.get("accepted_at"),
            " · ".join(str(x) for x in (j.get("status"), j.get("terminal")) if x))

    out.sort(key=lambda e: str(e["ts"]))
    out.append({"stage": "CURRENT_STATUS", "label": "Current Status", "ts": None,
                "detail": assignment_status})
    return out


_GATE_LABELS = {
    "GATE_ARRIVAL": "Gate Arrival",
    "GATE_TXN_START": "Gate Transaction Started",
    "GATE_IN": "Gate Entry",
    "GATE_OUT": "Gate Exit",
}


async def driver_intel(driver_key: str, *, dsn: Optional[str]) -> dict:
    """Driver profile + DL history + vehicle assignment + violations + activity."""
    if not dsn:
        return {}
    from jnpa_shared.db import fetch_all, fetch_one

    out: Dict[str, Any] = {"driver_key": driver_key}
    try:
        drv = await fetch_one(
            "SELECT * FROM core.driver_identity WHERE driver_id = :k OR license_no = :k",
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
            "SELECT status, source, response_payload, created_at FROM core.driver_license_lookup_history WHERE dl_number = :d ORDER BY created_at DESC LIMIT 10",
            {"d": dl_no}, dsn=dsn)
        return [_row(r) for r in rows]

    async def _activity():
        rows = await fetch_all(
            "SELECT decision, score, ts FROM core.verification_log WHERE driver_id = :k ORDER BY ts DESC LIMIT 20",
            {"k": driver_id}, dsn=dsn)
        return [_row(r) for r in rows]

    async def _violations():
        if not vehicle_no:
            return []
        rows = await fetch_all(
            "SELECT case_id, status, total_fine FROM core.violation_case WHERE vehicle_number = :p ORDER BY first_detected_at DESC LIMIT 20",
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
        "SELECT id, vehicle_number, verification_status, source, created_at FROM core.vehicle_verification_history ORDER BY created_at DESC LIMIT :l",
        {"l": max(1, min(int(limit), 1000))}, dsn=dsn)
    return [_row(r) for r in rows]


async def dl_history(*, limit: int, dsn: Optional[str]) -> List[dict]:
    if not dsn:
        return []
    from jnpa_shared.db import fetch_all

    rows = await fetch_all(
        "SELECT id, dl_number, status, source, created_at FROM core.driver_license_lookup_history ORDER BY created_at DESC LIMIT :l",
        {"l": max(1, min(int(limit), 1000))}, dsn=dsn)
    return [_row(r) for r in rows]


__all__ = [
    "ensure_intel_schema", "record_vehicle_verification", "record_dl_lookup",
    "upsert_driver_from_dl", "dl_status", "vehicle_intel", "vehicle_360", "driver_intel",
    "verification_history", "dl_history",
]
