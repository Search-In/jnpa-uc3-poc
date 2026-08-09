"""Berthing Reports schema bootstrap (UC-III module 7) — idempotent, additive.

Applies the same DDL as infra/postgres/migrations/0036_berthing_reports.sql at
gateway boot so a dev/mock database that never ran the migration still gets the
new tables lazily — exactly the pattern gateway/cfs_ecy_ext.ensure_cfs_ecy_schema
and gateway/shipping_lines_ext.ensure_shipping_lines_schema already use.

Every statement is CREATE ... IF NOT EXISTS: running it against a DB that already
has the objects (because the migration ran) is a no-op. NEVER drops/alters existing
objects. Called once from gateway/main.py::_lifespan (best-effort). Also reused by
scripts/import_berthing_reports.py so the importer is self-contained.

The _DDL list is kept byte-for-byte in lock-step with migration 0036 (a test asserts
the two define the identical object set).
"""
from __future__ import annotations

import os

from typing import Optional

from .logging import get_logger

log = get_logger("gateway.berthing_ext")

# One idempotent statement per list item (SQLAlchemy text() runs a single statement
# per execute()). Mirrors migration 0036 exactly.
_DDL: list[str] = [
    "CREATE SCHEMA IF NOT EXISTS core",
    """CREATE TABLE IF NOT EXISTS core.berthing_record (
        id                    bigserial PRIMARY KEY,
        terminal              text NOT NULL,
        vessel_name           text NOT NULL,
        imo_number            text,
        voyage_number         text NOT NULL,
        shipping_line         text,
        berth_number          text,
        eta                   timestamptz,
        ata                   timestamptz,
        berthing_time         timestamptz,
        departure_time        timestamptz,
        cargo_operation_start timestamptz,
        cargo_operation_end   timestamptz,
        status                text NOT NULL DEFAULT 'EXPECTED'
                              CHECK (status IN ('EXPECTED','ARRIVED','BERTH_ASSIGNED',
                                                'BERTHING_STARTED','CARGO_OPERATION',
                                                'COMPLETED','DEPARTED')),
        source_file           text,
        import_file_id        bigint,
        created_at            timestamptz NOT NULL DEFAULT now(),
        updated_at            timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_berthing_call UNIQUE (terminal, voyage_number, vessel_name))""",
    "CREATE INDEX IF NOT EXISTS idx_berthing_terminal_status ON core.berthing_record (terminal, status)",
    "CREATE INDEX IF NOT EXISTS idx_berthing_voyage ON core.berthing_record (voyage_number)",
    "CREATE INDEX IF NOT EXISTS idx_berthing_vessel ON core.berthing_record (vessel_name)",
    "CREATE INDEX IF NOT EXISTS idx_berthing_eta ON core.berthing_record (eta DESC)",
    "CREATE INDEX IF NOT EXISTS idx_berthing_import_file ON core.berthing_record (import_file_id)",
    """CREATE TABLE IF NOT EXISTS core.berthing_record_event (
        id           bigserial PRIMARY KEY,
        berthing_id  bigint NOT NULL REFERENCES core.berthing_record (id) ON DELETE CASCADE,
        event_type   text NOT NULL,
        event_time   timestamptz,
        created_by   text,
        created_at   timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_berthing_event UNIQUE (berthing_id, event_type))""",
    "CREATE INDEX IF NOT EXISTS idx_berthing_event_call ON core.berthing_record_event (berthing_id, id)",
    """CREATE TABLE IF NOT EXISTS core.berthing_import_file (
        id               bigserial PRIMARY KEY,
        filename         text,
        file_hash        text,
        terminal         text,
        physical_format  text NOT NULL DEFAULT 'CSV'
                         CHECK (physical_format IN ('CSV','XLS','XLSX','PDF')),
        uploaded_by      text,
        status           text NOT NULL DEFAULT 'PENDING'
                         CHECK (status IN ('PENDING','SUCCESS','PARTIAL','FAILED','SKIPPED_DUPLICATE')),
        total_rows       integer NOT NULL DEFAULT 0,
        success_rows     integer NOT NULL DEFAULT 0,
        failed_rows      integer NOT NULL DEFAULT 0,
        duplicate_rows   integer NOT NULL DEFAULT 0,
        source           text NOT NULL DEFAULT 'UPLOAD' CHECK (source IN ('DIRECTORY','UPLOAD')),
        error_detail     text,
        created_at       timestamptz NOT NULL DEFAULT now(),
        updated_at       timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_berthing_import_file_hash UNIQUE (file_hash))""",
    "CREATE INDEX IF NOT EXISTS idx_berthing_file_status ON core.berthing_import_file (status, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_berthing_file_terminal ON core.berthing_import_file (terminal, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_berthing_file_source ON core.berthing_import_file (source, id DESC)",
    """CREATE TABLE IF NOT EXISTS core.berthing_import_error (
        id              bigserial PRIMARY KEY,
        import_file_id  bigint NOT NULL
                        REFERENCES core.berthing_import_file (id) ON DELETE CASCADE,
        row_number      integer,
        error_message   text,
        raw_data        text,
        created_at      timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_berthing_err_file ON core.berthing_import_error (import_file_id, id)",
    # --- Full-fidelity PDF capture (migration 0037) — additive verbatim table store ----------
    # Mirrors migration 0037 exactly so a dev/mock DB that never ran it still gets the objects.
    # Never touches the 0036 normalised tables above.
    """CREATE TABLE IF NOT EXISTS core.berthing_report_document (
        id            bigserial PRIMARY KEY,
        file_name     text NOT NULL,
        terminal      text,
        report_date   date,
        pdf_hash      text,
        page_count    integer,
        table_count   integer NOT NULL DEFAULT 0,
        row_count     integer NOT NULL DEFAULT 0,
        uploaded_by   text,
        created_at    timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_berthing_document_hash UNIQUE (pdf_hash))""",
    "CREATE INDEX IF NOT EXISTS idx_brdoc_terminal ON core.berthing_report_document (terminal, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_brdoc_created ON core.berthing_report_document (id DESC)",
    """CREATE TABLE IF NOT EXISTS core.berthing_report_table (
        id                bigserial PRIMARY KEY,
        document_id       bigint NOT NULL
                          REFERENCES core.berthing_report_document (id) ON DELETE CASCADE,
        terminal          text,
        table_name        text NOT NULL,
        panel_index       integer NOT NULL DEFAULT 0,
        page_number       integer NOT NULL DEFAULT 1,
        original_columns  jsonb NOT NULL,
        rows              jsonb NOT NULL,
        row_count         integer NOT NULL DEFAULT 0,
        extraction_note   text,
        created_at        timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_brt_doc ON core.berthing_report_table (document_id, panel_index)",
    "CREATE INDEX IF NOT EXISTS idx_brt_name ON core.berthing_report_table (terminal, table_name)",

    # ==================================================================
    # Migration 0120 - data_origin provenance ('API' | 'MANUAL') for the berthing
    # import ledger, and the per-origin file-hash uniqueness that replaces the
    # single-origin one. Reproduced from infra/postgres/v3/0120_data_origin_provenance.sql
    # section 2, statement for statement, and mirroring what gateway/marine_ext.py already
    # does for core.marine_import_files.
    #
    # WHY IT IS HERE. services/berthing/repository.py de-dupes on
    # (file_hash, data_origin) - `WHERE file_hash = :h AND data_origin = :o`. Without this
    # block that column does not exist on a database this repository provisions, and the
    # FIRST berthing import dies with UndefinedColumnError. It only ever worked because the
    # shared instance had 0120 applied to it; berthing_ext mirrored 0036/0037 and stopped
    # there. The UPDATE that back-tags existing 'jnpa-api' rows is included so an adopted
    # database gets the same provenance the migration would have given it.
    # ==================================================================
    "ALTER TABLE core.berthing_import_file "
    "ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT 'MANUAL'",
    "UPDATE core.berthing_import_file SET data_origin = 'API' "
    "WHERE uploaded_by = 'jnpa-api' AND data_origin <> 'API'",
    "ALTER TABLE core.berthing_import_file "
    "DROP CONSTRAINT IF EXISTS uq_berthing_import_file_hash",
    "DROP INDEX IF EXISTS core.uq_berthing_import_file_hash",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_berthing_import_file_hash_origin "
    "ON core.berthing_import_file (file_hash, data_origin)",
]


async def ensure_berthing_schema(dsn: Optional[str] = None) -> None:
    """Create the berthing_* tables if absent. Idempotent."""
    if os.getenv("JNPA_RUNTIME_DDL", "0") != "1":
        # schema-v3: DDL is owned by infra/postgres/v3 migrations, never runtime.
        return
    from sqlalchemy import text

    from jnpa_shared.db import get_engine

    engine = get_engine(dsn)
    async with engine.begin() as conn:
        for stmt in _DDL:
            await conn.execute(text(stmt))
    log.info("berthing_schema_ready")


__all__ = ["ensure_berthing_schema"]
