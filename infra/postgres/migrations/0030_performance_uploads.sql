-- =====================================================================
-- 0030_performance_uploads.sql  —  UC-III Module 12: Data Upload Management
-- =====================================================================
-- PURELY ADDITIVE. New sub-module that lets DTCCC_ADMIN users upload JNPA
-- performance data (CSV/XLSX) into the EXISTING jnpa.perf_* dashboard tables.
-- These three tables track the upload lifecycle (history / logs / row errors).
-- They never reference or alter any table outside this sub-module; FKs are
-- internal only. Every statement is CREATE ... IF NOT EXISTS (idempotent).
--
-- Apply:
--   psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0030_performance_uploads.sql
-- (also applied idempotently at gateway boot via
--  gateway/performance_upload_ext.ensure_performance_upload_schema)
-- =====================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

-- gen_random_uuid() is core on PG13+, but pgcrypto also provides it — additive,
-- idempotent, and a no-op where it is already available.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Upload history — one row per validate/import attempt.
CREATE TABLE IF NOT EXISTS jnpa.perf_uploads (
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
    CONSTRAINT uq_perf_uploads_upload_id UNIQUE (upload_id)
);
CREATE INDEX IF NOT EXISTS idx_perf_uploads_created ON jnpa.perf_uploads (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_perf_uploads_type_status ON jnpa.perf_uploads (report_type, status);

-- Import logs — structured events per upload (validate + import phases).
CREATE TABLE IF NOT EXISTS jnpa.perf_import_logs (
    id            bigserial PRIMARY KEY,
    upload_id     uuid NOT NULL REFERENCES jnpa.perf_uploads(upload_id) ON DELETE CASCADE,
    phase         text NOT NULL CHECK (phase IN ('VALIDATE','IMPORT')),
    level         text NOT NULL DEFAULT 'INFO' CHECK (level IN ('INFO','WARN','ERROR')),
    message       text,
    target_table  text,
    affected_rows int,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_perf_import_logs_upload ON jnpa.perf_import_logs (upload_id, created_at);

-- Validation errors — one row per rejected cell/row.
CREATE TABLE IF NOT EXISTS jnpa.perf_upload_errors (
    id           bigserial PRIMARY KEY,
    upload_id    uuid NOT NULL REFERENCES jnpa.perf_uploads(upload_id) ON DELETE CASCADE,
    row_number   int,
    column_name  text,
    error_code   text,
    error_detail text,
    raw_value    text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_perf_upload_errors_upload ON jnpa.perf_upload_errors (upload_id);
