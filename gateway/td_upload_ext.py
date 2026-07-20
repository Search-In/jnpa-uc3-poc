"""Transporters & Drivers Data-Upload schema bootstrap (idempotent, additive).

Applies the same DDL as infra/postgres/migrations/0035_transporters_drivers_upload.sql
at gateway boot so a dev/mock database that never ran the migration still gets the
import-ledger tables + the masters' import_file_id link lazily — exactly the pattern
gateway/cfs_ecy_ext.ensure_cfs_ecy_schema uses.

Every statement is CREATE ... IF NOT EXISTS / ALTER ... ADD COLUMN IF NOT EXISTS:
running it against a DB that already has the objects (migration ran) is a no-op. It
NEVER drops/alters existing objects. Asserted in lock-step by
tests/test_td_upload_schema.py.

Called once from gateway/main.py::_lifespan (best-effort; a DB blip only logs).
"""
from __future__ import annotations

from typing import Optional

from .logging import get_logger

log = get_logger("gateway.td_upload_ext")

# One idempotent statement per list item. Mirrors migration 0035 exactly.
_DDL: list[str] = [
    "CREATE SCHEMA IF NOT EXISTS jnpa",
    "ALTER TABLE jnpa.transporters  ADD COLUMN IF NOT EXISTS import_file_id bigint",
    "ALTER TABLE jnpa.driver_master ADD COLUMN IF NOT EXISTS import_file_id bigint",
    "CREATE INDEX IF NOT EXISTS idx_transporters_import_file ON jnpa.transporters (import_file_id)",
    "CREATE INDEX IF NOT EXISTS idx_driver_master_import_file ON jnpa.driver_master (import_file_id)",
    """CREATE TABLE IF NOT EXISTS jnpa.td_import_files (
        id               bigserial PRIMARY KEY,
        entity_type      text NOT NULL CHECK (entity_type IN ('TRANSPORTER','DRIVER')),
        physical_format  text NOT NULL CHECK (physical_format IN ('CSV','XLS','XLSX')),
        source_file      text,
        source_sha256    text,
        file_size_bytes  bigint,
        record_count     integer NOT NULL DEFAULT 0,
        imported_count   integer NOT NULL DEFAULT 0,
        error_count      integer NOT NULL DEFAULT 0,
        duplicate_count  integer NOT NULL DEFAULT 0,
        import_status    text NOT NULL DEFAULT 'PENDING'
                         CHECK (import_status IN
                                ('PENDING','SUCCESS','PARTIAL','FAILED','SKIPPED_DUPLICATE')),
        error_detail     text,
        uploaded_by      text,
        source           text NOT NULL DEFAULT 'UPLOAD' CHECK (source IN ('DIRECTORY','UPLOAD')),
        created_at       timestamptz NOT NULL DEFAULT now(),
        updated_at       timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_td_import_file_sha UNIQUE (source_sha256))""",
    "CREATE INDEX IF NOT EXISTS idx_td_file_status ON jnpa.td_import_files (import_status, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_td_file_source ON jnpa.td_import_files (source, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_td_file_entity ON jnpa.td_import_files (entity_type, id DESC)",
    """CREATE TABLE IF NOT EXISTS jnpa.td_import_errors (
        id               bigserial PRIMARY KEY,
        import_file_id   bigint NOT NULL
                         REFERENCES jnpa.td_import_files (id) ON DELETE CASCADE,
        record_ref       text,
        error_code       text NOT NULL,
        error_detail     text,
        created_at       timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_td_err_file ON jnpa.td_import_errors (import_file_id, id)",
]


async def ensure_td_upload_schema(dsn: Optional[str] = None) -> None:
    """Create the Transporter/Driver import-ledger tables + import_file_id links if
    absent. Idempotent."""
    from sqlalchemy import text

    from jnpa_shared.db import get_engine

    engine = get_engine(dsn)
    async with engine.begin() as conn:
        for stmt in _DDL:
            await conn.execute(text(stmt))
    log.info("td_upload_schema_ready")
