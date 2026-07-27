-- ============================================================================
-- HOTFIX: backfill the (existing but EMPTY) core.perf_* tables from a
-- temporarily-copied jnpa.perf_* schema in the SAME database.
--
-- Context (found 2026-07-27 on live RDS): jnpa_schema_v3 has all 15
-- core.perf_* tables from 0101 but with 0 rows — the perf section of the
-- 0201 backfill never landed. The report data lives in database jnpa3
-- (jnpa.perf_*). Run hotfix_perf_backfill_rds.sh, which copies the source
-- tables over, executes this file, verifies, and drops the temp copy.
--
-- Statements are from 0201_backfill_ported.sql lines 247-306,
-- REORDERED so FK parents load first (perf_upload before perf_import_log /
-- perf_upload_error) — this avoids SET session_replication_role entirely.
-- ADAPTED for the live legacy schema, which predates the upload-ledger
-- columns (source_file on non-snapshot tables, upload_id, uploaded_at,
-- file_format, updated_count): those select NULL (0 for updated_count).
-- Single transaction: any error leaves the database untouched.
--
-- Usage (via wrapper) or directly:
--   psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/v3/hotfix_perf_backfill.sql
-- ============================================================================
BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                 WHERE table_schema='jnpa' AND table_name='perf_daily_snapshot') THEN
    RAISE EXCEPTION 'source jnpa.perf_* not found in this database - run the .sh wrapper first';
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- dimension + FK parents first
-- ---------------------------------------------------------------------------
TRUNCATE core.perf_terminal CASCADE;
INSERT INTO core.perf_terminal (id, code, full_name, operator, terminal_type, is_container, aliases, sort_order, created_at)
  SELECT id, code, full_name, operator, terminal_type, is_container, aliases, sort_order, created_at FROM jnpa.perf_terminals;
SELECT setval('core.perf_terminal_id_seq', coalesce((SELECT max(id) FROM core.perf_terminal), 0) + 1, false);
TRUNCATE core.perf_upload CASCADE;
INSERT INTO core.perf_upload (id, upload_id, report_type, original_filename, file_size_bytes, status, uploaded_by, row_count, inserted_count, skipped_count, error_count, notes, created_at, completed_at, file_format, updated_count)
  SELECT id, upload_id, report_type, original_filename, file_size_bytes, status, uploaded_by, row_count, inserted_count, skipped_count, error_count, notes, created_at, completed_at, NULL, 0 FROM jnpa.perf_uploads;
SELECT setval('core.perf_upload_id_seq', coalesce((SELECT max(id) FROM core.perf_upload), 0) + 1, false);
TRUNCATE core.perf_upload_error CASCADE;
INSERT INTO core.perf_upload_error (id, upload_id, row_number, column_name, error_code, error_detail, raw_value, created_at)
  SELECT id, upload_id, row_number, column_name, error_code, error_detail, raw_value, created_at FROM jnpa.perf_upload_errors;
SELECT setval('core.perf_upload_error_id_seq', coalesce((SELECT max(id) FROM core.perf_upload_error), 0) + 1, false);
TRUNCATE core.perf_import_log CASCADE;
INSERT INTO core.perf_import_log (id, upload_id, phase, level, message, target_table, affected_rows, created_at)
  SELECT id, upload_id, phase, level, message, target_table, affected_rows, created_at FROM jnpa.perf_import_logs;
SELECT setval('core.perf_import_log_id_seq', coalesce((SELECT max(id) FROM core.perf_import_log), 0) + 1, false);

-- ---------------------------------------------------------------------------
-- remaining perf tables
-- ---------------------------------------------------------------------------
TRUNCATE core.perf_daily_snapshot CASCADE;
INSERT INTO core.perf_daily_snapshot (id, report_date, as_of_ts, source_file, created_at, upload_id, uploaded_at)
  SELECT id, report_date, as_of_ts, source_file, created_at, NULL, NULL FROM jnpa.perf_daily_snapshot;
SELECT setval('core.perf_daily_snapshot_id_seq', coalesce((SELECT max(id) FROM core.perf_daily_snapshot), 0) + 1, false);
TRUNCATE core.perf_daily_terminal_status CASCADE;
INSERT INTO core.perf_daily_terminal_status (id, report_date, terminal_code, icd_pendency_teus, cfs_pendency_teus, yard_import_teus, yard_export_teus, yard_transhipment_teus, yard_total_teus, yard_usable_capacity_teus, yard_occupancy_pct, gate_in_teus, gate_out_teus, gate_total_teus, reefer_total_slots, reefer_occupied_slots, reefer_available_slots, created_at, source_file, upload_id, uploaded_at)
  SELECT id, report_date, terminal_code, icd_pendency_teus, cfs_pendency_teus, yard_import_teus, yard_export_teus, yard_transhipment_teus, yard_total_teus, yard_usable_capacity_teus, yard_occupancy_pct, gate_in_teus, gate_out_teus, gate_total_teus, reefer_total_slots, reefer_occupied_slots, reefer_available_slots, created_at, NULL, NULL, NULL FROM jnpa.perf_daily_terminal_status;
SELECT setval('core.perf_daily_terminal_status_id_seq', coalesce((SELECT max(id) FROM core.perf_daily_terminal_status), 0) + 1, false);
TRUNCATE core.perf_daily_tonnage CASCADE;
INSERT INTO core.perf_daily_tonnage (id, report_date, category, period, vessels, liquid_tonnes, dry_bulk_tonnes, break_bulk_tonnes, total_tonnes, created_at, source_file, upload_id, uploaded_at)
  SELECT id, report_date, category, period, vessels, liquid_tonnes, dry_bulk_tonnes, break_bulk_tonnes, total_tonnes, created_at, NULL, NULL, NULL FROM jnpa.perf_daily_tonnage;
SELECT setval('core.perf_daily_tonnage_id_seq', coalesce((SELECT max(id) FROM core.perf_daily_tonnage), 0) + 1, false);
TRUNCATE core.perf_daily_traffic CASCADE;
INSERT INTO core.perf_daily_traffic (id, report_date, terminal_code, period, vessels, imp_teus, exp_teus, total_teus, rakes, rail_dis_teus, rail_ldg_teus, rail_total_teus, created_at, source_file, upload_id, uploaded_at)
  SELECT id, report_date, terminal_code, period, vessels, imp_teus, exp_teus, total_teus, rakes, rail_dis_teus, rail_ldg_teus, rail_total_teus, created_at, NULL, NULL, NULL FROM jnpa.perf_daily_traffic;
SELECT setval('core.perf_daily_traffic_id_seq', coalesce((SELECT max(id) FROM core.perf_daily_traffic), 0) + 1, false);
TRUNCATE core.perf_daily_vessel CASCADE;
INSERT INTO core.perf_daily_vessel (id, report_date, terminal_code, berth_no, via_no, vessel_name, cargo_commodity, berthed_on, expected_completion, created_at, source_file, upload_id, uploaded_at)
  SELECT id, report_date, terminal_code, berth_no, via_no, vessel_name, cargo_commodity, berthed_on, expected_completion, created_at, NULL, NULL, NULL FROM jnpa.perf_daily_vessels;
SELECT setval('core.perf_daily_vessel_id_seq', coalesce((SELECT max(id) FROM core.perf_daily_vessel), 0) + 1, false);
TRUNCATE core.perf_ldb_congestion CASCADE;
INSERT INTO core.perf_ldb_congestion (id, report_month, cycle, cluster_no, cluster_name, cfs_count, pct_containers, congestion_level, created_at, source_file, upload_id, uploaded_at)
  SELECT id, report_month, cycle, cluster_no, cluster_name, cfs_count, pct_containers, congestion_level, created_at, NULL, NULL, NULL FROM jnpa.perf_ldb_congestion;
SELECT setval('core.perf_ldb_congestion_id_seq', coalesce((SELECT max(id) FROM core.perf_ldb_congestion), 0) + 1, false);
TRUNCATE core.perf_ldb_facility_dwell CASCADE;
INSERT INTO core.perf_ldb_facility_dwell (id, report_month, facility_type, facility_name, facility_name_norm, dwell_hours, dwell_hours_prev, created_at, source_file, upload_id, uploaded_at)
  SELECT id, report_month, facility_type, facility_name, facility_name_norm, dwell_hours, dwell_hours_prev, created_at, NULL, NULL, NULL FROM jnpa.perf_ldb_facility_dwell;
SELECT setval('core.perf_ldb_facility_dwell_id_seq', coalesce((SELECT max(id) FROM core.perf_ldb_facility_dwell), 0) + 1, false);
TRUNCATE core.perf_ldb_port_dwell CASCADE;
INSERT INTO core.perf_ldb_port_dwell (id, report_month, terminal_code, cycle, segment, dwell_hours, dwell_hours_prev, created_at, source_file, upload_id, uploaded_at)
  SELECT id, report_month, terminal_code, cycle, segment, dwell_hours, dwell_hours_prev, created_at, NULL, NULL, NULL FROM jnpa.perf_ldb_port_dwell;
SELECT setval('core.perf_ldb_port_dwell_id_seq', coalesce((SELECT max(id) FROM core.perf_ldb_port_dwell), 0) + 1, false);
TRUNCATE core.perf_ldb_route_movement CASCADE;
INSERT INTO core.perf_ldb_route_movement (id, report_month, cycle, transport_mode, route_name, pct_share, created_at, source_file, upload_id, uploaded_at)
  SELECT id, report_month, cycle, transport_mode, route_name, pct_share, created_at, NULL, NULL, NULL FROM jnpa.perf_ldb_route_movement;
SELECT setval('core.perf_ldb_route_movement_id_seq', coalesce((SELECT max(id) FROM core.perf_ldb_route_movement), 0) + 1, false);
TRUNCATE core.perf_ldb_weather CASCADE;
INSERT INTO core.perf_ldb_weather (id, report_month, terminal_code, cycle, weather, dwell_hours, created_at, source_file, upload_id, uploaded_at)
  SELECT id, report_month, terminal_code, cycle, weather, dwell_hours, created_at, NULL, NULL, NULL FROM jnpa.perf_ldb_weather;
SELECT setval('core.perf_ldb_weather_id_seq', coalesce((SELECT max(id) FROM core.perf_ldb_weather), 0) + 1, false);
TRUNCATE core.perf_monthly_teu CASCADE;
INSERT INTO core.perf_monthly_teu (id, fiscal_year, month_date, year_label, month_label, terminal_code, vessel_calls, discharge_teus, load_teus, total_teus, created_at, source_file, upload_id, uploaded_at)
  SELECT id, fiscal_year, month_date, year_label, month_label, terminal_code, vessel_calls, discharge_teus, load_teus, total_teus, created_at, NULL, NULL, NULL FROM jnpa.perf_monthly_teu;
SELECT setval('core.perf_monthly_teu_id_seq', coalesce((SELECT max(id) FROM core.perf_monthly_teu), 0) + 1, false);

-- ---------------------------------------------------------------------------
-- verification: hard-fail unless copy is 1:1
-- ---------------------------------------------------------------------------
DO $$
DECLARE t record; src bigint; dst bigint;
BEGIN
  FOR t IN SELECT * FROM (VALUES
      ('perf_daily_snapshot','perf_daily_snapshot'),
      ('perf_daily_terminal_status','perf_daily_terminal_status'),
      ('perf_daily_tonnage','perf_daily_tonnage'),
      ('perf_daily_traffic','perf_daily_traffic'),
      ('perf_daily_vessels','perf_daily_vessel'),
      ('perf_import_logs','perf_import_log'),
      ('perf_ldb_congestion','perf_ldb_congestion'),
      ('perf_ldb_facility_dwell','perf_ldb_facility_dwell'),
      ('perf_ldb_port_dwell','perf_ldb_port_dwell'),
      ('perf_ldb_route_movement','perf_ldb_route_movement'),
      ('perf_ldb_weather','perf_ldb_weather'),
      ('perf_monthly_teu','perf_monthly_teu'),
      ('perf_terminals','perf_terminal'),
      ('perf_upload_errors','perf_upload_error'),
      ('perf_uploads','perf_upload')) AS v(src_t, dst_t)
  LOOP
    EXECUTE format('SELECT count(*) FROM jnpa.%I', t.src_t) INTO src;
    EXECUTE format('SELECT count(*) FROM core.%I', t.dst_t) INTO dst;
    IF src <> dst THEN
      RAISE EXCEPTION 'row-count mismatch % -> %: src=% dst=%', t.src_t, t.dst_t, src, dst;
    END IF;
    RAISE NOTICE '% -> %: % rows OK', t.src_t, t.dst_t, dst;
  END LOOP;
END $$;

COMMIT;
