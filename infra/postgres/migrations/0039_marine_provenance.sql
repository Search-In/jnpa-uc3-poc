-- 0039_marine_provenance.sql — UC-I Marine: file provenance root. Additive + idempotent.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0039_marine_provenance.sql
--
-- core.ingest_file — one row per physical source file loaded into the core.* model
-- (VESPRO/CALINF/BERMAN XML, PCS report CSVs, pilot-card XLSX, the sea-channel SHP,
-- the berthing/daily PDFs, ...). Every core fact row keeps a source_file reference back
-- to this table so the anomaly write-up can cite the offending file (schema.sql §7).
--
-- STRICTLY ADDITIVE. Creates ONLY new objects in the `core` schema; touches NOTHING in
-- the jnpa schema. It becomes the parent for core.vessel_call_event.source_file, whose
-- FK migration 0038 deferred and migration 0044 re-attaches.
--
-- SOURCE OF TRUTH: schema.sql, section 0 (PROVENANCE & DATA QUALITY), core.ingest_file.
-- Columns, types, nullability and the path UNIQUE are taken verbatim. Deviations are the
-- same schema-wide ones already recorded in migration 0038's ledger:
--   [D1] `core` schema (per schema.sql), not jnpa.
--   [D9] `bigint GENERATED ALWAYS AS IDENTITY`, per schema.sql (not the jnpa bigserial).
-- No deviation is specific to this file.
--
-- ROLLBACK: DROP TABLE core.ingest_file;   (only safe before 0044 attaches the
--           vessel_call_event.source_file FK; drop that constraint first otherwise.)

CREATE SCHEMA IF NOT EXISTS core;

CREATE TABLE IF NOT EXISTS core.ingest_file (
    file_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    path           text NOT NULL UNIQUE,       -- natural key: the source file path
    source_system  text NOT NULL,              -- 'NLP-M','ICEGATE','TOS','FOIS','LDB','PDP',...
    file_format    text NOT NULL,              -- 'xml','csv','xlsx','pdf','jpeg','log','dbf'
    loaded_at      timestamptz NOT NULL DEFAULT now(),
    row_count      integer,
    notes          text);

CREATE INDEX IF NOT EXISTS idx_ingest_file_source ON core.ingest_file (source_system);
CREATE INDEX IF NOT EXISTS idx_ingest_file_loaded ON core.ingest_file (loaded_at DESC);
