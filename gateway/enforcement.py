"""Enforcement system-of-record: cases, challans, lifecycle, audit.

The ADDITIVE layer that turns the detection/reporting platform into a traffic
enforcement system of record. It owns three NEW tables (never touches existing
ones) and the rules that bind them:

    core.violation_case  — one row per case; the lifecycle anchor.
    core.challan         — immutable-after-issue, sequenced legal record.
    core.case_audit       — hash-chained, append-only transition log.

Plus:
  * a state machine (DETECTED → REVIEWED → CONFIRMED → CHALLAN_ISSUED → PAID →
    CLOSED) that rejects illegal jumps;
  * a backward-compatible fine engine (the reports._CHALLAN base schedule with
    optional zone / vehicle-class / time multipliers — defaults to the flat base);
  * SHA-256 evidence hashing + per-transition hash chaining for tamper-evidence.

Schema is idempotent (CREATE … IF NOT EXISTS) and applied lazily so an existing
volume gains the tables without an init.sql re-run — mirroring gateway/enrollment.
Nothing here imports or alters the ANPR / identity / model code.
"""
from __future__ import annotations

import os

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .logging import get_logger
# The base fine schedule is the single source of truth, reused untouched.
from .routers.reports import _CHALLAN

log = get_logger("gateway.enforcement")

IST = timezone(timedelta(hours=5, minutes=30))

# --- lifecycle --------------------------------------------------------------
CASE_STATES = ("DETECTED", "REVIEWED", "CONFIRMED", "CHALLAN_ISSUED", "PAID", "CLOSED")
# Canonical forward order used by advance_to() to walk one valid step at a time.
_CANON = list(CASE_STATES)
# Legal transitions (a case may always be CLOSED; PAID only after a challan).
_ALLOWED: Dict[str, set] = {
    "DETECTED": {"REVIEWED", "CLOSED"},
    "REVIEWED": {"CONFIRMED", "DETECTED", "CLOSED"},
    "CONFIRMED": {"CHALLAN_ISSUED", "CLOSED"},
    "CHALLAN_ISSUED": {"PAID", "DISPUTED", "CLOSED"},
    "PAID": {"CLOSED"},
    "DISPUTED": {"CHALLAN_ISSUED", "CLOSED"},
    "CLOSED": set(),
}
CHALLAN_STATES = ("ISSUED", "PAID", "DISPUTED", "CLOSED")


def can_transition(frm: str, to: str) -> bool:
    return to in _ALLOWED.get(frm, set())


class InvalidTransition(ValueError):
    """Raised when a requested case transition is not permitted."""


# --- schema (idempotent; lazily applied) ------------------------------------
_DDL = """
CREATE SCHEMA IF NOT EXISTS core;
CREATE SEQUENCE IF NOT EXISTS core.challan_seq START 1001;
CREATE TABLE IF NOT EXISTS core.violation_case (
    case_id           uuid PRIMARY KEY,
    vehicle_number    text,
    driver_id         text,
    first_detected_at timestamptz NOT NULL DEFAULT now(),
    last_updated_at   timestamptz NOT NULL DEFAULT now(),
    status            text NOT NULL DEFAULT 'DETECTED'
                      CHECK (status IN ('DETECTED','REVIEWED','CONFIRMED',
                                        'CHALLAN_ISSUED','PAID','CLOSED')),
    total_fine        integer NOT NULL DEFAULT 0,
    evidence_url      text,
    evidence_sha256   text,
    gate_id           text,
    confidence        double precision
);
CREATE INDEX IF NOT EXISTS idx_violation_cases_status
    ON core.violation_case (status, last_updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_violation_cases_plate
    ON core.violation_case (vehicle_number, first_detected_at DESC);
CREATE TABLE IF NOT EXISTS core.challan (
    challan_id      uuid PRIMARY KEY,
    challan_no      text UNIQUE,
    case_id         uuid NOT NULL UNIQUE,
    vehicle_number  text,
    total_fine      integer NOT NULL DEFAULT 0,
    status          text NOT NULL DEFAULT 'ISSUED'
                    CHECK (status IN ('ISSUED','PAID','DISPUTED','CLOSED')),
    mva_section     text,
    issued_at       timestamptz NOT NULL DEFAULT now(),
    payment_ref     text,
    pdf_url         text,
    evidence_sha256 text,
    created_by      text
);
CREATE INDEX IF NOT EXISTS idx_challans_case ON core.challan (case_id);
CREATE TABLE IF NOT EXISTS core.case_audit (
    id          bigserial PRIMARY KEY,
    case_id     uuid NOT NULL,
    event       text NOT NULL,
    from_status text,
    to_status   text,
    actor       text,
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    prev_hash   text,
    hash        text,
    ts          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_case_audit_case ON core.case_audit (case_id, id);
"""
# Defence-in-depth idempotency: at most one console-issued alert per (case, kind).
# A partial unique index, so non-enforcement alerts (anomaly, customs, …) are
# completely unaffected. Created separately because it references core.alert.
_ALERTS_DEDUP_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_alerts_case_kind "
    "ON core.alert ((payload->>'case_id'), kind) "
    "WHERE payload->>'source' = 'violation-console'"
)

_SCHEMA_READY: Dict[str, bool] = {}


async def ensure_schema(dsn: str) -> None:
    """Apply the idempotent enforcement DDL once per DSN (best-effort cached)."""
    if os.getenv("JNPA_RUNTIME_DDL", "0") != "1":
        # schema-v3: DDL is owned by infra/postgres/v3 migrations, never runtime.
        return
    if _SCHEMA_READY.get(dsn):
        return
    from jnpa_shared.db import execute  # lazy import

    for stmt in (s.strip() for s in _DDL.split(";")):
        if stmt:
            await execute(stmt, dsn=dsn)
    # The dedup index is best-effort: if legacy duplicate rows exist it may fail
    # to build — the in-app pre-check still guarantees idempotency, so we don't
    # let that abort case/challan creation.
    try:
        await execute(_ALERTS_DEDUP_DDL, dsn=dsn)
    except Exception as exc:  # noqa: BLE001
        log.warning("alerts_dedup_index_skipped", error=str(exc))
    _SCHEMA_READY[dsn] = True


# --- evidence integrity -----------------------------------------------------
def evidence_sha256(data: bytes) -> str:
    """SHA-256 of the captured evidence bytes (stored with the MinIO URL)."""
    return hashlib.sha256(data).hexdigest()


# --- fine engine (backward compatible) --------------------------------------
# Multipliers default to 1.0, so with no context the fine equals the flat base
# in reports._CHALLAN — existing behaviour is preserved exactly.
_VEHICLE_MULT = {"HGV": 1.5, "TANKER": 1.5, "MGV": 1.25, "BUS": 1.25, "LGV": 1.0, "CAR": 1.0}
_ZONE_MULT = {"restricted": 1.5, "no_parking": 1.0}
_NIGHT_MULT = 1.25  # 22:00–06:00 IST


def base_fine(kind: str) -> int:
    return int((_CHALLAN.get(kind) or {}).get("fine_inr") or 0)


def mva_section(kind: str) -> Optional[str]:
    return (_CHALLAN.get(kind) or {}).get("section")


def compute_fine(
    kind: str,
    *,
    vehicle_class: Optional[str] = None,
    zone_kind: Optional[str] = None,
    at: Optional[datetime] = None,
) -> Tuple[int, dict]:
    """Return (fine, breakdown). Multipliers apply only when context is given."""
    base = base_fine(kind)
    mult = 1.0
    factors: Dict[str, Any] = {}

    vc = (vehicle_class or "").upper()
    if vc in _VEHICLE_MULT and _VEHICLE_MULT[vc] != 1.0:
        mult *= _VEHICLE_MULT[vc]
        factors["vehicle_class"] = {"class": vc, "x": _VEHICLE_MULT[vc]}

    if zone_kind in _ZONE_MULT and _ZONE_MULT[zone_kind] != 1.0:
        mult *= _ZONE_MULT[zone_kind]
        factors["zone"] = {"kind": zone_kind, "x": _ZONE_MULT[zone_kind]}

    hour = (at or datetime.now(timezone.utc)).astimezone(IST).hour
    if hour >= 22 or hour < 6:
        mult *= _NIGHT_MULT
        factors["time"] = {"band": "night", "x": _NIGHT_MULT}

    fine = int(round(base * mult))
    return fine, {"base": base, "multiplier": round(mult, 3), "factors": factors}


# --- audit (hash-chained, append-only) --------------------------------------
async def _audit(
    dsn: str, case_id: str, event: str, *,
    from_status: Optional[str] = None, to_status: Optional[str] = None,
    actor: Optional[str] = None, detail: Optional[dict] = None,
) -> str:
    """Append a tamper-evident audit row; returns the new chain hash."""
    from jnpa_shared.db import execute, fetch_one

    prev = await fetch_one(
        "SELECT hash FROM core.case_audit WHERE case_id = CAST(:c AS uuid) "
        "ORDER BY id DESC LIMIT 1",
        {"c": case_id}, dsn=dsn,
    )
    prev_hash = (prev or {}).get("hash")
    stamp = datetime.now(timezone.utc).isoformat()
    body = json.dumps(
        {"event": event, "from": from_status, "to": to_status, "actor": actor,
         "detail": detail or {}, "at": stamp},
        sort_keys=True, separators=(",", ":"),
    )
    chain = hashlib.sha256(((prev_hash or "") + body).encode()).hexdigest()
    await execute(
        """
        INSERT INTO core.case_audit
            (case_id, event, from_status, to_status, actor, detail, prev_hash, hash)
        VALUES (CAST(:c AS uuid), :event, :frm, :to, :actor,
                CAST(:detail AS jsonb), :prev, :hash)
        """,
        {"c": case_id, "event": event, "frm": from_status, "to": to_status,
         "actor": actor, "detail": json.dumps({**(detail or {}), "at": stamp}),
         "prev": prev_hash, "hash": chain},
        dsn=dsn,
    )
    return chain


# --- case operations --------------------------------------------------------
async def open_or_get_case(
    dsn: str, case_id: str, *,
    vehicle_number: Optional[str], driver_id: Optional[str],
    gate_id: Optional[str], evidence_url: Optional[str],
    evidence_sha256: Optional[str], confidence: Optional[float], actor: str,
) -> dict:
    """Insert the case at DETECTED if new (audited), else return the existing row."""
    from jnpa_shared.db import execute, fetch_one

    await execute(
        """
        INSERT INTO core.violation_case
            (case_id, vehicle_number, driver_id, gate_id, evidence_url,
             evidence_sha256, confidence, status)
        VALUES (CAST(:c AS uuid), :veh, :drv, :gate, :ev, :sha, :conf, 'DETECTED')
        ON CONFLICT (case_id) DO NOTHING
        """,
        {"c": case_id, "veh": vehicle_number, "drv": driver_id, "gate": gate_id,
         "ev": evidence_url, "sha": evidence_sha256, "conf": confidence},
        dsn=dsn,
    )
    row = await fetch_one(
        "SELECT * FROM core.violation_case WHERE case_id = CAST(:c AS uuid)",
        {"c": case_id}, dsn=dsn,
    )
    created = bool(row) and row.get("status") == "DETECTED" and row.get("total_fine", 0) == 0
    # Audit CASE_OPENED only on genuine creation (first_detected within this call):
    # detect via a marker row absence is unreliable, so we check the audit log.
    has_open = await fetch_one(
        "SELECT 1 AS x FROM core.case_audit WHERE case_id = CAST(:c AS uuid) "
        "AND event = 'CASE_OPENED' LIMIT 1",
        {"c": case_id}, dsn=dsn,
    )
    if not has_open:
        await _audit(dsn, case_id, "CASE_OPENED", to_status="DETECTED", actor=actor,
                     detail={"vehicle_number": vehicle_number, "driver_id": driver_id})
    return dict(row) if row else {}


async def existing_violations(dsn: str, case_id: str) -> Dict[str, dict]:
    """{kind: {id, fine}} already recorded for this case (idempotency source)."""
    from jnpa_shared.db import fetch_all

    rows = await fetch_all(
        """
        SELECT kind, id::text AS id,
               COALESCE((payload->>'fine_inr')::int, 0) AS fine
        FROM core.alert
        WHERE payload->>'case_id' = :c AND payload->>'source' = 'violation-console'
        """,
        {"c": case_id}, dsn=dsn,
    )
    return {r["kind"]: {"id": r["id"], "fine": r["fine"]} for r in rows}


async def insert_violation_alert(
    dsn: str, *, alert_id: str, case_id: str, kind: str, severity: str,
    gate_id: Optional[str], plate: Optional[str], payload: dict,
) -> bool:
    """Insert one core.alert row (backward-compatible). False if it already
    existed (unique-index race) — caller treats that as an idempotent skip."""
    from jnpa_shared.db import execute

    try:
        await execute(
            """
            INSERT INTO core.alert (id, ts, kind, severity, gate_id, plate, payload, ack)
            VALUES (CAST(:id AS uuid), now(), :kind, :severity, :gate, :plate,
                    CAST(:payload AS jsonb), false)
            """,
            {"id": alert_id, "kind": kind, "severity": severity, "gate": gate_id,
             "plate": plate, "payload": json.dumps(payload)},
            dsn=dsn,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        # Unique-index hit (concurrent duplicate) -> idempotent skip, not an error.
        if "uq_alerts_case_kind" in str(exc):
            log.info("violation_alert_dedup_skip", case_id=case_id, kind=kind)
            return False
        raise


async def advance_to(dsn: str, case_id: str, target: str, *, actor: str) -> str:
    """Walk the case forward one valid step at a time to `target` (audited).

    Each hop is validated against the state machine, so no illegal jump can be
    recorded. Returns the case's final status. Idempotent if already at/after
    target on the canonical chain.
    """
    from jnpa_shared.db import execute, fetch_one

    row = await fetch_one(
        "SELECT status FROM core.violation_case WHERE case_id = CAST(:c AS uuid)",
        {"c": case_id}, dsn=dsn,
    )
    if not row:
        raise InvalidTransition(f"case {case_id} not found")
    cur = row["status"]
    if cur not in _CANON or target not in _CANON:
        raise InvalidTransition(f"non-canonical advance {cur} -> {target}")
    while _CANON.index(cur) < _CANON.index(target):
        nxt = _CANON[_CANON.index(cur) + 1]
        if not can_transition(cur, nxt):
            raise InvalidTransition(f"{cur} -> {nxt}")
        await execute(
            "UPDATE core.violation_case SET status = :s, last_updated_at = now() "
            "WHERE case_id = CAST(:c AS uuid)",
            {"s": nxt, "c": case_id}, dsn=dsn,
        )
        await _audit(dsn, case_id, "TRANSITION", from_status=cur, to_status=nxt, actor=actor)
        cur = nxt
    return cur


async def set_case_totals(
    dsn: str, case_id: str, *, total_fine: int,
    vehicle_number: Optional[str], driver_id: Optional[str],
    evidence_url: Optional[str], evidence_sha256: Optional[str],
    gate_id: Optional[str], confidence: Optional[float],
) -> None:
    """Refresh aggregate fields without overwriting set values with NULLs."""
    from jnpa_shared.db import execute

    await execute(
        """
        UPDATE core.violation_case SET
            total_fine      = :total,
            vehicle_number  = COALESCE(vehicle_number, :veh),
            driver_id       = COALESCE(driver_id, :drv),
            evidence_url    = COALESCE(evidence_url, :ev),
            evidence_sha256 = COALESCE(evidence_sha256, :sha),
            gate_id         = COALESCE(gate_id, :gate),
            confidence      = COALESCE(confidence, :conf),
            last_updated_at = now()
        WHERE case_id = CAST(:c AS uuid)
        """,
        {"total": total_fine, "veh": vehicle_number, "drv": driver_id,
         "ev": evidence_url, "sha": evidence_sha256, "gate": gate_id,
         "conf": confidence, "c": case_id},
        dsn=dsn,
    )


async def issue_challan(
    dsn: str, case_id: str, *, vehicle_number: Optional[str], total_fine: int,
    mva_section: Optional[str], pdf_url: Optional[str],
    evidence_sha256: Optional[str], actor: str,
) -> dict:
    """Create the case's challan once (immutable after issue); return it.

    ONE CASE → ONE CHALLAN: case_id is UNIQUE, so a repeat call returns the
    existing challan rather than minting a second. Core fields are never updated
    afterwards (only status/payment advance via transitions).
    """
    import uuid as _uuid

    from jnpa_shared.db import execute, fetch_one

    existing = await fetch_one(
        "SELECT * FROM core.challan WHERE case_id = CAST(:c AS uuid)",
        {"c": case_id}, dsn=dsn,
    )
    if existing:
        return dict(existing)

    challan_id = str(_uuid.uuid4())
    await execute(
        """
        INSERT INTO core.challan
            (challan_id, challan_no, case_id, vehicle_number, total_fine, status,
             mva_section, issued_at, pdf_url, evidence_sha256, created_by)
        VALUES (CAST(:cid AS uuid),
                'ECH-' || to_char(now(), 'YYYY') || '-' ||
                    lpad(nextval('core.challan_seq')::text, 6, '0'),
                CAST(:case AS uuid), :veh, :fine, 'ISSUED', :sec, now(),
                :pdf, :sha, :by)
        ON CONFLICT (case_id) DO NOTHING
        """,
        {"cid": challan_id, "case": case_id, "veh": vehicle_number, "fine": total_fine,
         "sec": mva_section, "pdf": pdf_url, "sha": evidence_sha256, "by": actor},
        dsn=dsn,
    )
    row = await fetch_one(
        "SELECT * FROM core.challan WHERE case_id = CAST(:c AS uuid)",
        {"c": case_id}, dsn=dsn,
    )
    await _audit(dsn, case_id, "CHALLAN_ISSUED", to_status="CHALLAN_ISSUED", actor=actor,
                 detail={"challan_no": (row or {}).get("challan_no"), "total_fine": total_fine})
    return dict(row) if row else {}


async def transition_case(
    dsn: str, case_id: str, to_status: str, *, actor: str,
    payment_ref: Optional[str] = None,
) -> dict:
    """Apply a single explicit, validated case transition (REST surface).

    Mirrors the challan side-effects: → PAID marks the challan PAID (+payment_ref),
    → CLOSED closes the challan, → DISPUTED disputes it. Raises InvalidTransition
    on an illegal hop.
    """
    from jnpa_shared.db import execute, fetch_one

    row = await fetch_one(
        "SELECT status FROM core.violation_case WHERE case_id = CAST(:c AS uuid)",
        {"c": case_id}, dsn=dsn,
    )
    if not row:
        raise InvalidTransition(f"case {case_id} not found")
    cur = row["status"]
    if not can_transition(cur, to_status):
        raise InvalidTransition(f"{cur} -> {to_status}")

    await execute(
        "UPDATE core.violation_case SET status = :s, last_updated_at = now() "
        "WHERE case_id = CAST(:c AS uuid)",
        {"s": to_status, "c": case_id}, dsn=dsn,
    )
    # Reflect onto the (otherwise immutable) challan's status + payment_ref only.
    if to_status in ("PAID", "CLOSED", "DISPUTED"):
        await execute(
            "UPDATE core.challan SET status = :s, "
            "payment_ref = COALESCE(:pref, payment_ref) "
            "WHERE case_id = CAST(:c AS uuid)",
            {"s": to_status, "pref": payment_ref, "c": case_id}, dsn=dsn,
        )
    await _audit(dsn, case_id, "TRANSITION", from_status=cur, to_status=to_status,
                 actor=actor, detail={"payment_ref": payment_ref} if payment_ref else None)
    return await get_case_bundle(dsn, case_id)


async def get_case_bundle(dsn: str, case_id: str) -> dict:
    """Full case view: case + its violation alerts + challan + audit timeline."""
    from jnpa_shared.db import fetch_all, fetch_one

    case = await fetch_one(
        "SELECT * FROM core.violation_case WHERE case_id = CAST(:c AS uuid)",
        {"c": case_id}, dsn=dsn,
    )
    if not case:
        return {}
    violations = await fetch_all(
        """
        SELECT id::text AS id, kind, severity, ts,
               COALESCE((payload->>'fine_inr')::int, 0) AS fine_inr,
               payload->>'section' AS section
        FROM core.alert
        WHERE payload->>'case_id' = :c AND payload->>'source' = 'violation-console'
        ORDER BY ts
        """,
        {"c": case_id}, dsn=dsn,
    )
    challan = await fetch_one(
        "SELECT * FROM core.challan WHERE case_id = CAST(:c AS uuid)",
        {"c": case_id}, dsn=dsn,
    )
    audit = await fetch_all(
        "SELECT event, from_status, to_status, actor, ts, hash "
        "FROM core.case_audit WHERE case_id = CAST(:c AS uuid) ORDER BY id",
        {"c": case_id}, dsn=dsn,
    )
    return {
        "case": _iso(dict(case)),
        "violations": [_iso(dict(v)) for v in violations],
        "challan": _iso(dict(challan)) if challan else None,
        "audit": [_iso(dict(a)) for a in audit],
    }


def _iso(d: Mapping[str, Any]) -> dict:
    out = dict(d)
    for k, v in list(out.items()):
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif hasattr(v, "hex") and v.__class__.__name__ == "UUID":
            out[k] = str(v)
    return out
