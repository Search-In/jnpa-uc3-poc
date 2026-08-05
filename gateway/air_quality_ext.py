"""Air-quality module schema bootstrap (idempotent, additive).

Applies the same DDL as infra/postgres/v3/0108_air_quality.sql at gateway
boot so a dev database that never ran the migration still gets the table —
exactly the pattern gateway/traffic_ext.ensure_traffic_schema uses (the
gateway image does not ship infra/, so the DDL is embedded here).

Every statement is CREATE ... IF NOT EXISTS: running it against a DB that
already has the objects is a no-op. It DROPS/ALTERS nothing existing.

Called once from gateway/main.py::_lifespan (best-effort; a DB blip only logs)
and, like the other exts, gated on JNPA_RUNTIME_DDL — under schema-v3 the DDL
is owned by the infra/postgres/v3 migrations, never runtime.

The _DDL list below MUST stay in lock-step with migration 0108; the test
tests/test_openaq.py asserts both define the same table + columns.
"""
from __future__ import annotations

import os
from typing import Optional

from .logging import get_logger

log = get_logger("gateway.air_quality_ext")

# One idempotent statement per list item (SQLAlchemy text() runs a single
# statement per execute()). Mirrors migration 0108 exactly.
_DDL: list[str] = [
    "CREATE SCHEMA IF NOT EXISTS core",
    """CREATE TABLE IF NOT EXISTS core.air_quality_readings (
        id         bigserial PRIMARY KEY,
        latitude   double precision NOT NULL,
        longitude  double precision NOT NULL,
        pm25       numeric,
        pm10       numeric,
        no2        numeric,
        so2        numeric,
        co         numeric,
        o3         numeric,
        aq_status  text,
        source     text NOT NULL DEFAULT 'OPENAQ',
        payload    jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_air_quality_readings_created "
    "ON core.air_quality_readings (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_air_quality_readings_coords "
    "ON core.air_quality_readings (round(CAST(latitude AS numeric), 2), "
    "round(CAST(longitude AS numeric), 2), created_at DESC)",
]


async def ensure_air_quality_schema(dsn: Optional[str] = None) -> None:
    """Create the air-quality table if absent. Idempotent; safe to call every boot."""
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
    log.info("air_quality_schema_ready", statements=applied, total=len(_DDL))


__all__ = ["ensure_air_quality_schema"]
