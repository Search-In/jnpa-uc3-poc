-- 0046_marine_multiformat_persistence.sql — UC-I Marine: multi-format persistence.
-- Additive + idempotent.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0046_marine_multiformat_persistence.sql
--
-- Enables persisting the normalized PCS parser output (VESPRO/CALINF/BERMAN/VESARR/
-- VESDEP) produced by services/marine/parsers. Three additive changes, all in core.*;
-- touches NOTHING in jnpa and NEVER drops data.
--
--   (1) Widen marine_import_files.physical_format CHECK to accept the new envelopes
--       'XML' (direct PCS files) and 'LOG' (VESARR/VESDEP transmission logs).
--   (2) Add the pre-VCN dedup key: a CALINF seed has vcn IS NULL, so uq_vessel_call_vcn
--       cannot dedupe it — this partial unique index on (imo_no, voyage_no) WHERE
--       vcn IS NULL is both the dedup key and the ON CONFLICT target for the CALINF
--       upsert, and the anchor a later BERMAN promotion updates.
--   (3) Record the PCS document_type per uploaded file (import-history requirement).
--
-- ROLLBACK:
--   DROP INDEX IF EXISTS core.uq_vessel_call_imo_voyage_pre_vcn;
--   ALTER TABLE core.marine_import_files DROP COLUMN IF EXISTS document_type;
--   -- and restore the original physical_format CHECK (CSV/XLS/XLSX/PDF).

CREATE SCHEMA IF NOT EXISTS core;

-- ---------------------------------------------------------------------------
-- (1) physical_format CHECK: add 'XML','LOG'. Guarded drop+re-add so a re-run is a
--     no-op. Existing rows are 'CSV' and remain valid under the widened CHECK.
-- ---------------------------------------------------------------------------
DO $$
DECLARE v_conname text;
BEGIN
    SELECT c.conname INTO v_conname
    FROM pg_constraint c
    WHERE c.conrelid = 'core.marine_import_files'::regclass
      AND c.contype = 'c'
      AND pg_get_constraintdef(c.oid) ILIKE '%physical_format%'
      AND pg_get_constraintdef(c.oid) NOT ILIKE '%XML%';
    IF v_conname IS NOT NULL THEN
        EXECUTE format('ALTER TABLE core.marine_import_files DROP CONSTRAINT %I', v_conname);
        ALTER TABLE core.marine_import_files
            ADD CONSTRAINT marine_import_files_physical_format_check
            CHECK (physical_format IN ('CSV','XLS','XLSX','PDF','XML','LOG'));
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- (2) Pre-VCN dedup key.
--     SAFETY (decision 2): refuse to build the unique index if duplicate pre-VCN
--     (imo_no, voyage_no) groups already exist — fail loudly with a clear message
--     rather than let CREATE UNIQUE INDEX emit a raw "could not create unique index".
-- ---------------------------------------------------------------------------
DO $$
DECLARE v_dups int;
BEGIN
    SELECT count(*) INTO v_dups FROM (
        SELECT imo_no, voyage_no
        FROM core.vessel_call
        WHERE vcn IS NULL AND imo_no IS NOT NULL AND voyage_no IS NOT NULL
        GROUP BY imo_no, voyage_no
        HAVING count(*) > 1
    ) d;
    IF v_dups > 0 THEN
        RAISE EXCEPTION
            'Cannot create uq_vessel_call_imo_voyage_pre_vcn: % duplicate pre-VCN (imo_no, voyage_no) group(s) exist in core.vessel_call. Dedupe them before applying 0046.', v_dups;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_vessel_call_imo_voyage_pre_vcn
    ON core.vessel_call (imo_no, voyage_no)
    WHERE vcn IS NULL;

-- ---------------------------------------------------------------------------
-- (3) Import history: PCS document type per uploaded file. Additive, nullable.
-- ---------------------------------------------------------------------------
ALTER TABLE core.marine_import_files
    ADD COLUMN IF NOT EXISTS document_type text;
