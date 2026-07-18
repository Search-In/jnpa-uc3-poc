"""CFS-ECY CODECO schema bootstrap (idempotent, additive).

Applies the same DDL as infra/postgres/migrations/0027_cfs_ecy_codeco.sql at
gateway boot so a dev/mock database that never ran the migration still gets the
new table + view lazily — exactly the pattern gateway/uc3_ext.ensure_uc3_schema
and gateway/routers/kpi.ensure_kpi_gate_schema already use.

Every statement is CREATE ... IF NOT EXISTS / CREATE OR REPLACE VIEW: running it
against a DB that already has the objects (because the migration ran) is a no-op.
NEVER drops/alters existing objects.

Called once from gateway/main.py::_lifespan (best-effort; a DB blip only logs).
Also reused by scripts/import_cfs_ecy_codeco.py so the importer is self-contained.
"""
from __future__ import annotations

from typing import Optional

from .logging import get_logger

log = get_logger("gateway.cfs_ecy_ext")

# One idempotent statement per list item (SQLAlchemy text() runs a single
# statement per execute()). Mirrors migration 0027 exactly.
_DDL: list[str] = [
    "CREATE SCHEMA IF NOT EXISTS jnpa",
    """CREATE TABLE IF NOT EXISTS jnpa.cfs_ecy_movements (
        id               bigserial PRIMARY KEY,
        facility_type    text NOT NULL CHECK (facility_type IN ('CFS','ECY')),
        container_number text NOT NULL,
        iso_valid        boolean NOT NULL DEFAULT true,
        event_ts         timestamptz NOT NULL,
        mode             text NOT NULL CHECK (mode IN ('IN','OUT')),
        source           text NOT NULL DEFAULT 'CODECO',
        source_file      text,
        created_at       timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_cfs_ecy_movement UNIQUE (facility_type, container_number, event_ts, mode))""",
    "CREATE INDEX IF NOT EXISTS idx_cfsecy_container ON jnpa.cfs_ecy_movements (container_number, event_ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_cfsecy_facility_ts ON jnpa.cfs_ecy_movements (facility_type, event_ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_cfsecy_facility_mode_ts ON jnpa.cfs_ecy_movements (facility_type, mode, event_ts DESC)",
    """CREATE OR REPLACE VIEW jnpa.v_cfs_ecy_dwell AS
        SELECT
            m.container_number,
            m.facility_type,
            min(m.event_ts) FILTER (WHERE m.mode = 'IN')  AS first_in_ts,
            max(m.event_ts) FILTER (WHERE m.mode = 'OUT') AS last_out_ts,
            count(*)        FILTER (WHERE m.mode = 'IN')  AS in_events,
            count(*)        FILTER (WHERE m.mode = 'OUT') AS out_events,
            CASE
                WHEN m.facility_type = 'CFS'
                 AND min(m.event_ts) FILTER (WHERE m.mode = 'IN')  IS NOT NULL
                 AND max(m.event_ts) FILTER (WHERE m.mode = 'OUT') IS NOT NULL
                 AND max(m.event_ts) FILTER (WHERE m.mode = 'OUT')
                     >= min(m.event_ts) FILTER (WHERE m.mode = 'IN')
                THEN round(extract(epoch FROM (
                        max(m.event_ts) FILTER (WHERE m.mode = 'OUT')
                      - min(m.event_ts) FILTER (WHERE m.mode = 'IN')
                     )) / 3600.0::numeric, 2)
                ELSE NULL
            END AS dwell_hours
        FROM jnpa.cfs_ecy_movements m
        GROUP BY m.container_number, m.facility_type""",
]


async def ensure_cfs_ecy_schema(dsn: Optional[str] = None) -> None:
    """Create the CFS-ECY movement table + dwell view if absent. Idempotent."""
    from sqlalchemy import text

    from jnpa_shared.db import get_engine

    engine = get_engine(dsn)
    async with engine.begin() as conn:
        for stmt in _DDL:
            await conn.execute(text(stmt))
    log.info("cfs_ecy_schema_ready")
