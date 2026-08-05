-- 0036_berthing_reports.sql — Berthing Reports (UC-III module 7). Additive + idempotent.
--
-- Normalised terminal berthing schema for the five JNPA container terminals
-- (APMT / BMCT / NSFT / NSICT / NSIGT). The customer source files are per-terminal
-- daily PDF reports (heterogeneous layouts) parsed into one common vessel-call model
-- by services/berthing/pdf_parsers.py; the interactive Data-Upload sub-module ingests
-- the SAME normalised model as CSV/XLS/XLSX (mirrors Shipping Lines / CFS-ECY).
--
-- Strictly additive: creates ONLY new jnpa.berthing_* objects. It NEVER drops or
-- alters cargo / shipping_lines / cfs_ecy / customs / vehicle / driver tables. The
-- vessel_name + voyage_number soft-link to Shipping Lines / Cargo BY VALUE (no FK).
--
-- Required fields (enforced at parse/validate time): terminal, vessel_name,
-- voyage_number. Everything else is nullable — imo_number is absent from every
-- source file, NSFT reports carry no berth column, and ETA appears only in the
-- "Expected" section, so none of those can be mandatory across the real corpus.

CREATE SCHEMA IF NOT EXISTS jnpa;

-- ------------------------------------------------------------------ vessel calls
CREATE TABLE IF NOT EXISTS jnpa.berthing_reports (
    id                    bigserial PRIMARY KEY,
    terminal              text NOT NULL,
    vessel_name           text NOT NULL,
    imo_number            text,                         -- absent in source; kept nullable
    voyage_number         text NOT NULL,                -- JNPA rotation / VIA no (e.g. S0561)
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
    -- One row per vessel-call: the same daily report re-import / consecutive daily
    -- snapshots collapse onto this key (status advances, timestamps fill in).
    CONSTRAINT uq_berthing_call UNIQUE (terminal, voyage_number, vessel_name));

CREATE INDEX IF NOT EXISTS idx_berthing_terminal_status ON jnpa.berthing_reports (terminal, status);
CREATE INDEX IF NOT EXISTS idx_berthing_voyage          ON jnpa.berthing_reports (voyage_number);
CREATE INDEX IF NOT EXISTS idx_berthing_vessel          ON jnpa.berthing_reports (vessel_name);
CREATE INDEX IF NOT EXISTS idx_berthing_eta             ON jnpa.berthing_reports (eta DESC);
CREATE INDEX IF NOT EXISTS idx_berthing_import_file     ON jnpa.berthing_reports (import_file_id);

-- ------------------------------------------------------------------ lifecycle events
CREATE TABLE IF NOT EXISTS jnpa.berthing_events (
    id           bigserial PRIMARY KEY,
    berthing_id  bigint NOT NULL REFERENCES jnpa.berthing_reports (id) ON DELETE CASCADE,
    event_type   text NOT NULL,
    event_time   timestamptz,
    created_by   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    -- Idempotent event generation: one row per (call, milestone).
    CONSTRAINT uq_berthing_event UNIQUE (berthing_id, event_type));

CREATE INDEX IF NOT EXISTS idx_berthing_event_call ON jnpa.berthing_events (berthing_id, id);

-- ------------------------------------------------------------------ import ledger
CREATE TABLE IF NOT EXISTS jnpa.berthing_import_files (
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
    CONSTRAINT uq_berthing_import_file_hash UNIQUE (file_hash));

CREATE INDEX IF NOT EXISTS idx_berthing_file_status   ON jnpa.berthing_import_files (status, id DESC);
CREATE INDEX IF NOT EXISTS idx_berthing_file_terminal ON jnpa.berthing_import_files (terminal, id DESC);
CREATE INDEX IF NOT EXISTS idx_berthing_file_source   ON jnpa.berthing_import_files (source, id DESC);

-- ------------------------------------------------------------------ error ledger
CREATE TABLE IF NOT EXISTS jnpa.berthing_import_errors (
    id              bigserial PRIMARY KEY,
    import_file_id  bigint NOT NULL
                    REFERENCES jnpa.berthing_import_files (id) ON DELETE CASCADE,
    row_number      integer,
    error_message   text,
    raw_data        text,
    created_at      timestamptz NOT NULL DEFAULT now());

CREATE INDEX IF NOT EXISTS idx_berthing_err_file ON jnpa.berthing_import_errors (import_file_id, id);
