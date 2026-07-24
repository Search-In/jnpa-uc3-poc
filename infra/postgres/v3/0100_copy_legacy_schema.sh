#!/usr/bin/env bash
# ============================================================================
# 0100  Scenario-B step 0: copy the legacy `jnpa` schema from the OLD database
#       (jnpa3) into the TARGET database (jnpa_schema_v3) so the same-database
#       backfill scripts (0201/0202/0203) can run INSERT ... SELECT FROM jnpa.*.
#
# Usage:
#   PGHOST=... PGUSER=postgres PGPASSWORD=... ./0100_copy_legacy_schema.sh
#
# Idempotent-ish: refuses to run if jnpa already exists in the target (drop it
# first with 0900_drop_legacy_schema.sql if you intend a re-copy).
#
# After 0201/0202/0203 have completed AND the deployment is verified, remove
# the copied schema from the target with 0900_drop_legacy_schema.sql. The
# original legacy data in database jnpa3 is never touched and remains the
# rollback reference.
# ============================================================================
set -euo pipefail

SRC_DB="${SRC_DB:-jnpa3}"
DST_DB="${DST_DB:-jnpa_schema_v3}"

echo ">> preflight: target must not already contain schema jnpa"
EXISTS=$(psql -d "$DST_DB" -tAc \
  "SELECT 1 FROM pg_namespace WHERE nspname = 'jnpa'")
if [ "$EXISTS" = "1" ]; then
  echo "!! schema jnpa already exists in $DST_DB — aborting (drop it first)" >&2
  exit 1
fi

echo ">> preflight: source must contain schema jnpa"
SRC_OK=$(psql -d "$SRC_DB" -tAc \
  "SELECT 1 FROM pg_namespace WHERE nspname = 'jnpa'")
if [ "$SRC_OK" != "1" ]; then
  echo "!! schema jnpa not found in $SRC_DB — wrong source database?" >&2
  exit 1
fi

echo ">> copying schema jnpa: $SRC_DB -> $DST_DB (pg_dump | psql, single stream)"
pg_dump -d "$SRC_DB" --schema=jnpa --no-owner --no-privileges \
  | psql -d "$DST_DB" -v ON_ERROR_STOP=1 -q

echo ">> row-count spot check"
for t in transporters driver_master cargo alerts gate_events; do
  SRC_N=$(psql -d "$SRC_DB" -tAc "SELECT count(*) FROM jnpa.$t")
  DST_N=$(psql -d "$DST_DB" -tAc "SELECT count(*) FROM jnpa.$t")
  echo "   jnpa.$t: src=$SRC_N dst=$DST_N"
  [ "$SRC_N" = "$DST_N" ] || { echo "!! count mismatch on jnpa.$t" >&2; exit 1; }
done

echo ">> OK — now run 0101..0104, 0201..0203 against $DST_DB"
