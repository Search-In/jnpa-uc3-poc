#!/usr/bin/env bash
# ============================================================================
# HOTFIX wrapper: repair the empty core.perf_* tables on the live v3 database.
#
# What it does (all steps abort on first error):
#   1. copies ONLY the 15 jnpa.perf_* source tables from the legacy database
#      (jnpa3) into a temporary `jnpa` schema inside the target database
#   2. runs hotfix_perf_backfill.sql (single transaction, verifies 1:1 counts)
#   3. drops the temporary `jnpa` schema again
#
# The legacy database jnpa3 is only READ. The target's core.perf_* tables are
# currently empty, so the TRUNCATE+INSERT backfill destroys nothing.
#
# Usage:
#   PGHOST=<rds-endpoint> PGUSER=postgres PGPASSWORD=... PGSSLMODE=require \
#     ./infra/postgres/v3/hotfix_perf_backfill_rds.sh
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

SRC_DB="${SRC_DB:-jnpa3}"
DST_DB="${DST_DB:-jnpa_schema_v3}"

TABLES=(perf_daily_snapshot perf_daily_terminal_status perf_daily_tonnage
        perf_daily_traffic perf_daily_vessels perf_import_logs
        perf_ldb_congestion perf_ldb_facility_dwell perf_ldb_port_dwell
        perf_ldb_route_movement perf_ldb_weather perf_monthly_teu
        perf_terminals perf_upload_errors perf_uploads)

echo ">> preflight: target must not already contain schema jnpa"
EXISTS=$(psql -d "$DST_DB" -tAc "SELECT 1 FROM pg_namespace WHERE nspname='jnpa'")
if [ "$EXISTS" = "1" ]; then
  echo "!! schema jnpa already exists in $DST_DB — drop it first (0900) or investigate" >&2
  exit 1
fi

echo ">> preflight: core.perf_daily_snapshot in $DST_DB must be empty"
N=$(psql -d "$DST_DB" -tAc "SELECT count(*) FROM core.perf_daily_snapshot")
if [ "$N" != "0" ]; then
  echo "!! core.perf_daily_snapshot already has $N rows — refusing to overwrite" >&2
  exit 1
fi

echo ">> copying ${#TABLES[@]} jnpa.perf_* tables: $SRC_DB -> $DST_DB"
psql -d "$DST_DB" -v ON_ERROR_STOP=1 -qc "CREATE SCHEMA jnpa"
TABLE_ARGS=()
for t in "${TABLES[@]}"; do TABLE_ARGS+=("--table=jnpa.$t"); done
pg_dump -d "$SRC_DB" "${TABLE_ARGS[@]}" --no-owner --no-privileges \
  | psql -d "$DST_DB" -v ON_ERROR_STOP=1 -q

echo ">> running backfill (single transaction, verifies row counts)"
psql -d "$DST_DB" -v ON_ERROR_STOP=1 -f hotfix_perf_backfill.sql

echo ">> dropping temporary schema jnpa from $DST_DB"
psql -d "$DST_DB" -v ON_ERROR_STOP=1 -qc "DROP SCHEMA jnpa CASCADE"

echo ">> done — spot check"
psql -d "$DST_DB" -tAc "SELECT 'core.perf_daily_snapshot rows: '||count(*)||', latest: '||max(report_date) FROM core.perf_daily_snapshot"
