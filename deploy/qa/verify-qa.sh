#!/usr/bin/env bash
# =============================================================================
# verify-qa.sh — prove the QA stack talks to RDS/jnpa_qa, is on its own ports,
# and that PRODUCTION is untouched.
#
#   bash deploy/qa/verify-qa.sh [env-file]      # default: .env.qa
#
# This is the QA twin of deploy/aws/verify-rds.sh. It is a separate script on
# purpose: verify-rds.sh filters containers by the substring `jnpa-`, which also
# matches `jnpa-qa-*`, so running it unchanged while QA is up would report the
# QA containers as NOT-RDS. Here everything is scoped by the compose PROJECT
# LABEL (com.docker.compose.project), which is exact.
#
# Checks, in order:
#   1. every *_POSTGRES_DSN in the QA env file points at the RDS host + jnpa_qa
#   2. the rendered QA compose config contains no local-postgres DSN and no
#      reference to the production database
#   3. the QA stack publishes ONLY the QA host ports (18000 / 18080)
#   4. each running QA container has a jnpa_qa DSN in its environment
#   5. the QA gateway actually opens a connection to RDS (db name + ssl)
#   6. PRODUCTION containers are still running and still on jnpa_schema_v3
#
# Exit code 0 = QA is correct and production is intact.
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")/../.."

ENV_FILE="${1:-.env.qa}"
# REQUIRED — the endpoint is deliberately not committed (docs/RDS_SECURITY.md,
# enforced by tests/test_rds_security.py). Export it, or pass it inline:
#   RDS_HOST=... bash deploy/qa/verify-qa.sh
RDS_HOST="${RDS_HOST:?RDS_HOST is required — export the RDS endpoint (see docs/RDS_SECURITY.md)}"
QA_DB="${QA_DB:-jnpa_qa}"
PROD_DB="${PROD_DB:-jnpa_schema_v3}"
QA_PROJECT="${QA_PROJECT:-jnpa-uc3-poc-qa}"
PROD_PROJECT="${PROD_PROJECT:-jnpa-uc3-poc}"
QA_GATEWAY="${QA_GATEWAY:-jnpa-qa-gateway}"
DC=(docker compose --env-file "$ENV_FILE" -p "$QA_PROJECT"
    -f docker-compose.yml -f docker-compose.aws.yml -f docker-compose.qa.yml)
fail=0
mask() { sed -E 's/:[^:@/]*@/:****@/g'; }

echo "== 1. QA env file DSNs (${ENV_FILE}) =="
if [[ -f "$ENV_FILE" ]]; then
  for var in POSTGRES_DSN RFID_POSTGRES_DSN TRUCK_POSTGRES_DSN CONGESTION_POSTGRES_DSN ANOMALY_POSTGRES_DSN; do
    line="$(grep -E "^${var}=" "$ENV_FILE" | tail -1)"
    if [[ -z "$line" ]]; then
      echo "  MISSING  ${var}  (compose will refuse to start — this is intended)"; fail=1
    elif [[ "$line" == *"${PROD_DB}"* ]]; then
      echo "  PROD-DB  ${var} points at ${PROD_DB} — QA must never do this"; fail=1
    elif [[ "$line" == *"${RDS_HOST}"* && "$line" == *"/${QA_DB}"* ]]; then
      echo "  OK       $(printf '%s' "$line" | mask)"
    else
      echo "  NOT-QA   $(printf '%s' "$line" | mask)"; fail=1
    fi
  done
else
  echo "  ERROR: $ENV_FILE not found"; fail=1
fi

echo
echo "== 2. Rendered QA compose config =="
cfg="$("${DC[@]}" config 2>/dev/null)"
if [[ -z "$cfg" ]]; then
  echo "  SKIPPED (compose config failed — run '${DC[*]} config' to see the error)"; fail=1
else
  bad="$(printf '%s' "$cfg" | grep -nE "@postgres:5432|@localhost:543|@127\.0\.0\.1:543|/${PROD_DB}" || true)"
  if [[ -n "$bad" ]]; then echo "  FOUND local or production DSNs:"; printf '%s\n' "$bad" | mask; fail=1
  else echo "  OK — no local-postgres DSN and no ${PROD_DB} anywhere in the merged QA config"; fi
  n="$(printf '%s' "$cfg" | grep -cE "POSTGRES_DSN.*${RDS_HOST}.*/${QA_DB}")"
  echo "  ${n} service env entries resolve to ${RDS_HOST}/${QA_DB}"
fi

echo
echo "== 3. QA publishes only its own host ports =="
if [[ -n "$cfg" ]]; then
  qa_ports="$(printf '%s' "$cfg" | grep -oE 'published: "[0-9]+"' | grep -oE '[0-9]+' | sort -un | tr '\n' ' ')"
  echo "  QA host ports: ${qa_ports:-<none>}"
  # Section-local flag: the global `fail` may already be set by an earlier check,
  # which would wrongly suppress this section's OK line.
  port_fail=0
  for p in 80 443 3000 8000 8210; do
    if [[ " $qa_ports " == *" $p "* ]]; then
      echo "  CONFLICT QA publishes production port ${p}"; port_fail=1
    fi
  done
  # Positive assertion too: anything beyond 18000/18080 is an unintended publish.
  if [[ "$(printf '%s' "$qa_ports" | tr -s ' ' | sed 's/ $//')" != "18000 18080" ]]; then
    echo "  UNEXPECTED QA host ports (want exactly '18000 18080'): ${qa_ports:-<none>}"; port_fail=1
  fi
  [[ "$port_fail" -eq 0 ]] && echo "  OK — exactly 18000/18080; none of 80/443/3000/8000/8210 claimed by QA"
  [[ "$port_fail" -eq 0 ]] || fail=1
fi

echo
echo "== 4. Per-container environment (project=${QA_PROJECT}) =="
qa_containers="$(docker ps --filter "label=com.docker.compose.project=${QA_PROJECT}" --format '{{.Names}}')"
if [[ -z "$qa_containers" ]]; then
  echo "  (no QA containers running — start them with deploy/qa/manage.sh up)"
fi
for c in $qa_containers; do
  envs="$(docker exec "$c" env 2>/dev/null | grep -E '^(POSTGRES_DSN|POSTGRES_DSN_LIBPQ|CONGESTION_POSTGRES_DSN|ANOMALY_POSTGRES_DSN)=' || true)"
  [[ -z "$envs" ]] && continue
  while IFS= read -r e; do
    if [[ "$e" == *"${RDS_HOST}"* && "$e" == *"/${QA_DB}"* ]]; then
      printf '  OK       %-28s %s\n' "$c" "$(printf '%s' "$e" | mask)"
    else
      printf '  NOT-QA   %-28s %s\n' "$c" "$(printf '%s' "$e" | mask)"; fail=1
    fi
  done <<< "$envs"
done

echo
echo "== 5. Live connection from the QA gateway =="
if docker ps --format '{{.Names}}' | grep -qx "$QA_GATEWAY"; then
  docker exec "$QA_GATEWAY" python - <<'PY' || { echo "  FAILED to query the QA database"; fail=1; }
import asyncio, os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

async def main():
    engine = create_async_engine(os.environ["POSTGRES_DSN"])
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT current_database() AS db,"
            "       inet_server_addr()::text AS server,"
            "       (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()) AS ssl"
        ))).mappings().one()
    await engine.dispose()
    assert row["db"] == "jnpa_qa", f"QA gateway is connected to {row['db']}, not jnpa_qa"
    print(f"  OK       db={row['db']} server={row['server']} ssl={row['ssl']}")

asyncio.run(main())
PY
else
  echo "  SKIPPED (${QA_GATEWAY} not running)"
fi

echo
echo "== 6. Production is untouched (project=${PROD_PROJECT}) =="
prod_containers="$(docker ps --filter "label=com.docker.compose.project=${PROD_PROJECT}" --format '{{.Names}}\t{{.Status}}')"
if [[ -z "$prod_containers" ]]; then
  echo "  (no containers with the ${PROD_PROJECT} project label — production may be"
  echo "   managed by deploy/jnpa-uc3.sh instead; check 'docker ps' by hand)"
else
  printf '%s\n' "$prod_containers" | sed 's/^/  /'
  prod_gw="$(printf '%s' "$prod_containers" | awk -F'\t' '/gateway/ {print $1; exit}')"
  if [[ -n "$prod_gw" ]]; then
    dsn="$(docker exec "$prod_gw" env 2>/dev/null | grep -E '^POSTGRES_DSN=' || true)"
    if [[ "$dsn" == *"/${PROD_DB}"* ]]; then
      echo "  OK       ${prod_gw} still on ${PROD_DB}"
    else
      echo "  WARNING  ${prod_gw} DSN is not ${PROD_DB}: $(printf '%s' "$dsn" | mask)"; fail=1
    fi
  fi
fi

echo
if [[ "$fail" -eq 0 ]]; then echo "RESULT: QA -> ${QA_DB}, production intact ✔"; else echo "RESULT: issues found ✘ (see the lines above)"; fi
exit "$fail"
