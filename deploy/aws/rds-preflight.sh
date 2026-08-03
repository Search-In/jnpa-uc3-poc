#!/usr/bin/env bash
# =============================================================================
# rds-preflight.sh — verify the box can reach the application database (RDS).
# Answers:
#   (1) Can we reach RDS with SSL from this host?
#   (2) Is the connection actually encrypted?
#   (3) Does this RDS have timescaledb available?
#   (4) Is jnpa_schema_v3 populated?
#
# There is no local postgres container any more (it is dev-only, behind the
# "localdb" compose profile), so this uses a throwaway postgres image as the
# psql client — nothing needs to be installed on the host. No writes.
#
# Usage:
#   export PGPASSWORD='the-real-rds-password'
#   bash deploy/aws/rds-preflight.sh
# =============================================================================
set -euo pipefail

RDS_HOST="${RDS_HOST:-database-1.c5gg8y8cyk0z.ap-south-1.rds.amazonaws.com}"
RDS_PORT="${RDS_PORT:-5432}"
RDS_DB="${RDS_DB:-jnpa_schema_v3}"
RDS_USER="${RDS_USER:-postgres}"
# Client major must be >= the RDS server major (PostgreSQL 18).
PSQL_IMAGE="${PSQL_IMAGE:-postgres:18-alpine}"
: "${PGPASSWORD:?export PGPASSWORD with the RDS password first}"

RDS_URI="postgresql://${RDS_USER}@${RDS_HOST}:${RDS_PORT}/${RDS_DB}?sslmode=require"
run() {
  docker run --rm -e PGPASSWORD="$PGPASSWORD" "$PSQL_IMAGE" \
    psql "$RDS_URI" -tAc "$1"
}

echo "== 1. Connectivity + SSL =="
run "SELECT 'connected as '||current_user||' to '||current_database()||' server '||version();"
echo
echo "== 2. Is the connection actually encrypted? =="
run "SELECT ssl, version, cipher FROM pg_stat_ssl WHERE pid = pg_backend_pid();"
echo
echo "== 3. timescaledb availability on this RDS =="
run "SELECT COALESCE((SELECT default_version FROM pg_available_extensions WHERE name='timescaledb'),'NOT-AVAILABLE') AS timescaledb;"
echo
echo "== 4. Existing content (is ${RDS_DB} populated?) =="
run "SELECT string_agg(table_schema||'='||cnt::text, ' ' ORDER BY table_schema)
       FROM (SELECT table_schema, count(*) AS cnt
               FROM information_schema.tables
              WHERE table_schema NOT IN ('pg_catalog','information_schema')
              GROUP BY table_schema) s;"
echo
echo "Preflight done. If line 3 says NOT-AVAILABLE, RDS is vanilla Postgres and"
echo "the schema needs the timescale-conversion path (report back before migrating)."
