"""RDS persistence for the gate/customs domain (Phase 2).

Makes e-Seal / Form-13 / Weighbridge / ICEGATE captures and Auto-LEO
reconciliations durable in Postgres (the single source of truth), and writes
customs flags to ``core.alert`` so the customs feed survives a restart and is
queryable for audit / reporting.

Deliberately lives INSIDE the gate-data service (not the shared package) so it
ships via the bind-mounted service code and does NOT touch the validated audit
persistence layer. It builds on the installed ``jnpa_shared.db`` helpers (the
same engine every service already uses), so no new dependency is introduced.

Every writer is best-effort: a DB blip must never break capture/serve. The DDL
is idempotent (mirrors migration 0004) and applied at service boot.
"""
from __future__ import annotations

import os

import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from jnpa_shared.logging import get_logger

log = get_logger("gate_data.persistence")

# Stable namespace so a given (container, flag) always maps to the same alert id
# -> ON CONFLICT (id) DO NOTHING makes customs-alert writes idempotent on restart.
_ALERT_NS = uuid.UUID("6f1b2c3d-4e5a-6b7c-8d9e-0a1b2c3d4e5f")

_DDL = """
CREATE SCHEMA IF NOT EXISTS core;
CREATE TABLE IF NOT EXISTS core.gate_capture (
    id            bigserial PRIMARY KEY,
    capture_type  text NOT NULL
                  CHECK (capture_type IN ('ESEAL','FORM13','WEIGHBRIDGE','ICEGATE')),
    container_no  text,
    vehicle_plate text,
    gate_id       text,
    source_mode   text NOT NULL DEFAULT 'sim',
    status        text,
    captured_at   timestamptz,
    payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (container_no, capture_type, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_gate_captures_container ON core.gate_capture (container_no);
CREATE INDEX IF NOT EXISTS idx_gate_captures_plate     ON core.gate_capture (vehicle_plate);
CREATE INDEX IF NOT EXISTS idx_gate_captures_type_ts   ON core.gate_capture (capture_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gate_captures_ts        ON core.gate_capture (created_at DESC);
CREATE TABLE IF NOT EXISTS core.leo_reconciliation (
    id             bigserial PRIMARY KEY,
    container_no   text,
    vehicle_plate  text,
    leo_ready      boolean NOT NULL DEFAULT false,
    customs_flags  jsonb NOT NULL DEFAULT '[]'::jsonb,
    checks         jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_mode    text NOT NULL DEFAULT 'sim',
    reconciled_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_leo_recon_container ON core.leo_reconciliation (container_no, reconciled_at DESC);
CREATE INDEX IF NOT EXISTS idx_leo_recon_ready     ON core.leo_reconciliation (leo_ready, reconciled_at DESC);
CREATE INDEX IF NOT EXISTS idx_leo_recon_ts        ON core.leo_reconciliation (reconciled_at DESC);
"""

_SCHEMA_READY: Dict[str, bool] = {}


def _j(value: Any) -> str:
    try:
        return json.dumps(value if value is not None else {}, default=str)
    except Exception:  # noqa: BLE001
        return json.dumps({"_unserializable": str(value)})


def _parse_ts(value: Any) -> Optional[datetime]:
    """ISO-8601 string / datetime -> datetime (asyncpg needs a datetime for a
    timestamptz bind, not a str). None on failure -> DB default / NULL."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return None
    return None


async def ensure_gate_schema(dsn: Optional[str]) -> None:
    """Apply the idempotent gate/customs DDL once per DSN (best-effort, cached)."""
    if os.getenv("JNPA_RUNTIME_DDL", "0") != "1":
        # schema-v3: DDL is owned by infra/postgres/v3 migrations, never runtime.
        return
    if not dsn or _SCHEMA_READY.get(dsn):
        return
    from jnpa_shared.db import execute

    for stmt in (s.strip() for s in _DDL.split(";")):
        if stmt:
            try:
                await execute(stmt, dsn=dsn)
            except Exception as exc:  # noqa: BLE001
                log.warning("gate_ddl_stmt_skipped", error=str(exc), stmt=stmt[:60])
    _SCHEMA_READY[dsn] = True
    log.info("gate_schema_ready")


# --- capture persistence ----------------------------------------------------
async def upsert_capture(
    *,
    capture_type: str,
    container_no: Optional[str],
    vehicle_plate: Optional[str],
    gate_id: Optional[str] = None,
    source_mode: str = "sim",
    status: Optional[str] = None,
    captured_at: Any = None,
    payload: Any = None,
    dsn: Optional[str] = None,
) -> None:
    """Persist one captured source record (idempotent on container+type+captured_at)."""
    if not dsn:
        return
    from jnpa_shared.db import execute

    try:
        await execute(
            """
            INSERT INTO core.gate_capture
                (capture_type, container_no, vehicle_plate, gate_id, source_mode,
                 status, captured_at, payload)
            VALUES
                (:capture_type, :container_no, :vehicle_plate, :gate_id, :source_mode,
                 :status, :captured_at, CAST(:payload AS jsonb))
            ON CONFLICT (container_no, capture_type, captured_at) DO UPDATE SET
                status = EXCLUDED.status,
                payload = EXCLUDED.payload,
                source_mode = EXCLUDED.source_mode
            """,
            {
                "capture_type": capture_type,
                "container_no": container_no,
                "vehicle_plate": vehicle_plate,
                "gate_id": gate_id,
                "source_mode": source_mode,
                "status": status,
                "captured_at": _parse_ts(captured_at),
                "payload": _j(payload),
            },
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("gate_capture_write_failed", type=capture_type, error=str(exc))


async def record_reconciliation(
    *,
    container_no: str,
    vehicle_plate: Optional[str],
    leo_ready: bool,
    customs_flags: List[str],
    checks: Dict[str, Any],
    source_mode: str = "sim",
    dsn: Optional[str] = None,
) -> None:
    """Persist one Auto-LEO reconciliation outcome."""
    if not dsn:
        return
    from jnpa_shared.db import execute

    try:
        await execute(
            """
            INSERT INTO core.leo_reconciliation
                (container_no, vehicle_plate, leo_ready, customs_flags, checks, source_mode)
            VALUES
                (:container_no, :vehicle_plate, :leo_ready,
                 CAST(:customs_flags AS jsonb), CAST(:checks AS jsonb), :source_mode)
            """,
            {
                "container_no": container_no,
                "vehicle_plate": vehicle_plate,
                "leo_ready": leo_ready,
                "customs_flags": _j(customs_flags),
                "checks": _j(checks),
                "source_mode": source_mode,
            },
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("leo_recon_write_failed", container=container_no, error=str(exc))


async def raise_customs_alert(
    *,
    flag: str,
    severity: str,
    container_no: str,
    vehicle_plate: Optional[str],
    payload: Dict[str, Any],
    dsn: Optional[str] = None,
) -> None:
    """Write a durable CUSTOMS_FLAG row to core.alert (idempotent per container+flag).

    Uses a deterministic uuid5(container|flag) as the alert id so re-reconciling
    the same corpus on restart does not duplicate the customs feed. The gateway
    alert pump mirrors this into core.digital_twin_event automatically.
    """
    if not dsn:
        return
    from jnpa_shared.db import execute

    alert_id = str(uuid.uuid5(_ALERT_NS, f"{container_no}|{flag}"))
    body = {"source": "gate-data", "container_no": container_no,
            "vehicle_plate": vehicle_plate, "flag": flag, **payload}
    try:
        # Durable customs feed (reports/police read core.alert). Idempotent.
        rc = await execute(
            """
            INSERT INTO core.alert (id, kind, severity, plate, payload)
            VALUES (CAST(:id AS uuid), 'CUSTOMS_FLAG', :severity, :plate,
                    CAST(:payload AS jsonb))
            ON CONFLICT (id) DO NOTHING
            """,
            {"id": alert_id, "severity": severity, "plate": vehicle_plate,
             "payload": _j(body)},
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("customs_alert_write_failed", flag=flag, error=str(exc))
        return
    # Direct alert inserts bypass the gateway Kafka pump, so mirror into the
    # unified event timeline here (idempotent via the same deterministic id).
    try:
        await execute(
            """
            INSERT INTO core.digital_twin_event
                (event_type, vehicle_id, location, payload)
            SELECT 'CUSTOMS_ALERT', :plate, CAST(:loc AS jsonb), CAST(:payload AS jsonb)
            WHERE NOT EXISTS (
                SELECT 1 FROM core.digital_twin_event
                WHERE event_type = 'CUSTOMS_ALERT'
                  AND payload->>'alert_id' = :alert_id
            )
            """,
            {"plate": vehicle_plate, "loc": _j({"container_no": container_no}),
             "payload": _j({"alert_id": alert_id, "kind": "CUSTOMS_FLAG",
                            "severity": severity, **body}),
             "alert_id": alert_id},
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("customs_event_mirror_failed", flag=flag, error=str(exc))


async def log_api_audit(
    *,
    service_name: str,
    endpoint: Optional[str] = None,
    method: Optional[str] = None,
    request_payload: Any = None,
    response_payload: Any = None,
    status_code: Optional[int] = None,
    latency_ms: Optional[float] = None,
    error: Optional[str] = None,
    transaction_id: Optional[str] = None,
    dsn: Optional[str] = None,
) -> None:
    """Log a deepest-hop external call (LIVE provider) to core.api_audit_log.

    Same table/shape the gateway middleware uses; this captures the gate-data ->
    vendor hop that the gateway cannot see. No-op until a LIVE endpoint is set.
    """
    if not dsn:
        return
    from jnpa_shared.db import execute

    try:
        await execute(
            """
            INSERT INTO core.api_audit_log
                (service_name, endpoint, method, request_payload, response_payload,
                 status_code, latency_ms, error, transaction_id)
            VALUES
                (:service_name, :endpoint, :method, CAST(:req AS jsonb),
                 CAST(:resp AS jsonb), :status_code, :latency_ms, :error, :txn)
            """,
            {"service_name": service_name, "endpoint": endpoint, "method": method,
             "req": _j(request_payload), "resp": _j(response_payload),
             "status_code": status_code, "latency_ms": latency_ms, "error": error,
             "txn": transaction_id},
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("gate_api_audit_write_failed", service=service_name, error=str(exc))


# --- read paths (RDS-backed, for the dashboards) ----------------------------
async def recent_captures(
    *, capture_type: Optional[str] = None, container_no: Optional[str] = None,
    limit: int = 100, dsn: Optional[str] = None,
) -> List[dict]:
    if not dsn:
        return []
    from jnpa_shared.db import fetch_all

    where = []
    params: Dict[str, Any] = {"limit": max(1, min(int(limit), 1000))}
    if capture_type:
        where.append("capture_type = :ct")
        params["ct"] = capture_type.upper()
    if container_no:
        where.append("container_no = :cn")
        params["cn"] = container_no
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    try:
        rows = await fetch_all(
            f"""
            SELECT id, capture_type, container_no, vehicle_plate, gate_id,
                   source_mode, status, captured_at, payload, created_at
            FROM core.gate_capture
            {clause}
            ORDER BY created_at DESC
            LIMIT :limit
            """,
            params, dsn=dsn,
        )
        return [_row(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.debug("gate_captures_read_failed", error=str(exc))
        return []


async def recent_reconciliations(
    *, leo_ready: Optional[bool] = None, limit: int = 100, dsn: Optional[str] = None,
) -> List[dict]:
    if not dsn:
        return []
    from jnpa_shared.db import fetch_all

    where = ""
    params: Dict[str, Any] = {"limit": max(1, min(int(limit), 1000))}
    if leo_ready is not None:
        where = "WHERE leo_ready = :ready"
        params["ready"] = leo_ready
    try:
        rows = await fetch_all(
            f"""
            SELECT id, container_no, vehicle_plate, leo_ready, customs_flags,
                   checks, source_mode, reconciled_at
            FROM core.leo_reconciliation
            {where}
            ORDER BY reconciled_at DESC
            LIMIT :limit
            """,
            params, dsn=dsn,
        )
        return [_row(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.debug("leo_recon_read_failed", error=str(exc))
        return []


async def customs_flag_history(*, limit: int = 200, dsn: Optional[str] = None) -> List[dict]:
    """Durable customs feed from core.alert (survives restart)."""
    if not dsn:
        return []
    from jnpa_shared.db import fetch_all

    try:
        rows = await fetch_all(
            """
            SELECT id, ts, kind, severity, plate, payload, ack
            FROM core.alert
            WHERE kind = 'CUSTOMS_FLAG'
            ORDER BY ts DESC
            LIMIT :limit
            """,
            {"limit": max(1, min(int(limit), 1000))}, dsn=dsn,
        )
        return [_row(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.debug("customs_history_read_failed", error=str(exc))
        return []


def _row(r: Any) -> dict:
    from datetime import datetime

    d = dict(r)
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
        elif isinstance(v, uuid.UUID):
            d[k] = str(v)
    return d


__all__ = [
    "ensure_gate_schema", "upsert_capture", "record_reconciliation",
    "raise_customs_alert", "log_api_audit", "recent_captures",
    "recent_reconciliations", "customs_flag_history",
]
