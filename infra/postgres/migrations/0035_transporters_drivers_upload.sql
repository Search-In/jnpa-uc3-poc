-- 0035_transporters_drivers_upload.sql
-- Transporters & Drivers — reusable Data Upload sub-module (UC-III).
--
-- PURELY ADDITIVE. Lets CONTROL_ROOM / CUSTOMS / ADMIN users upload future
-- Transporter and Driver master data (CSV/XLS/XLSX) through the UI without
-- developer help. It REUSES the existing master tables end to end:
--   * jnpa.transporters   — the SAME target (migration 0024/0025), upserted on the
--     UNIQUE source_company_id key (the same key scripts/import_transporter_master.py
--     uses) → idempotent, duplicate-safe.
--   * jnpa.driver_master  — the SAME target (migration 0026), upserted on the UNIQUE
--     licence_no_norm key (the same key scripts/import_driver_master.py uses).
--   * a new nullable <table>.import_file_id column links each uploaded row back to its
--     upload for audit + honest per-file counts (existing rows stay NULL, unaffected).
--
-- Like CFS-ECY (migration 0034), this is ONE combined module with a TRANSPORTER /
-- DRIVER selector, so a single shared import ledger carries both — mirroring the
-- CFS-ECY facility(CFS/ECY) dimension with an entity_type(TRANSPORTER/DRIVER) one:
--   * jnpa.td_import_files   — import ledger / upload history (one row per file)
--   * jnpa.td_import_errors  — per-row validation / import errors
--
-- It does NOT touch cargo / customs / gate / vehicle / driver-login / auth tables, and
-- it does NOT modify existing production rows except via the intended upsert. Fully
-- idempotent. The identical DDL is embedded in gateway/td_upload_ext.py (applied at
-- gateway boot) and asserted in lock-step by tests/test_td_upload_schema.py.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0035_transporters_drivers_upload.sql

CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

-- Link an upserted master row back to the upload that produced/last-touched it
-- (audit + honest per-file counts). NULL for the CLI importers / the pre-existing rows.
ALTER TABLE jnpa.transporters   ADD COLUMN IF NOT EXISTS import_file_id bigint;
ALTER TABLE jnpa.driver_master  ADD COLUMN IF NOT EXISTS import_file_id bigint;
CREATE INDEX IF NOT EXISTS idx_transporters_import_file  ON jnpa.transporters  (import_file_id);
CREATE INDEX IF NOT EXISTS idx_driver_master_import_file ON jnpa.driver_master (import_file_id);

-- Import ledger / upload history — one row per uploaded file (both entity types).
CREATE TABLE IF NOT EXISTS jnpa.td_import_files (
    id               bigserial PRIMARY KEY,
    entity_type      text NOT NULL
                     CHECK (entity_type IN ('TRANSPORTER','DRIVER')),  -- selected type
    physical_format  text NOT NULL
                     CHECK (physical_format IN ('CSV','XLS','XLSX')),
    source_file      text,                                          -- original filename
    source_sha256    text,                                          -- content dedup key
    file_size_bytes  bigint,
    record_count     integer NOT NULL DEFAULT 0,                    -- valid rows parsed
    imported_count   integer NOT NULL DEFAULT 0,                    -- rows inserted or updated
    error_count      integer NOT NULL DEFAULT 0,                    -- invalid/rejected rows
    duplicate_count  integer NOT NULL DEFAULT 0,                    -- in-file / already-present rows
    import_status    text NOT NULL DEFAULT 'PENDING'
                     CHECK (import_status IN
                            ('PENDING','SUCCESS','PARTIAL','FAILED','SKIPPED_DUPLICATE')),
    error_detail     text,
    uploaded_by      text,                                          -- audit: who uploaded
    source           text NOT NULL DEFAULT 'UPLOAD'
                     CHECK (source IN ('DIRECTORY','UPLOAD')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    -- Content-level idempotency: re-uploading the exact same bytes is a no-op.
    CONSTRAINT uq_td_import_file_sha UNIQUE (source_sha256)
);

CREATE INDEX IF NOT EXISTS idx_td_file_status ON jnpa.td_import_files (import_status, id DESC);
CREATE INDEX IF NOT EXISTS idx_td_file_source ON jnpa.td_import_files (source, id DESC);
CREATE INDEX IF NOT EXISTS idx_td_file_entity ON jnpa.td_import_files (entity_type, id DESC);

-- Per-row validation / import errors (FK → import_files, cascade on delete).
CREATE TABLE IF NOT EXISTS jnpa.td_import_errors (
    id               bigserial PRIMARY KEY,
    import_file_id   bigint NOT NULL
                     REFERENCES jnpa.td_import_files (id) ON DELETE CASCADE,
    record_ref       text,                                          -- e.g. "row 12"
    error_code       text NOT NULL,
    error_detail     text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_td_err_file ON jnpa.td_import_errors (import_file_id, id);
