"""Logistics module schema bootstrap (idempotent, additive).

Applies the same DDL as infra/postgres/v3/0109_logistics_ulip.sql at gateway
boot so a dev database that never ran the migration still gets the tables —
exactly the pattern gateway/traffic_ext.ensure_traffic_schema uses (the
gateway image does not ship infra/, so the DDL is embedded here).

Every statement is CREATE ... IF NOT EXISTS: running it against a DB that
already has the objects is a no-op. Nothing existing is modified or removed.

Called once from gateway/main.py::_lifespan (best-effort; a DB blip only logs)
and, like the other exts, gated on JNPA_RUNTIME_DDL — under schema-v3 the DDL
is owned by the infra/postgres/v3 migrations, never runtime.

The _DDL list below MUST stay in lock-step with migration 0109; the test
tests/test_ulip_logistics.py asserts both define the same tables + columns.
"""
from __future__ import annotations

import os
from typing import Optional

from .logging import get_logger

log = get_logger("gateway.logistics_ext")

# One idempotent statement per list item (SQLAlchemy text() runs a single
# statement per execute()). Mirrors migration 0109 exactly.
_DDL: list[str] = [
    "CREATE SCHEMA IF NOT EXISTS core",
    """CREATE TABLE IF NOT EXISTS core.logistics_event (
        id          bigserial PRIMARY KEY,
        ref_type    text NOT NULL,
        ref_id      text NOT NULL,
        event_type  text NOT NULL,
        event_ts    timestamptz,
        location    text,
        latitude    double precision,
        longitude   double precision,
        source      text NOT NULL DEFAULT 'ULIP',
        source_api  text,
        detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at  timestamptz NOT NULL DEFAULT now())""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_logistics_event_dedup "
    "ON core.logistics_event (ref_type, ref_id, event_type, "
    "COALESCE(event_ts, 'epoch'::timestamptz), COALESCE(location, ''))",
    "CREATE INDEX IF NOT EXISTS idx_logistics_event_ref "
    "ON core.logistics_event (ref_id, event_ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_logistics_event_created "
    "ON core.logistics_event (created_at DESC)",
    """CREATE TABLE IF NOT EXISTS core.logistics_tracking (
        id             bigserial PRIMARY KEY,
        ref_type       text NOT NULL,
        ref_id         text NOT NULL,
        status         text NOT NULL DEFAULT 'UNKNOWN',
        last_event     text,
        last_location  text,
        last_event_ts  timestamptz,
        event_count    integer NOT NULL DEFAULT 0,
        source         text NOT NULL DEFAULT 'ULIP',
        payload        jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at     timestamptz NOT NULL DEFAULT now(),
        updated_at     timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_logistics_tracking_ref UNIQUE (ref_type, ref_id))""",
    "CREATE INDEX IF NOT EXISTS idx_logistics_tracking_updated "
    "ON core.logistics_tracking (updated_at DESC)",
    """CREATE TABLE IF NOT EXISTS core.ulip_api_audit (
        id          bigserial PRIMARY KEY,
        api_name    text NOT NULL,
        ref_type    text,
        ref_id      text,
        ok          boolean NOT NULL DEFAULT false,
        http_status integer,
        latency_ms  numeric,
        error       text,
        response    jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at  timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_ulip_api_audit_created "
    "ON core.ulip_api_audit (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ulip_api_audit_ref "
    "ON core.ulip_api_audit (ref_id, created_at DESC)",
]


async def ensure_logistics_schema(dsn: Optional[str] = None) -> None:
    """Create the logistics tables if absent. Idempotent; safe to call every boot."""
    if os.getenv("JNPA_RUNTIME_DDL", "0") != "1":
        # schema-v3: DDL is owned by infra/postgres/v3 migrations, never runtime.
        return
    from sqlalchemy import text

    from jnpa_shared.db import get_engine

    engine = get_engine(dsn)
    applied = 0
    async with engine.begin() as conn:
        for stmt in _DDL:
            await conn.execute(text(stmt))
            applied += 1
    log.info("logistics_schema_ready", statements=applied, total=len(_DDL))


__all__ = ["ensure_logistics_schema"]
