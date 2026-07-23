"""Performance Data Upload schema bootstrap (idempotent, additive).

Applies the same DDL as infra/postgres/migrations/0030_performance_uploads.sql at
gateway boot so a dev/mock DB that never ran the migration still gets the upload
lifecycle tables. Mirrors gateway/performance_ext.ensure_performance_schema.

Every statement is CREATE ... IF NOT EXISTS; running against a DB that already has
the objects is a no-op. Touches nothing outside this sub-module.
"""
from __future__ import annotations

from typing import Optional

from .logging import get_logger

log = get_logger("gateway.performance_upload_ext")

_DDL: list[str] = [
    "CREATE SCHEMA IF NOT EXISTS jnpa",
    "CREATE EXTENSION IF NOT EXISTS pgcrypto",
    """CREATE TABLE IF NOT EXISTS jnpa.perf_uploads (
        id                bigserial PRIMARY KEY,
        upload_id         uuid NOT NULL DEFAULT gen_random_uuid(),
        report_type       text NOT NULL CHECK (report_type IN ('daily_status','monthly_teu','ldb_report')),
        original_filename text,
        file_size_bytes   int,
        status            text NOT NULL DEFAULT 'VALIDATED'
                              CHECK (status IN ('VALIDATED','REJECTED','IMPORTED','FAILED')),
        uploaded_by       text,
        row_count         int NOT NULL DEFAULT 0,
        inserted_count    int NOT NULL DEFAULT 0,
        skipped_count     int NOT NULL DEFAULT 0,
        error_count       int NOT NULL DEFAULT 0,
        notes             text,
        created_at        timestamptz NOT NULL DEFAULT now(),
        completed_at      timestamptz,
        CONSTRAINT uq_perf_uploads_upload_id UNIQUE (upload_id))""",
    "CREATE INDEX IF NOT EXISTS idx_perf_uploads_created ON jnpa.perf_uploads (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_perf_uploads_type_status ON jnpa.perf_uploads (report_type, status)",
    """CREATE TABLE IF NOT EXISTS jnpa.perf_import_logs (
        id            bigserial PRIMARY KEY,
        upload_id     uuid NOT NULL REFERENCES jnpa.perf_uploads(upload_id) ON DELETE CASCADE,
        phase         text NOT NULL CHECK (phase IN ('VALIDATE','IMPORT')),
        level         text NOT NULL DEFAULT 'INFO' CHECK (level IN ('INFO','WARN','ERROR')),
        message       text,
        target_table  text,
        affected_rows int,
        created_at    timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_perf_import_logs_upload ON jnpa.perf_import_logs (upload_id, created_at)",
    """CREATE TABLE IF NOT EXISTS jnpa.perf_upload_errors (
        id           bigserial PRIMARY KEY,
        upload_id    uuid NOT NULL REFERENCES jnpa.perf_uploads(upload_id) ON DELETE CASCADE,
        row_number   int,
        column_name  text,
        error_code   text,
        error_detail text,
        raw_value    text,
        created_at   timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_perf_upload_errors_upload ON jnpa.perf_upload_errors (upload_id)",
    # Upgrade path (mirrors migration 0038): the ledger records the physical format the
    # client uploaded (PDF | XLSX | CSV) and how many existing rows a re-uploaded
    # corrected report REPLACED (the importer now upserts instead of skipping).
    "ALTER TABLE jnpa.perf_uploads ADD COLUMN IF NOT EXISTS file_format text",
    "ALTER TABLE jnpa.perf_uploads ADD COLUMN IF NOT EXISTS updated_count integer NOT NULL DEFAULT 0",
]


async def ensure_performance_upload_schema(dsn: Optional[str] = None) -> None:
    """Create the upload lifecycle tables if absent. Idempotent, additive."""
    from sqlalchemy import text

    from jnpa_shared.db import get_engine

    engine = get_engine(dsn)
    async with engine.begin() as conn:
        for stmt in _DDL:
            await conn.execute(text(stmt))
    log.info("performance_upload_schema_ready")
