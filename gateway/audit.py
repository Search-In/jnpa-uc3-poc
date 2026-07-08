"""Common audit & persistence framework — the single-source-of-truth writers.

This module owns the five cross-cutting audit/event tables (see
``infra/postgres/migrations/0003_audit_persistence.sql``) and the ONE funnel each
subsystem calls to make its runtime output durable:

    api_audit_log        -> log_api_audit()        every external API req/resp
    digital_twin_events  -> record_event()         every operational / AI event
    notifications        -> log_notification()      every notification dispatch
    decision_audit       -> record_decision_audit() every orchestrated decision
    geofence_events      -> record_geofence_event() zone enter/exit + violations

Design rules:
* **Never raise into the caller.** Persistence is best-effort: a DB hiccup must
  not break a live request or a WS broadcast. Every writer swallows + logs.
* **Fire-and-forget friendly.** ``spawn(coro)`` schedules a writer on the running
  loop so hot paths (httpx send, ws broadcast) don't await the DB round-trip.
* **JSONB-safe.** Values are ``json.dumps``'d and cast in SQL (``CAST(:x AS jsonb)``)
  exactly like jnpa_shared.vahan_db, so asyncpg never sees a raw dict.

The DDL here is idempotent and applied at gateway boot via
``ensure_audit_schema(dsn)`` (mirrors gateway/enforcement.py) so an existing /
RDS database is topped up without a re-init.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Dict, Optional

from .logging import get_logger

log = get_logger("gateway.audit")

# --- schema (idempotent; lazily applied, cached per-DSN) --------------------
_DDL = """
CREATE SCHEMA IF NOT EXISTS jnpa;

CREATE TABLE IF NOT EXISTS jnpa.api_audit_log (
    id               bigserial PRIMARY KEY,
    service_name     text NOT NULL,
    endpoint         text,
    method           text,
    request_payload  jsonb NOT NULL DEFAULT '{}'::jsonb,
    response_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    status_code      integer,
    latency_ms       numeric(10,2),
    error            text,
    transaction_id   text,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_api_audit_service_ts ON jnpa.api_audit_log (service_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_audit_txn        ON jnpa.api_audit_log (transaction_id);
CREATE INDEX IF NOT EXISTS idx_api_audit_ts         ON jnpa.api_audit_log (created_at DESC);

CREATE TABLE IF NOT EXISTS jnpa.digital_twin_events (
    id           bigserial PRIMARY KEY,
    event_type   text NOT NULL,
    vehicle_id   text,
    driver_id    text,
    location     jsonb NOT NULL DEFAULT '{}'::jsonb,
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dt_events_type_ts    ON jnpa.digital_twin_events (event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dt_events_vehicle_ts ON jnpa.digital_twin_events (vehicle_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dt_events_driver_ts  ON jnpa.digital_twin_events (driver_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dt_events_ts         ON jnpa.digital_twin_events (created_at DESC);

CREATE TABLE IF NOT EXISTS jnpa.notifications (
    id                bigserial PRIMARY KEY,
    event_id          text,
    channel           text NOT NULL,
    receiver          text,
    message           text,
    delivery_status   text NOT NULL DEFAULT 'PENDING'
                      CHECK (delivery_status IN
                             ('PENDING','SENT','DELIVERED','FAILED','SKIPPED','NO_SUBSCRIPTION')),
    provider_response jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_notifications_ts       ON jnpa.notifications (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_receiver ON jnpa.notifications (receiver, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_status   ON jnpa.notifications (delivery_status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_event    ON jnpa.notifications (event_id);

CREATE TABLE IF NOT EXISTS jnpa.decision_audit (
    id            bigserial PRIMARY KEY,
    request_id    text,
    input_data    jsonb NOT NULL DEFAULT '{}'::jsonb,
    rule_executed text,
    decision      text,
    action_taken  text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_decision_audit_ts      ON jnpa.decision_audit (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decision_audit_request ON jnpa.decision_audit (request_id);
CREATE INDEX IF NOT EXISTS idx_decision_audit_rule    ON jnpa.decision_audit (rule_executed, created_at DESC);

CREATE TABLE IF NOT EXISTS jnpa.geofence_events (
    id             bigserial PRIMARY KEY,
    vehicle_id     text,
    zone_id        text,
    entry_time     timestamptz,
    exit_time      timestamptz,
    violation_type text,
    action_taken   text,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_geofence_events_vehicle ON jnpa.geofence_events (vehicle_id, entry_time DESC);
CREATE INDEX IF NOT EXISTS idx_geofence_events_zone    ON jnpa.geofence_events (zone_id, entry_time DESC);
CREATE INDEX IF NOT EXISTS idx_geofence_events_ts      ON jnpa.geofence_events (created_at DESC);

CREATE TABLE IF NOT EXISTS jnpa.anpr_reads (
    ts            timestamptz NOT NULL,
    camera_id     text,
    plate         text,
    conf          real,
    vehicle_class text,
    image_url     text,
    weather       text,
    degraded      boolean DEFAULT false
);
CREATE INDEX IF NOT EXISTS idx_anpr_plate_ts ON jnpa.anpr_reads (plate, ts DESC);
"""

_SCHEMA_READY: Dict[str, bool] = {}
# The DSN the fire-and-forget writers use. Set once at gateway boot from cfg so
# ``spawn(record_event(...))`` callers don't have to thread the DSN through.
_DEFAULT_DSN: Optional[str] = None


def configure(dsn: Optional[str]) -> None:
    """Record the default DSN used by the audit writers (called at gateway boot)."""
    global _DEFAULT_DSN
    _DEFAULT_DSN = dsn or None


async def ensure_audit_schema(dsn: Optional[str]) -> None:
    """Apply the idempotent audit/event DDL once per DSN (best-effort, cached).

    ``jnpa.anpr_reads`` is (re)created here too so the ANPR-persistence pump has a
    guaranteed writer target even on volumes predating init.sql's hypertable. The
    plain-table form is compatible with the existing hypertable (CREATE ... IF NOT
    EXISTS is a no-op when the hypertable already exists).
    """
    configure(dsn)
    if not dsn:
        log.warning("audit_schema_skipped_no_dsn")
        return
    if _SCHEMA_READY.get(dsn):
        return
    from jnpa_shared.db import execute  # lazy import (shared engine)

    for stmt in (s.strip() for s in _DDL.split(";")):
        if stmt:
            try:
                await execute(stmt, dsn=dsn)
            except Exception as exc:  # noqa: BLE001 — one bad DDL must not abort boot
                log.warning("audit_ddl_stmt_skipped", error=str(exc), stmt=stmt[:60])
    _SCHEMA_READY[dsn] = True
    log.info("audit_schema_ready")


# --- helpers ----------------------------------------------------------------
def _j(value: Any) -> str:
    """JSON-encode any value for a JSONB column (default=str for datetimes etc.)."""
    try:
        return json.dumps(value if value is not None else {}, default=str)
    except Exception:  # noqa: BLE001
        return json.dumps({"_unserializable": str(value)})


def _parse_ts(value: Any) -> Optional[datetime]:
    """Coerce an ISO-8601 string / datetime into a datetime (asyncpg wants a
    datetime object for a timestamptz bind, not a str). Returns None on failure so
    the caller's COALESCE(:ts, now()) supplies a server timestamp."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return None
    return None


def spawn(coro) -> None:
    """Fire-and-forget a writer coroutine on the running loop (never awaits DB).

    Safe to call from hot paths. If there is no running loop (sync context), the
    write is run to completion instead so nothing is silently dropped.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001
            log.warning("audit_spawn_sync_failed", error=str(exc))
        return
    task = loop.create_task(coro)
    # Prevent "task was never retrieved" warnings; errors are logged in-writer.
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


# --- writers (each best-effort; never raises) -------------------------------
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
    """Persist one external API request/response to jnpa.api_audit_log."""
    dsn = dsn or _DEFAULT_DSN
    if not dsn:
        return
    from jnpa_shared.db import execute

    try:
        await execute(
            """
            INSERT INTO jnpa.api_audit_log
                (service_name, endpoint, method, request_payload, response_payload,
                 status_code, latency_ms, error, transaction_id)
            VALUES
                (:service_name, :endpoint, :method,
                 CAST(:request_payload AS jsonb), CAST(:response_payload AS jsonb),
                 :status_code, :latency_ms, :error, :transaction_id)
            """,
            {
                "service_name": service_name,
                "endpoint": endpoint,
                "method": method,
                "request_payload": _j(request_payload),
                "response_payload": _j(response_payload),
                "status_code": status_code,
                "latency_ms": round(latency_ms, 2) if latency_ms is not None else None,
                "error": error,
                "transaction_id": transaction_id,
            },
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("api_audit_write_failed", service=service_name, error=str(exc))


async def record_event(
    *,
    event_type: str,
    vehicle_id: Optional[str] = None,
    driver_id: Optional[str] = None,
    location: Any = None,
    payload: Any = None,
    dsn: Optional[str] = None,
) -> None:
    """Persist one operational / AI event to jnpa.digital_twin_events."""
    dsn = dsn or _DEFAULT_DSN
    if not dsn:
        return
    from jnpa_shared.db import execute

    try:
        await execute(
            """
            INSERT INTO jnpa.digital_twin_events
                (event_type, vehicle_id, driver_id, location, payload)
            VALUES
                (:event_type, :vehicle_id, :driver_id,
                 CAST(:location AS jsonb), CAST(:payload AS jsonb))
            """,
            {
                "event_type": event_type,
                "vehicle_id": vehicle_id,
                "driver_id": driver_id,
                "location": _j(location),
                "payload": _j(payload),
            },
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("event_write_failed", event_type=event_type, error=str(exc))


async def log_notification(
    *,
    channel: str,
    event_id: Optional[str] = None,
    receiver: Optional[str] = None,
    message: Optional[str] = None,
    delivery_status: str = "PENDING",
    provider_response: Any = None,
    dsn: Optional[str] = None,
) -> None:
    """Persist one notification dispatch to jnpa.notifications."""
    dsn = dsn or _DEFAULT_DSN
    if not dsn:
        return
    from jnpa_shared.db import execute

    valid = {"PENDING", "SENT", "DELIVERED", "FAILED", "SKIPPED", "NO_SUBSCRIPTION"}
    status = delivery_status if delivery_status in valid else "PENDING"
    try:
        await execute(
            """
            INSERT INTO jnpa.notifications
                (event_id, channel, receiver, message, delivery_status, provider_response)
            VALUES
                (:event_id, :channel, :receiver, :message, :delivery_status,
                 CAST(:provider_response AS jsonb))
            """,
            {
                "event_id": str(event_id) if event_id is not None else None,
                "channel": channel,
                "receiver": receiver,
                "message": message,
                "delivery_status": status,
                "provider_response": _j(provider_response),
            },
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("notification_write_failed", channel=channel, error=str(exc))


async def record_decision_audit(
    *,
    request_id: Optional[str] = None,
    input_data: Any = None,
    rule_executed: Optional[str] = None,
    decision: Optional[str] = None,
    action_taken: Optional[str] = None,
    dsn: Optional[str] = None,
) -> None:
    """Persist one orchestrated decision to jnpa.decision_audit (durable DecisionRing)."""
    dsn = dsn or _DEFAULT_DSN
    if not dsn:
        return
    from jnpa_shared.db import execute

    try:
        await execute(
            """
            INSERT INTO jnpa.decision_audit
                (request_id, input_data, rule_executed, decision, action_taken)
            VALUES
                (:request_id, CAST(:input_data AS jsonb), :rule_executed,
                 :decision, :action_taken)
            """,
            {
                "request_id": request_id,
                "input_data": _j(input_data),
                "rule_executed": rule_executed,
                "decision": decision,
                "action_taken": action_taken,
            },
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("decision_audit_write_failed", rule=rule_executed, error=str(exc))


async def record_geofence_event(
    *,
    vehicle_id: Optional[str] = None,
    zone_id: Optional[str] = None,
    entry_time: Optional[datetime] = None,
    exit_time: Optional[datetime] = None,
    violation_type: Optional[str] = None,
    action_taken: Optional[str] = None,
    dsn: Optional[str] = None,
) -> None:
    """Persist one geofence enter/exit/violation to jnpa.geofence_events."""
    dsn = dsn or _DEFAULT_DSN
    if not dsn:
        return
    from jnpa_shared.db import execute

    try:
        await execute(
            """
            INSERT INTO jnpa.geofence_events
                (vehicle_id, zone_id, entry_time, exit_time, violation_type, action_taken)
            VALUES
                (:vehicle_id, :zone_id, :entry_time, :exit_time, :violation_type, :action_taken)
            """,
            {
                "vehicle_id": vehicle_id,
                "zone_id": zone_id,
                "entry_time": entry_time,
                "exit_time": exit_time,
                "violation_type": violation_type,
                "action_taken": action_taken,
            },
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("geofence_event_write_failed", zone=zone_id, error=str(exc))


async def persist_anpr_read(read: Dict[str, Any], *, dsn: Optional[str] = None) -> None:
    """Persist one ANPR read (Kafka anpr.reads message) to jnpa.anpr_reads.

    Gives the long-empty jnpa.anpr_reads hypertable its missing writer, and mirrors
    the detection into the unified event timeline as an ANPR_DETECTION event.
    """
    dsn = dsn or _DEFAULT_DSN
    if not dsn:
        return
    from jnpa_shared.db import execute

    try:
        await execute(
            """
            INSERT INTO jnpa.anpr_reads
                (ts, camera_id, plate, conf, vehicle_class, image_url, weather, degraded)
            VALUES
                (COALESCE(:ts, now()), :camera_id, :plate, :conf,
                 :vehicle_class, :image_url, :weather, :degraded)
            """,
            {
                "ts": _parse_ts(read.get("ts")),
                "camera_id": read.get("camera_id"),
                "plate": read.get("plate"),
                "conf": read.get("conf"),
                "vehicle_class": read.get("vehicle_class"),
                "image_url": read.get("image_url"),
                "weather": read.get("weather"),
                "degraded": bool(read.get("degraded", False)),
            },
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("anpr_read_write_failed", error=str(exc))
        return
    # Mirror into the unified event timeline (best-effort).
    await record_event(
        event_type="ANPR_DETECTION",
        vehicle_id=read.get("plate"),
        location={"camera_id": read.get("camera_id")},
        payload={
            "conf": read.get("conf"),
            "vehicle_class": read.get("vehicle_class"),
            "weather": read.get("weather"),
            "degraded": bool(read.get("degraded", False)),
        },
        dsn=dsn,
    )


# --- alert -> event/geofence mapping (used by the alerts pump) --------------
_ALERT_KIND_TO_EVENT = {
    "CUSTOMS_FLAG": "CUSTOMS_ALERT",
    "ILLEGAL_PARKING": "PARKING_VIOLATION",
    "ABANDONED": "GEOFENCE_VIOLATION",
    "ROUTE_DEVIATION": "ROUTE_DEVIATION",
    "CONGESTION": "CONGESTION_ALERT",
    "GEOFENCE": "GEOFENCE_VIOLATION",
}
_GEOFENCE_KINDS = {"ILLEGAL_PARKING", "ABANDONED", "GEOFENCE"}


async def persist_alert_event(alert: Dict[str, Any], *, dsn: Optional[str] = None) -> None:
    """Mirror an alert (as it flows through the gateway) into the event timeline.

    Every alert becomes a jnpa.digital_twin_events row; geofence-family alerts ALSO
    land in jnpa.geofence_events so the zone violation trail is queryable directly.
    Best-effort; the alerts feed itself is unchanged.
    """
    dsn = dsn or _DEFAULT_DSN
    if not dsn or not isinstance(alert, dict):
        return
    kind = str(alert.get("kind") or alert.get("type") or "ALERT")
    payload = alert.get("payload") if isinstance(alert.get("payload"), dict) else alert
    plate = alert.get("plate") or (payload or {}).get("plate")
    driver_id = (payload or {}).get("driver_id")
    zone_id = (payload or {}).get("zone_id") or (payload or {}).get("zone")
    await record_event(
        event_type=_ALERT_KIND_TO_EVENT.get(kind, "AI_EVENT"),
        vehicle_id=plate,
        driver_id=driver_id,
        location={"gate_id": alert.get("gate_id"), "zone_id": zone_id},
        payload={"alert_id": str(alert.get("id") or ""), "kind": kind,
                 "severity": alert.get("severity"), **(payload or {})},
        dsn=dsn,
    )
    if kind in _GEOFENCE_KINDS:
        await record_geofence_event(
            vehicle_id=plate,
            zone_id=zone_id,
            violation_type=kind,
            action_taken=(payload or {}).get("action") or "ALERT_RAISED",
            dsn=dsn,
        )


__all__ = [
    "configure",
    "ensure_audit_schema",
    "spawn",
    "log_api_audit",
    "record_event",
    "log_notification",
    "record_decision_audit",
    "record_geofence_event",
    "persist_anpr_read",
    "persist_alert_event",
]
