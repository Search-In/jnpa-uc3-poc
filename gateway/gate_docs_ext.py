"""Gate Document schema bootstrap (idempotent, additive).

Applies the same DDL as infra/postgres/v3/0112_gate_documents.sql at gateway boot
so a dev/mock database that never ran the migration still gets the EIR / PIN /
Form-13 tables lazily — the pattern gateway/cfs_ecy_ext and gateway/customs_ext
already use (the gateway image does not ship infra/, so the DDL is embedded).

Every statement is CREATE ... IF NOT EXISTS: running it against a DB that already
has the objects (because the migration ran) is a no-op. It DROPS/ALTERS nothing.

Gated by JNPA_RUNTIME_DDL=1 — under schema-v3 the migrations own DDL.
The _DDL list MUST stay in lock-step with migration 0112; tests/test_gate_documents.py
asserts both define the same table set.
"""
from __future__ import annotations

import os
from typing import Optional

from .logging import get_logger

log = get_logger("gateway.gate_docs_ext")

_DDL: list[str] = [
    "CREATE SCHEMA IF NOT EXISTS core",
    """CREATE TABLE IF NOT EXISTS core.gate_doc_import_file (
        id               bigserial PRIMARY KEY,
        doc_type         text NOT NULL CHECK (doc_type IN ('EIR','PIN','FORM13')),
        physical_format  text NOT NULL CHECK (physical_format IN ('CSV','XLS','XLSX')),
        source_file      text NOT NULL,
        source_sha256    text NOT NULL,
        file_size_bytes  bigint,
        record_count     integer NOT NULL DEFAULT 0,
        imported_count   integer NOT NULL DEFAULT 0,
        error_count      integer NOT NULL DEFAULT 0,
        duplicate_count  integer NOT NULL DEFAULT 0,
        import_status    text NOT NULL DEFAULT 'PENDING'
                         CHECK (import_status IN ('PENDING','SUCCESS','PARTIAL','FAILED','SKIPPED_DUPLICATE')),
        error_detail     text,
        uploaded_by      text,
        source           text NOT NULL DEFAULT 'UPLOAD' CHECK (source IN ('DIRECTORY','UPLOAD')),
        created_at       timestamptz NOT NULL DEFAULT now(),
        updated_at       timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_gate_doc_import_sha UNIQUE (source_sha256))""",
    "CREATE INDEX IF NOT EXISTS idx_gate_doc_file_type ON core.gate_doc_import_file (doc_type, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_gate_doc_file_status ON core.gate_doc_import_file (import_status, id DESC)",
    """CREATE TABLE IF NOT EXISTS core.gate_doc_import_error (
        id              bigserial PRIMARY KEY,
        import_file_id  bigint NOT NULL REFERENCES core.gate_doc_import_file(id) ON DELETE CASCADE,
        record_ref      text,
        error_code      text NOT NULL,
        error_detail    text,
        created_at      timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_gate_doc_err_file ON core.gate_doc_import_error (import_file_id, id)",
    """CREATE TABLE IF NOT EXISTS core.eir (
        id               bigserial PRIMARY KEY,
        eir_no           text,
        eir_type         text,
        terminal         text,
        container_number text,
        iso_valid        boolean,
        vessel           text,
        via_no           text,
        seal_number      text,
        bat_lane         text,
        truck_no         text NOT NULL,
        driver_name      text,
        driver_licence   text,
        truck_in_time    timestamptz,
        truck_out_time   timestamptz,
        tat_minutes      numeric GENERATED ALWAYS AS (
                             round(extract(epoch FROM (truck_out_time - truck_in_time)) / 60.0)
                         ) STORED,
        gross_weight_mt  numeric,
        company          text,
        cfs_from         text,
        cfs_to           text,
        group_code       text,
        scanner_stamp    text,
        remarks          text,
        row_sha256       text,
        source_file      text,
        import_file_id   bigint REFERENCES core.gate_doc_import_file(id),
        created_at       timestamptz NOT NULL DEFAULT now())""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_eir_row_sha ON core.eir (row_sha256) WHERE row_sha256 IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_eir_container ON core.eir (container_number)",
    "CREATE INDEX IF NOT EXISTS idx_eir_truck ON core.eir (truck_no)",
    "CREATE INDEX IF NOT EXISTS idx_eir_truck_in ON core.eir (truck_in_time DESC NULLS LAST)",
    """CREATE TABLE IF NOT EXISTS core.pin_ticket (
        id               bigserial PRIMARY KEY,
        pin_number       text NOT NULL,
        ticket_type      text,
        terminal         text,
        truck_no         text NOT NULL,
        company          text,
        container_number text,
        iso_valid        boolean,
        group_code       text,
        yard_location    text,
        gate             text,
        move_type        text CHECK (move_type IS NULL OR
                                     move_type IN ('IMPORT_PICK','EXPORT_DROP','EMPTY_PICK','EMPTY_DROP')),
        leg_seq          integer NOT NULL DEFAULT 1,
        issued_at        timestamptz,
        remarks          text,
        row_sha256       text,
        source_file      text,
        import_file_id   bigint REFERENCES core.gate_doc_import_file(id),
        created_at       timestamptz NOT NULL DEFAULT now())""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_pin_row_sha ON core.pin_ticket (row_sha256) WHERE row_sha256 IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_pin_number ON core.pin_ticket (pin_number)",
    "CREATE INDEX IF NOT EXISTS idx_pin_container ON core.pin_ticket (container_number)",
    "CREATE INDEX IF NOT EXISTS idx_pin_truck ON core.pin_ticket (truck_no)",
    # Form-13 creates NO table: it reuses the existing core.gate_capture store
    # (capture_type='FORM13' is already in that table's CHECK). Only the two
    # payload indexes the new query paths need are added here.
    "CREATE INDEX IF NOT EXISTS idx_gate_capture_form13_visit "
    "ON core.gate_capture ((payload->>'visit_id')) WHERE capture_type = 'FORM13'",
    "CREATE INDEX IF NOT EXISTS idx_gate_capture_form13_sha "
    "ON core.gate_capture ((payload->>'row_sha256')) WHERE capture_type = 'FORM13'",
]


async def ensure_gate_doc_schema(dsn: Optional[str] = None) -> None:
    """Create the gate-document tables if absent. Idempotent; safe every boot."""
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
    log.info("gate_doc_schema_ready", statements=applied, total=len(_DDL))
