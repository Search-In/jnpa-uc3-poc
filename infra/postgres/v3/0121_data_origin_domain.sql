-- ============================================================
-- 0121  data_origin on the DOMAIN (dashboard-read) tables
-- Companion to 0120 (which tagged the import ledgers + per-origin dedup).
--
-- Adds data_origin ('API' | 'MANUAL', default 'MANUAL') to every primary corpus
-- table a dashboard list/read endpoint filters on, so a LIVE/DEMO request can
-- narrow the rows by provenance. Write path sets it from the import's origin
-- (uploaded_by = 'jnpa-api' ⇒ 'API'); reads add `AND data_origin = :mode`.
--
-- Tolerant loop: a table absent from this schema variant is skipped (to_regclass
-- guard) rather than failing the migration. Existing RDS rows are all manual, so
-- the 'MANUAL' default backfills them correctly. Gate-Documents' core.gate_capture
-- already carries `source_mode` and is intentionally NOT in this list — its
-- reads keep using that existing provenance column.
-- ============================================================
BEGIN;

DO $$
DECLARE
  t   text;
  cn  text;
  tbls text[] := ARRAY[
    -- marine
    'core.vessel_call', 'core.vessel_call_event', 'core.pilotage',
    'core.port_craft', 'core.sea_channel', 'core.bathymetry_survey',
    'core.bathymetry_sounding',
    -- berthing
    'core.berthing_record', 'core.berthing_record_event',
    'core.berthing_report_document', 'core.berthing_report_table',
    -- customs
    'core.customs_message', 'core.igm', 'core.igm_line',
    'core.igm_line_container', 'core.bill_of_entry_ooc', 'core.ooc_item',
    'core.smtp_permit', 'core.smtp_container', 'core.rms_scan_report',
    'core.rms_scan_container', 'core.leo', 'core.shipping_bill',
    'core.customs_event',
    -- shipping lines
    'core.advance_list_container', 'core.delivery_order',
    'core.delivery_order_line', 'core.codeco_movement', 'core.sl_event',
    -- cfs / ecy
    'core.cfs_ecy_movement', 'core.ecy_cfs_chain',
    -- gate documents (gate_capture uses its own source_mode; EIR/PIN get this)
    'core.eir', 'core.pin_ticket',
    -- transporters / drivers
    'core.transporter', 'core.driver',
    -- rail (no read path yet, but tag for completeness/future reads)
    'core.fois_train_intimation', 'core.form11_entry', 'core.cto_manifest_entry',
    -- performance
    'core.perf_daily_snapshot', 'core.perf_daily_traffic',
    'core.perf_daily_tonnage', 'core.perf_daily_terminal_status',
    'core.perf_daily_vessel', 'core.perf_monthly_teu',
    'core.perf_ldb_port_dwell', 'core.perf_ldb_facility_dwell',
    'core.perf_ldb_congestion', 'core.perf_ldb_route_movement',
    'core.perf_ldb_weather'
  ];
BEGIN
  FOREACH t IN ARRAY tbls LOOP
    IF to_regclass(t) IS NOT NULL THEN
      EXECUTE format(
        'ALTER TABLE %s ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT ''MANUAL''', t);
      cn := 'ck_' || split_part(t, '.', 2) || '_data_origin';
      EXECUTE format('ALTER TABLE %s DROP CONSTRAINT IF EXISTS %I', t, cn);
      EXECUTE format(
        'ALTER TABLE %s ADD CONSTRAINT %I CHECK (data_origin IN (''API'',''MANUAL''))', t, cn);
      EXECUTE format(
        'CREATE INDEX IF NOT EXISTS %I ON %s (data_origin)',
        'idx_' || split_part(t, '.', 2) || '_data_origin', t);
    END IF;
  END LOOP;
END $$;

COMMIT;
