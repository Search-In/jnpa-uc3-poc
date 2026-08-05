#!/usr/bin/env bash
# =============================================================================
# use-rds.sh — point an env file at the AWS RDS application database.
#
# Rewrites the five Postgres DSN lines (plus POSTGRES_PASSWORD) in the given
# env file to RDS / jnpa_schema_v3, leaving every other line untouched. A
# timestamped .bak copy is kept next to the file.
#
#   RDS_PASSWORD='…' bash deploy/aws/use-rds.sh .env         # AWS compose stack
#   RDS_PASSWORD='…' bash deploy/aws/use-rds.sh .env.prod    # jnpa-uc3.sh stack
#   RDS_PASSWORD='…' bash deploy/aws/use-rds.sh .env.local   # developer laptop
#
# Without RDS_PASSWORD the script prompts (interactive use). CI passes it from
# the RDS_PASSWORD repository secret — see .github/workflows/deploy.yml step
# "1b-RDS", which runs this on every deploy (idempotent).
#
# URL-encode any of  : / ? # [ ] @  in the password before passing it.
# Overridable: RDS_HOST, RDS_PORT, RDS_DB, RDS_USER.
# Writes: POSTGRES_PASSWORD, POSTGRES_DSN (asyncpg) and the four libpq DSNs
# (RFID_/TRUCK_/CONGESTION_/ANOMALY_). Every other line is left untouched.
# =============================================================================
set -euo pipefail

ENV_FILE="${1:-.env}"
# RDS_HOST is REQUIRED — the endpoint is deliberately not committed (see
# docs/RDS_SECURITY.md). Export it, or pass it inline:  RDS_HOST=... use-rds.sh
RDS_HOST="${RDS_HOST:?RDS_HOST is required — export the RDS endpoint (see docs/RDS_SECURITY.md)}"
RDS_PORT="${RDS_PORT:-5432}"
RDS_DB="${RDS_DB:-jnpa_schema_v3}"
# Least-privilege application role, NOT the postgres superuser. The app must not
# be able to run DDL or read other databases — docs/RDS_SECURITY.md §3 has the
# CREATE ROLE / GRANT script. Override only for a migration run.
RDS_USER="${RDS_USER:-jnpa_app}"

if [[ -z "${RDS_PASSWORD:-}" ]]; then
  read -r -s -p "RDS password for ${RDS_USER}@${RDS_HOST}/${RDS_DB}: " RDS_PASSWORD
  echo
fi
[[ -n "$RDS_PASSWORD" ]] || { echo "ERROR: empty password." >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "ERROR: $ENV_FILE not found." >&2; exit 1; }

BASE="${RDS_USER}:${RDS_PASSWORD}@${RDS_HOST}:${RDS_PORT}/${RDS_DB}"
ASYNC_DSN="postgresql+asyncpg://${BASE}?ssl=require"      # asyncpg: ssl=…
LIBPQ_DSN="postgresql://${BASE}?sslmode=require"          # libpq/psycopg: sslmode=…

# Single rolling backup (not timestamped) so repeated CI runs cannot accumulate
# copies of a secret-bearing file on the box. Same 0600 mode as the env file.
cp -p "$ENV_FILE" "${ENV_FILE}.bak"
chmod 600 "${ENV_FILE}.bak"

# Drop the old values, then append the RDS block.
grep -vE '^(POSTGRES_PASSWORD|POSTGRES_DSN|RFID_POSTGRES_DSN|TRUCK_POSTGRES_DSN|CONGESTION_POSTGRES_DSN|ANOMALY_POSTGRES_DSN)=' \
  "$ENV_FILE" > "${ENV_FILE}.tmp"

cat >> "${ENV_FILE}.tmp" <<EOF

# --- Postgres: AWS RDS ONLY (written by deploy/aws/use-rds.sh) ---------------
# No local postgres container is used; asyncpg needs ssl=, libpq needs sslmode=.
POSTGRES_PASSWORD=${RDS_PASSWORD}
POSTGRES_DSN=${ASYNC_DSN}
RFID_POSTGRES_DSN=${LIBPQ_DSN}
TRUCK_POSTGRES_DSN=${LIBPQ_DSN}
CONGESTION_POSTGRES_DSN=${LIBPQ_DSN}
ANOMALY_POSTGRES_DSN=${LIBPQ_DSN}
EOF

mv "${ENV_FILE}.tmp" "$ENV_FILE"
chmod 600 "$ENV_FILE"

echo ">> ${ENV_FILE} now points at ${RDS_HOST}:${RDS_PORT}/${RDS_DB}"
sed -E 's/:[^:@/]*@/:****@/' <(grep -E '^(POSTGRES_DSN|RFID_POSTGRES_DSN|TRUCK_POSTGRES_DSN|CONGESTION_POSTGRES_DSN|ANOMALY_POSTGRES_DSN)=' "$ENV_FILE")
echo ">> Next: bash deploy/aws/verify-rds.sh ${ENV_FILE}"
