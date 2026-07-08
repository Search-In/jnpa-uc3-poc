#!/usr/bin/env bash
# =============================================================================
# rds-preflight.sh — run ON EC2 before any migration.
# Answers the two questions that decide the whole migration strategy:
#   (1) Can we reach RDS with SSL from the box?
#   (2) Does this RDS support the timescaledb extension? (init.sql needs it)
#
# Uses the running source postgres container as the psql client, so you don't
# need psql installed on the host. No writes are performed.
#
# Usage:
#   export PGPASSWORD='the-real-rds-password'
#   bash deploy/aws/rds-preflight.sh
# =============================================================================
set -euo pipefail

RDS_HOST="${RDS_HOST:-database-1.c5gg8y8cyk0z.ap-south-1.rds.amazonaws.com}"
RDS_PORT="${RDS_PORT:-5432}"
RDS_DB="${RDS_DB:-jnpa3}"
RDS_USER="${RDS_USER:-postgres}"
SRC_CONTAINER="${SRC_CONTAINER:-jnpa-postgres}"   # the local pg container = our psql client
: "${PGPASSWORD:?export PGPASSWORD with the RDS password first}"

RDS_URI="postgresql://${RDS_USER}@${RDS_HOST}:${RDS_PORT}/${RDS_DB}?sslmode=require"
run() { docker exec -e PGPASSWORD="$PGPASSWORD" -i "$SRC_CONTAINER" psql "$RDS_URI" -tAc "$1"; }

echo "== 1. Connectivity + SSL =="
run "SELECT 'connected as '||current_user||' to '||current_database()||' server '||version();"
echo
echo "== 2. Is the connection actually encrypted? =="
run "SELECT ssl, version, cipher FROM pg_stat_ssl WHERE pid = pg_backend_pid();"
echo
echo "== 3. timescaledb availability on this RDS =="
run "SELECT COALESCE((SELECT default_version FROM pg_available_extensions WHERE name='timescaledb'),'NOT-AVAILABLE') AS timescaledb;"
echo
echo "== 4. Existing content (is jnpa3 already populated?) =="
run "SELECT COALESCE((SELECT count(*)::text FROM information_schema.tables WHERE table_schema='jnpa'),'0')||' tables in schema jnpa';"
echo
echo "== 5. Source (local) row snapshot for later reconciliation =="
docker exec -i "$SRC_CONTAINER" psql -U postgres -d postgres -tAc \
  "SELECT string_agg(format('%s=%s',relname,n_live_tup),' ' ORDER BY relname)
     FROM pg_stat_user_tables WHERE schemaname='jnpa';" || true
echo
echo "Preflight done. If line 3 says NOT-AVAILABLE, RDS is vanilla Postgres and"
echo "the schema needs the timescale-conversion path (report back before migrating)."
