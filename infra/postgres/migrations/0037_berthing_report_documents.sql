-- 0037_berthing_report_documents.sql — Berthing full-fidelity PDF capture (UC-III module 7).
-- Additive + idempotent. Stores EVERY table on each terminal berthing report PDF verbatim
-- (original columns + JSONB rows), alongside — never replacing — the normalised vessel-call
-- model from migration 0036. It creates ONLY new jnpa.berthing_report_* objects and NEVER
-- drops or alters berthing_reports / berthing_events / berthing_import_files /
-- berthing_import_errors (0036), nor any other table.
--
-- See docs/BERTHING_PDF_DATA_AUDIT.md for the layout catalogue and extraction design.

CREATE SCHEMA IF NOT EXISTS jnpa;

-- ------------------------------------------------------------------ one row per uploaded PDF
CREATE TABLE IF NOT EXISTS jnpa.berthing_report_documents (
    id            bigserial PRIMARY KEY,
    file_name     text NOT NULL,
    terminal      text,                       -- APMT|BMCT|NSFT|NSICT|NSIGT (auto-detected)
    report_date   date,                       -- parsed from the PDF body
    pdf_hash      text,                        -- sha256(bytes) → idempotent re-upload
    page_count    integer,
    table_count   integer NOT NULL DEFAULT 0,
    row_count     integer NOT NULL DEFAULT 0,
    uploaded_by   text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_berthing_document_hash UNIQUE (pdf_hash));

CREATE INDEX IF NOT EXISTS idx_brdoc_terminal ON jnpa.berthing_report_documents (terminal, id DESC);
CREATE INDEX IF NOT EXISTS idx_brdoc_created  ON jnpa.berthing_report_documents (id DESC);

-- ------------------------------------------------------------------ one row per extracted panel
CREATE TABLE IF NOT EXISTS jnpa.berthing_report_tables (
    id                bigserial PRIMARY KEY,
    document_id       bigint NOT NULL
                      REFERENCES jnpa.berthing_report_documents (id) ON DELETE CASCADE,
    terminal          text,
    table_name        text NOT NULL,           -- ON_BERTH_VESSEL, VESSELS_EXPECTED, ...
    panel_index       integer NOT NULL DEFAULT 0,
    page_number       integer NOT NULL DEFAULT 1,
    original_columns  jsonb NOT NULL,          -- ["Berth","Vessel","VIA","LOA","Alongside",...]
    rows              jsonb NOT NULL,           -- [{"Berth":"APM01","Vessel":"OOCL LUXEMBOURG",...}]
    row_count         integer NOT NULL DEFAULT 0,
    extraction_note   text,                    -- warnings (e.g. "raw_fallback")
    created_at        timestamptz NOT NULL DEFAULT now());

CREATE INDEX IF NOT EXISTS idx_brt_doc  ON jnpa.berthing_report_tables (document_id, panel_index);
CREATE INDEX IF NOT EXISTS idx_brt_name ON jnpa.berthing_report_tables (terminal, table_name);
