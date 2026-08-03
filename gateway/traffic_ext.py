"""Traffic module schema bootstrap (idempotent, additive).

Applies the same DDL as infra/postgres/v3/0107_traffic_reading.sql at gateway
boot so a dev database that never ran the migration still gets the table —
exactly the pattern gateway/weather_ext.ensure_weather_schema uses (the
gateway image does not ship infra/, so the DDL is embedded here).

Every statement is CREATE ... IF NOT EXISTS: running it against a DB that
already has the objects is a no-op. It DROPS/ALTERS nothing existing.

Called once from gateway/main.py::_lifespan (best-effort; a DB blip only logs)
and, like the other exts, gated on JNPA_RUNTIME_DDL — under schema-v3 the DDL
is owned by the infra/postgres/v3 migrations, never runtime.

The _DDL list below MUST stay in lock-step with migration 0107; the test
tests/test_tomtom.py asserts both define the same table + columns.
"""
from __future__ import annotations

import os
from typing import Optional

from .logging import get_logger

log = get_logger("gateway.traffic_ext")

# One idempotent statement per list item (SQLAlchemy text() runs a single
# statement per execute()). Mirrors migration 0107 exactly.
_DDL: list[str] = [
    "CREATE SCHEMA IF NOT EXISTS core",
    """CREATE TABLE IF NOT EXISTS core.traffic_reading (
        id               bigserial PRIMARY KEY,
        latitude         double precision NOT NULL,
        longitude        double precision NOT NULL,
        current_speed    numeric,
        free_flow_speed  numeric,
        congestion_level text,
        delay_seconds    numeric,
        source           text NOT NULL DEFAULT 'TOMTOM',
        payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at       timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_traffic_reading_created "
    "ON core.traffic_reading (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_traffic_reading_coords "
    "ON core.traffic_reading (round(CAST(latitude AS numeric), 2), "
    "round(CAST(longitude AS numeric), 2), created_at DESC)",
]


async def ensure_traffic_schema(dsn: Optional[str] = None) -> None:
    """Create the traffic-reading table if absent. Idempotent; safe to call every boot."""
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
    log.info("traffic_schema_ready", statements=applied, total=len(_DDL))


__all__ = ["ensure_traffic_schema"]
