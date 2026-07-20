-- 0034_cfs_ecy_upload.sql
-- CFS-ECY — reusable Data Upload sub-module (UC-III module 13).
--
-- PURELY ADDITIVE. Lets CONTROL_ROOM / CUSTOMS / ADMIN users upload future
-- CFS-CODECO / ECY-CODECO files (CSV/XLS/XLSX) through the UI without developer
-- help. It REUSES the existing movement pipeline end to end:
--   * jnpa.cfs_ecy_movements  — the SAME target table (migration 0027), written via
--     the SAME (facility_type, container_number, event_ts, mode) UNIQUE key
--     (ON CONFLICT DO NOTHING — idempotent, duplicate-safe, never overwrites).
--   * a new nullable movements.import_file_id column links each uploaded row back
--     to its upload for audit + honest per-file counts (existing 1,928 CODECO rows
--     stay NULL, unaffected).
--
-- Unlike Shipping Lines, CFS-ECY had NO import-ledger tables, so this migration
-- CREATES them (still additive — CREATE TABLE IF NOT EXISTS, nothing dropped/altered):
--   * jnpa.cfs_ecy_import_files   — import ledger / upload history (one row per file)
--   * jnpa.cfs_ecy_import_errors  — per-row validation / import errors
--
-- It does NOT touch cargo / customs / gate / vehicle / driver / auth tables. Fully
-- idempotent. The identical DDL is embedded in gateway/cfs_ecy_ext.py (applied at
-- gateway boot) and asserted in lock-step by tests/test_cfs_ecy_upload_schema.py.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0034_cfs_ecy_upload.sql

CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

-- Link an uploaded movement row back to the upload that produced it (audit + honest
-- per-file imported count). NULL for the directory importer / the pre-existing rows.
ALTER TABLE jnpa.cfs_ecy_movements ADD COLUMN IF NOT EXISTS import_file_id bigint;
CREATE INDEX IF NOT EXISTS idx_cfsecy_import_file
    ON jnpa.cfs_ecy_movements (import_file_id);

-- Import ledger / upload history — one row per uploaded file.
CREATE TABLE IF NOT EXISTS jnpa.cfs_ecy_import_files (
    id               bigserial PRIMARY KEY,
    facility_type    text CHECK (facility_type IN ('CFS','ECY')),   -- selected facility
    physical_format  text NOT NULL
                     CHECK (physical_format IN ('CSV','XLS','XLSX')),
    source_file      text,                                          -- original filename
    source_sha256    text,                                          -- content dedup key
    file_size_bytes  bigint,
    record_count     integer NOT NULL DEFAULT 0,                    -- valid rows parsed
    imported_count   integer NOT NULL DEFAULT 0,                    -- newly inserted rows
    error_count      integer NOT NULL DEFAULT 0,                    -- invalid/rejected rows
    duplicate_count  integer NOT NULL DEFAULT 0,                    -- rows already present
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
    CONSTRAINT uq_cfs_ecy_import_file_sha UNIQUE (source_sha256)
);

CREATE INDEX IF NOT EXISTS idx_cfsecy_file_status
    ON jnpa.cfs_ecy_import_files (import_status, id DESC);
CREATE INDEX IF NOT EXISTS idx_cfsecy_file_source
    ON jnpa.cfs_ecy_import_files (source, id DESC);
CREATE INDEX IF NOT EXISTS idx_cfsecy_file_facility
    ON jnpa.cfs_ecy_import_files (facility_type, id DESC);

-- Per-row validation / import errors (FK → import_files, cascade on delete).
CREATE TABLE IF NOT EXISTS jnpa.cfs_ecy_import_errors (
    id               bigserial PRIMARY KEY,
    import_file_id   bigint NOT NULL
                     REFERENCES jnpa.cfs_ecy_import_files (id) ON DELETE CASCADE,
    record_ref       text,                                          -- e.g. "row 12"
    error_code       text NOT NULL,
    error_detail     text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cfsecy_err_file
    ON jnpa.cfs_ecy_import_errors (import_file_id, id);
