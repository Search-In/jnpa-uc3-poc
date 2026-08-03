#!/usr/bin/env bash
# =============================================================================
# verify-rds.sh — prove the running stack talks to AWS RDS and nothing else.
#
#   bash deploy/aws/verify-rds.sh [env-file]      # default: .env
#
# Checks, in order:
#   1. every *_POSTGRES_DSN in the env file points at the RDS host + jnpa_schema_v3
#   2. the rendered compose config contains no local-postgres DSN
#   3. no local postgres container is running
#   4. each running service container has an RDS DSN in its environment
#   5. the gateway actually opens a connection to RDS (server address + db name)
#
# Exit code 0 = RDS-only. Non-zero = something still points at a local database.
# =============================================================================
set -uo pipefail

ENV_FILE="${1:-.env}"
RDS_HOST="${RDS_HOST:-database-1.c5gg8y8cyk0z.ap-south-1.rds.amazonaws.com}"
RDS_DB="${RDS_DB:-jnpa_schema_v3}"
COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.aws.yml)
fail=0
mask() { sed -E 's/:[^:@/]*@/:****@/g'; }

echo "== 1. Env file DSNs (${ENV_FILE}) =="
if [[ -f "$ENV_FILE" ]]; then
  for var in POSTGRES_DSN RFID_POSTGRES_DSN TRUCK_POSTGRES_DSN CONGESTION_POSTGRES_DSN ANOMALY_POSTGRES_DSN; do
    line="$(grep -E "^${var}=" "$ENV_FILE" | tail -1)"
    if [[ -z "$line" ]]; then
      echo "  MISSING  ${var}  (compose will refuse to start — this is intended)"; fail=1
    elif [[ "$line" == *"${RDS_HOST}"* && "$line" == *"/${RDS_DB}"* ]]; then
      echo "  OK       $(printf '%s' "$line" | mask)"
    else
      echo "  NOT-RDS  $(printf '%s' "$line" | mask)"; fail=1
    fi
  done
else
  echo "  ERROR: $ENV_FILE not found"; fail=1
fi

echo
echo "== 2. Rendered compose config has no local-postgres DSN =="
cfg="$(docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" config 2>/dev/null)"
if [[ -z "$cfg" ]]; then
  echo "  SKIPPED (compose config failed — run it directly to see the error)"; fail=1
else
  bad="$(printf '%s' "$cfg" | grep -nE '@postgres:5432|@localhost:543|@127\.0\.0\.1:543' || true)"
  if [[ -n "$bad" ]]; then echo "  FOUND local DSNs:"; printf '%s\n' "$bad" | mask; fail=1
  else echo "  OK — no @postgres:5432 / @localhost:543x anywhere in the merged config"; fi
  n="$(printf '%s' "$cfg" | grep -cE "POSTGRES_DSN.*${RDS_HOST}.*/${RDS_DB}")"
  echo "  ${n} service env entries resolve to ${RDS_HOST}/${RDS_DB}"
fi

echo
echo "== 3. No local postgres container running =="
pg="$(docker ps --filter 'name=jnpa-postgres' --format '{{.Names}} {{.Status}}')"
if [[ -n "$pg" ]]; then echo "  RUNNING: $pg  <-- stop it: docker rm -f jnpa-postgres"; fail=1
else echo "  OK — jnpa-postgres is not running"; fi

echo
echo "== 4. Per-container environment =="
for c in $(docker ps --filter 'name=jnpa-' --format '{{.Names}}'); do
  envs="$(docker exec "$c" env 2>/dev/null | grep -E '^(POSTGRES_DSN|POSTGRES_DSN_LIBPQ|CONGESTION_POSTGRES_DSN|ANOMALY_POSTGRES_DSN)=' || true)"
  [[ -z "$envs" ]] && continue
  while IFS= read -r e; do
    if [[ "$e" == *"${RDS_HOST}"* && "$e" == *"/${RDS_DB}"* ]]; then
      printf '  OK       %-28s %s\n' "$c" "$(printf '%s' "$e" | mask)"
    else
      printf '  NOT-RDS  %-28s %s\n' "$c" "$(printf '%s' "$e" | mask)"; fail=1
    fi
  done <<< "$envs"
done

echo
echo "== 5. Live connection from the gateway =="
docker exec jnpa-gateway python - <<'PY' 2>/dev/null || { echo "  SKIPPED (gateway not running)"; fail=1; }
import asyncio, os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

async def main():
    dsn = os.environ["POSTGRES_DSN"]
    engine = create_async_engine(dsn)
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT current_database() AS db,"
            "       inet_server_addr()::text AS server,"
            "       (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()) AS ssl"
        ))).mappings().one()
    await engine.dispose()
    print(f"  OK       db={row['db']} server={row['server']} ssl={row['ssl']}")

asyncio.run(main())
PY

echo
if [[ "$fail" -eq 0 ]]; then echo "RESULT: RDS-only ✔"; else echo "RESULT: issues found ✘ (see NOT-RDS / MISSING lines above)"; fi
exit "$fail"
