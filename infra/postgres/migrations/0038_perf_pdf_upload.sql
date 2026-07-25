-- =====================================================================
-- 0038_perf_pdf_upload.sql  —  UC-III Module 12 (Performance & Daily Reports)
-- =====================================================================
-- ADDITIVE, non-destructive hardening that lets the CLIENT upload the official
-- JNPA report PDFs through the normal upload workflow (previously CSV/XLSX only,
-- with PDF ingestion available solely via the offline backfill script).
--
-- Three concerns, all idempotent and safe to re-run:
--
--   1. NUMERIC PRECISION
--      migration 0028 declared the measure columns as bare `numeric` (unbounded).
--      Values arrive from the parsers as IEEE-754 doubles, so Postgres stored the
--      full binary expansion and the API served it verbatim:
--          yard_occupancy_pct -> 84.8700000000000045474735088646411895751953125
--          dwell_hours        -> 77.7000000000000028421709430404007434844970703125
--      The UI masked this with toFixed(1), but CSV export wrote the raw string, so
--      the client's exported spreadsheet disagreed with the source PDF.
--      Fixing the DECLARED SCALE fixes storage, API and export in one place, and
--      the ALTER rounds the already-stored rows too (84.87 / 77.70).
--      Scales are chosen to hold the largest real figures in the corpus:
--          tonnes / TEUs  -> numeric(16,2)   (JNPA YEAR total ~ 84,493,898.77)
--          percentages    -> numeric(6,2)    (0.00 .. 100.00)
--          dwell hours    -> numeric(8,2)    (ICD dwell ~ 263.30)
--
--   2. ROW-LEVEL PROVENANCE
--      adds source_file / upload_id / uploaded_at to every perf_* data table so a
--      figure on screen can be traced back to the exact PDF and upload that produced
--      it (and so a bad upload can be identified and re-imported).
--
--   3. UPLOAD LEDGER
--      adds file_format (PDF | XLSX | CSV) and updated_count to jnpa.perf_uploads.
--      updated_count is meaningful because the repository now upserts (ON CONFLICT
--      DO UPDATE) instead of skipping: re-uploading a corrected report REPLACES the
--      previous figures rather than silently discarding the correction.
--
-- Touches nothing outside jnpa.perf_* — no other module, no auth/JWT/RBAC, no data
-- deletion. Column ADDs are IF NOT EXISTS; type ALTERs are guarded on current scale.
--
-- Apply:
--   psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0038_perf_pdf_upload.sql
-- =====================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

-- ---------------------------------------------------------------------
-- 1. numeric precision
-- ---------------------------------------------------------------------
DO $$
DECLARE
    r        record;
    v_scale  int;
    -- table, column, precision, scale
    targets  text[][] := ARRAY[
        ['perf_daily_traffic','imp_teus','16','2'],
        ['perf_daily_traffic','exp_teus','16','2'],
        ['perf_daily_traffic','total_teus','16','2'],
        ['perf_daily_traffic','rail_dis_teus','16','2'],
        ['perf_daily_traffic','rail_ldg_teus','16','2'],
        ['perf_daily_traffic','rail_total_teus','16','2'],
        ['perf_daily_tonnage','liquid_tonnes','16','2'],
        ['perf_daily_tonnage','dry_bulk_tonnes','16','2'],
        ['perf_daily_tonnage','break_bulk_tonnes','16','2'],
        ['perf_daily_tonnage','total_tonnes','16','2'],
        ['perf_daily_terminal_status','icd_pendency_teus','16','2'],
        ['perf_daily_terminal_status','cfs_pendency_teus','16','2'],
        ['perf_daily_terminal_status','yard_import_teus','16','2'],
        ['perf_daily_terminal_status','yard_export_teus','16','2'],
        ['perf_daily_terminal_status','yard_transhipment_teus','16','2'],
        ['perf_daily_terminal_status','yard_total_teus','16','2'],
        ['perf_daily_terminal_status','yard_usable_capacity_teus','16','2'],
        ['perf_daily_terminal_status','yard_occupancy_pct','6','2'],
        ['perf_daily_terminal_status','gate_in_teus','16','2'],
        ['perf_daily_terminal_status','gate_out_teus','16','2'],
        ['perf_daily_terminal_status','gate_total_teus','16','2'],
        ['perf_monthly_teu','discharge_teus','16','2'],
        ['perf_monthly_teu','load_teus','16','2'],
        ['perf_monthly_teu','total_teus','16','2'],
        ['perf_ldb_port_dwell','dwell_hours','8','2'],
        ['perf_ldb_port_dwell','dwell_hours_prev','8','2'],
        ['perf_ldb_facility_dwell','dwell_hours','8','2'],
        ['perf_ldb_facility_dwell','dwell_hours_prev','8','2'],
        ['perf_ldb_weather','dwell_hours','8','2'],
        ['perf_ldb_congestion','pct_containers','6','2'],
        ['perf_ldb_route_movement','pct_share','6','2']
    ];
BEGIN
    FOR i IN 1 .. array_length(targets, 1) LOOP
        IF to_regclass('jnpa.' || targets[i][1]) IS NULL THEN
            CONTINUE;                       -- table not present in this database
        END IF;
        SELECT numeric_scale INTO v_scale
        FROM information_schema.columns
        WHERE table_schema = 'jnpa'
          AND table_name   = targets[i][1]
          AND column_name  = targets[i][2];

        IF v_scale IS NULL THEN
            -- column absent, or unbounded numeric (information_schema reports NULL
            -- scale for `numeric` without a declared precision) -> set the scale.
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_schema='jnpa' AND table_name=targets[i][1]
                         AND column_name=targets[i][2]) THEN
                EXECUTE format('ALTER TABLE jnpa.%I ALTER COLUMN %I TYPE numeric(%s,%s)',
                               targets[i][1], targets[i][2], targets[i][3], targets[i][4]);
                RAISE NOTICE '0038: %.% -> numeric(%,%)',
                    targets[i][1], targets[i][2], targets[i][3], targets[i][4];
            END IF;
        END IF;
    END LOOP;
END $$;

-- ---------------------------------------------------------------------
-- 2. row-level provenance on every perf_* data table
-- ---------------------------------------------------------------------
DO $$
DECLARE
    t   text;
    tbls text[] := ARRAY[
        'perf_daily_traffic', 'perf_daily_tonnage', 'perf_daily_terminal_status',
        'perf_daily_vessels', 'perf_monthly_teu', 'perf_ldb_port_dwell',
        'perf_ldb_facility_dwell', 'perf_ldb_congestion', 'perf_ldb_route_movement',
        'perf_ldb_weather'
    ];
BEGIN
    FOREACH t IN ARRAY tbls LOOP
        IF to_regclass('jnpa.' || t) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format('ALTER TABLE jnpa.%I ADD COLUMN IF NOT EXISTS source_file text', t);
        EXECUTE format('ALTER TABLE jnpa.%I ADD COLUMN IF NOT EXISTS upload_id  uuid', t);
        EXECUTE format('ALTER TABLE jnpa.%I ADD COLUMN IF NOT EXISTS uploaded_at timestamptz', t);
        EXECUTE format('CREATE INDEX IF NOT EXISTS ix_%s_upload ON jnpa.%I (upload_id)', t, t);
    END LOOP;
END $$;

-- perf_daily_snapshot already carries source_file; it needs only the upload linkage.
ALTER TABLE jnpa.perf_daily_snapshot ADD COLUMN IF NOT EXISTS upload_id   uuid;
ALTER TABLE jnpa.perf_daily_snapshot ADD COLUMN IF NOT EXISTS uploaded_at timestamptz;

-- ---------------------------------------------------------------------
-- 3. upload ledger
-- ---------------------------------------------------------------------
ALTER TABLE jnpa.perf_uploads ADD COLUMN IF NOT EXISTS file_format   text;
ALTER TABLE jnpa.perf_uploads ADD COLUMN IF NOT EXISTS updated_count integer NOT NULL DEFAULT 0;

-- Backfill the format of historical uploads from their filename (best effort).
UPDATE jnpa.perf_uploads
   SET file_format = CASE
        WHEN lower(original_filename) LIKE '%.pdf'  THEN 'PDF'
        WHEN lower(original_filename) LIKE '%.xlsx' THEN 'XLSX'
        WHEN lower(original_filename) LIKE '%.xlsm' THEN 'XLSX'
        ELSE 'CSV' END
 WHERE file_format IS NULL;
