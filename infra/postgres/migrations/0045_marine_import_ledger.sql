-- 0045_marine_import_ledger.sql — UC-I Marine: upload import ledger. Additive + idempotent.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0045_marine_import_ledger.sql
--
-- The twin import-ledger tables for the Marine Data-Upload sub-module, mirroring the
-- jnpa.berthing_import_files / jnpa.berthing_import_errors pair (migration 0036) — the
-- repo-wide convention (every upload module owns its own *_import_files / *_import_errors
-- twin; NONE reuses a shared ledger). These are DISTINCT from core.ingest_file (0039),
-- which is the provenance root for the parser/CLI ingestion lineage.
--
-- core.marine_import_files  — one row per uploaded file (audit history + idempotency).
-- core.marine_import_errors — per-row parse/validation errors for one uploaded file.
--
-- STRICTLY ADDITIVE. Creates ONLY new core.* objects; touches NOTHING in jnpa and NOTHING
-- in the existing core.* tables (0038-0044). No FK points INTO vessel_call — the domain
-- rows are not back-linked by a column (vessel_call has no import_file_id); the upload
-- audit is at file level, exactly as berthing tracks it.
--
-- CONSTRAINTS (per the requirement):
--   * file_hash is UNIQUE  -> a byte-identical re-upload is detected and NOT re-inserted
--     (the service returns SKIPPED_DUPLICATE); this is the file-level idempotency anchor.
--   * duplicate upload does not insert duplicate data -> enforced at TWO levels: the
--     file_hash UNIQUE above (file level) and the vessel_call upsert on uq_vessel_call_vcn
--     (row level, in the repository) — neither ever duplicates a row.
--   * import audit history -> every attempt persists a marine_import_files row with its
--     status + row counts; errors persist to marine_import_errors.
--
-- Deviations: [D1] core schema; [D9] IDENTITY PKs — both per migration 0038's ledger.
-- Shape mirrors jnpa.berthing_import_files MINUS the `terminal` column (a vessel_call
-- upload is keyed on VCN, not terminal-scoped).
--
-- ROLLBACK (reverse order):
--   DROP TABLE core.marine_import_errors; DROP TABLE core.marine_import_files;

CREATE SCHEMA IF NOT EXISTS core;

-- ------------------------------------------------------------------ import ledger
CREATE TABLE IF NOT EXISTS core.marine_import_files (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    filename         text,
    file_hash        text,
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
    -- file-level idempotency: identical bytes never import twice
    CONSTRAINT uq_marine_import_file_hash UNIQUE (file_hash));

CREATE INDEX IF NOT EXISTS idx_marine_file_status ON core.marine_import_files (status, id DESC);
CREATE INDEX IF NOT EXISTS idx_marine_file_source ON core.marine_import_files (source, id DESC);

-- ------------------------------------------------------------------ error ledger
CREATE TABLE IF NOT EXISTS core.marine_import_errors (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    import_file_id  bigint NOT NULL
                    REFERENCES core.marine_import_files (id) ON DELETE CASCADE,
    row_number      integer,
    error_message   text,
    raw_data        text,
    created_at      timestamptz NOT NULL DEFAULT now());

CREATE INDEX IF NOT EXISTS idx_marine_err_file ON core.marine_import_errors (import_file_id, id);
