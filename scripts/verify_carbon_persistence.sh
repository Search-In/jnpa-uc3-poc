#!/usr/bin/env bash
# Verify Carbon-Emission persistence end-to-end (UC-3 R6).
#
#   Login -> JWT -> POST /api/carbon/calculate -> SELECT jnpa.carbon_emission
#         -> GET /api/carbon/history/{vehicle}
#
# Run AFTER the stack is up and the gateway has been rebuilt with the fix:
#   docker compose build gateway && docker compose up -d gateway
#   ./scripts/verify_carbon_persistence.sh
#
# Requires: curl, python3 (for JSON parsing), and either `docker compose` (to run
# psql in the postgres container) or a host psql on localhost:5433.
set -euo pipefail

GW="${GATEWAY_URL:-http://localhost:8000}"
VEH="${VEHICLE_ID:-TRK-000001}"
PW="${POSTGRES_PASSWORD:-jnpa_pw}"

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
jq_get() { python3 -c "import sys,json;print(json.load(sys.stdin).get('$1',''))"; }

say "1) Login (admin/admin) -> access_token"
TOKEN="$(curl -fsS -X POST "$GW/api/auth/login" \
  -H 'content-type: application/json' \
  -d '{"username":"admin","password":"admin"}' | jq_get access_token)"
if [ -z "$TOKEN" ]; then echo "FAIL: no access_token returned"; exit 1; fi
echo "token: ${TOKEN:0:24}…"
AUTH=(-H "Authorization: Bearer $TOKEN")

say "2) GET /api/notifications/health (Bearer)"
curl -fsS "${AUTH[@]}" "$GW/api/notifications/health" | python3 -m json.tool

say "3) POST /api/carbon/calculate (Bearer)"
RESP="$(curl -fsS "${AUTH[@]}" -X POST "$GW/api/carbon/calculate" \
  -H 'content-type: application/json' \
  -d "{\"vehicle_id\":\"$VEH\",\"distance_km\":25,\"idle_time_minutes\":20,\"vehicle_type\":\"truck\"}")"
echo "$RESP" | python3 -m json.tool
EID="$(echo "$RESP"   | jq_get emission_id)"
PERS="$(echo "$RESP"  | jq_get persisted)"
CO2="$(echo "$RESP"   | jq_get co2_kg)"
echo "emission_id=$EID persisted=$PERS co2_kg=$CO2"
if [ "$PERS" != "True" ] && [ "$PERS" != "true" ]; then
  echo "FAIL: persisted is not true — the row did not commit"; exit 1; fi
if [ -z "$EID" ] || [ "$EID" = "None" ]; then
  echo "FAIL: emission_id is null"; exit 1; fi

say "4) SQL check — SELECT FROM jnpa.carbon_emission"
SQL="SELECT id, vehicle_id, distance_km, co2_kg, source, created_at
     FROM jnpa.carbon_emission ORDER BY created_at DESC LIMIT 5;"
if docker compose ps postgres >/dev/null 2>&1; then
  docker compose exec -T postgres psql -U postgres -d postgres -c "$SQL"
  ROWS="$(docker compose exec -T postgres psql -U postgres -d postgres -tAc \
    "SELECT count(*) FROM jnpa.carbon_emission WHERE vehicle_id='$VEH';")"
else
  PGPASSWORD="$PW" psql -h localhost -p 5433 -U postgres -d postgres -c "$SQL"
  ROWS="$(PGPASSWORD="$PW" psql -h localhost -p 5433 -U postgres -d postgres -tAc \
    "SELECT count(*) FROM jnpa.carbon_emission WHERE vehicle_id='$VEH';")"
fi
echo "rows for $VEH: $ROWS"
if [ "${ROWS//[[:space:]]/}" -lt 1 ]; then echo "FAIL: 0 rows persisted"; exit 1; fi

say "5) GET /api/carbon/history/$VEH (Bearer)"
curl -fsS "${AUTH[@]}" "$GW/api/carbon/history/$VEH" | python3 -m json.tool

printf '\n\033[1;32mPASS — Carbon Emission R6 persists end-to-end.\033[0m\n'
