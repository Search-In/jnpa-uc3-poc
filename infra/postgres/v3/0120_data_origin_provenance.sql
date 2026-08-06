-- ============================================================
-- 0120  data_origin provenance — LIVE (JNPA API) vs DEMO (manual import)
-- Additive. Lets the dashboards show either the JNPA-API-sourced corpus or the
-- manually-imported corpus, both resident in RDS side by side.
--
-- Design (user-approved): FILTER BY SOURCE + INGEST BOTH TAGGED SEPARATELY.
--   * every import-ledger row carries data_origin ('API' | 'MANUAL');
--   * the JNPA sync tags its imports 'API' (uploaded_by = 'jnpa-api'); every
--     other importer defaults to 'MANUAL';
--   * dedup becomes PER-ORIGIN: the same bytes delivered by both the API and a
--     manual dump are kept ONCE PER ORIGIN (UNIQUE(sha, data_origin)), so LIVE
--     and DEMO are each a complete dataset.
--
-- The domain rows are tagged + filtered in a companion step (0121) once the read
-- surface is mapped; this migration establishes the ledger-level provenance and
-- the per-origin uniqueness that the write path relies on.
--
-- Existing RDS data is entirely manual (the API path is not yet live), so the
-- 'MANUAL' default + the uploaded_by backfill are correct and lossless.
-- ============================================================
BEGIN;

-- ---- helper values -----------------------------------------------------------
--   data_origin: 'API'    — delivered by the JNPA Simulated Port-Data API sync
--                'MANUAL' — dump import / dashboard upload / directory watch

-- ---- 1. marine (file_hash) ---------------------------------------------------
ALTER TABLE core.marine_import_files
    ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT 'MANUAL';
UPDATE core.marine_import_files SET data_origin = 'API'
    WHERE uploaded_by = 'jnpa-api' AND data_origin <> 'API';
ALTER TABLE core.marine_import_files DROP CONSTRAINT IF EXISTS uq_marine_import_file_hash;
DROP INDEX IF EXISTS core.uq_marine_import_file_hash;
CREATE UNIQUE INDEX IF NOT EXISTS uq_marine_import_file_hash_origin
    ON core.marine_import_files (file_hash, data_origin);

-- ---- 2. berthing (file_hash) -------------------------------------------------
ALTER TABLE core.berthing_import_file
    ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT 'MANUAL';
UPDATE core.berthing_import_file SET data_origin = 'API'
    WHERE uploaded_by = 'jnpa-api' AND data_origin <> 'API';
ALTER TABLE core.berthing_import_file DROP CONSTRAINT IF EXISTS uq_berthing_import_file_hash;
DROP INDEX IF EXISTS core.uq_berthing_import_file_hash;
CREATE UNIQUE INDEX IF NOT EXISTS uq_berthing_import_file_hash_origin
    ON core.berthing_import_file (file_hash, data_origin);

-- ---- 3. customs (source_sha256; ledger has NO uploaded_by column) ------------
ALTER TABLE core.customs_message
    ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT 'MANUAL';
-- (no uploaded_by column here; existing rows are all manual — nothing to backfill)
ALTER TABLE core.customs_message DROP CONSTRAINT IF EXISTS uq_customs_message_sha;
DROP INDEX IF EXISTS core.uq_customs_message_sha;
CREATE UNIQUE INDEX IF NOT EXISTS uq_customs_message_sha_origin
    ON core.customs_message (source_sha256, data_origin);

-- ---- 4. shipping-lines (source_sha256) --------------------------------------
ALTER TABLE core.sl_import_file
    ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT 'MANUAL';
UPDATE core.sl_import_file SET data_origin = 'API'
    WHERE uploaded_by = 'jnpa-api' AND data_origin <> 'API';
ALTER TABLE core.sl_import_file DROP CONSTRAINT IF EXISTS uq_sl_import_file_sha;
DROP INDEX IF EXISTS core.uq_sl_import_file_sha;
CREATE UNIQUE INDEX IF NOT EXISTS uq_sl_import_file_sha_origin
    ON core.sl_import_file (source_sha256, data_origin);

-- ---- 5. cfs-ecy (source_sha256) ---------------------------------------------
ALTER TABLE core.cfs_ecy_import_file
    ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT 'MANUAL';
UPDATE core.cfs_ecy_import_file SET data_origin = 'API'
    WHERE uploaded_by = 'jnpa-api' AND data_origin <> 'API';
ALTER TABLE core.cfs_ecy_import_file DROP CONSTRAINT IF EXISTS uq_cfs_ecy_import_file_sha;
DROP INDEX IF EXISTS core.uq_cfs_ecy_import_file_sha;
CREATE UNIQUE INDEX IF NOT EXISTS uq_cfs_ecy_import_file_sha_origin
    ON core.cfs_ecy_import_file (source_sha256, data_origin);

-- ---- 6. gate-documents (source_sha256) --------------------------------------
ALTER TABLE core.gate_doc_import_file
    ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT 'MANUAL';
UPDATE core.gate_doc_import_file SET data_origin = 'API'
    WHERE uploaded_by = 'jnpa-api' AND data_origin <> 'API';
ALTER TABLE core.gate_doc_import_file DROP CONSTRAINT IF EXISTS uq_gate_doc_import_sha;
DROP INDEX IF EXISTS core.uq_gate_doc_import_sha;
CREATE UNIQUE INDEX IF NOT EXISTS uq_gate_doc_import_sha_origin
    ON core.gate_doc_import_file (source_sha256, data_origin);

-- ---- 7. transporters/drivers (source_sha256) --------------------------------
ALTER TABLE core.td_import_file
    ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT 'MANUAL';
UPDATE core.td_import_file SET data_origin = 'API'
    WHERE uploaded_by = 'jnpa-api' AND data_origin <> 'API';
ALTER TABLE core.td_import_file DROP CONSTRAINT IF EXISTS uq_td_import_file_sha;
DROP INDEX IF EXISTS core.uq_td_import_file_sha;
CREATE UNIQUE INDEX IF NOT EXISTS uq_td_import_file_sha_origin
    ON core.td_import_file (source_sha256, data_origin);

-- ---- 8. rail (source_sha256) ------------------------------------------------
ALTER TABLE core.rail_import_file
    ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT 'MANUAL';
UPDATE core.rail_import_file SET data_origin = 'API'
    WHERE uploaded_by = 'jnpa-api' AND data_origin <> 'API';
ALTER TABLE core.rail_import_file DROP CONSTRAINT IF EXISTS uq_rail_import_file_sha;
DROP INDEX IF EXISTS core.uq_rail_import_file_sha;
CREATE UNIQUE INDEX IF NOT EXISTS uq_rail_import_file_sha_origin
    ON core.rail_import_file (source_sha256, data_origin);

-- ---- 9. performance (no sha dedup; upsert REPLACES on report keys) -----------
-- perf_upload carries the ledger tag; the per-terminal/day domain upsert keys
-- are widened to include data_origin so an API report and a MANUAL report for
-- the same date are both kept (see 0121 for the domain columns/keys).
ALTER TABLE core.perf_upload
    ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT 'MANUAL';
UPDATE core.perf_upload SET data_origin = 'API'
    WHERE uploaded_by = 'jnpa-api' AND data_origin <> 'API';

-- ---- add the API/MANUAL CHECK on every tagged ledger -------------------------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'core.marine_import_files','core.berthing_import_file','core.customs_message',
    'core.sl_import_file','core.cfs_ecy_import_file','core.gate_doc_import_file',
    'core.td_import_file','core.rail_import_file','core.perf_upload']
  LOOP
    EXECUTE format(
      'ALTER TABLE %s DROP CONSTRAINT IF EXISTS %I',
      t, 'ck_' || split_part(t,'.',2) || '_data_origin');
    EXECUTE format(
      'ALTER TABLE %s ADD CONSTRAINT %I CHECK (data_origin IN (''API'',''MANUAL''))',
      t, 'ck_' || split_part(t,'.',2) || '_data_origin');
  END LOOP;
END $$;

COMMIT;
