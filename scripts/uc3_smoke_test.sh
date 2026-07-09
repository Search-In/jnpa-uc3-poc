#!/usr/bin/env bash
# =============================================================================
# JNPA UC-III Platform — EC2 API Smoke Test
# =============================================================================
# Exercises every gateway endpoint against a running stack and classifies the
# result. Discovers routes dynamically from the live OpenAPI spec, so it always
# matches what the gateway ACTUALLY mounted (not just what the source defines —
# this is deliberate: it will reveal routers that failed to import at startup).
#
# Usage:
#   ./uc3_smoke_test.sh                       # defaults to http://localhost:8000
#   GW=http://10.0.1.5:8000 ./uc3_smoke_test.sh
#   GW=https://api.example.com ./uc3_smoke_test.sh
#
# Auth: if AUTH_ENABLED=true on the gateway, set ROLE (default DTCCC_ADMIN);
# the script mints a token via /api/auth/dev-token, else falls back to
# /api/auth/login with LOGIN_USER/LOGIN_PASS. If auth is off, no token needed.
#
# Status classification:
#   200/201/204 = PASS         401/403 = AUTH ISSUE
#   404         = ROUTE/DATA   422/400 = PAYLOAD ISSUE
#   500         = BACKEND FAIL 502/503/504 = SERVICE FAILURE   000 = TIMEOUT/DOWN
# Exit code = number of hard failures (500/502/503/000 + unexpected 404).
# =============================================================================
set -uo pipefail

GW="${GW:-http://localhost:8000}"
ROLE="${ROLE:-DTCCC_ADMIN}"
LOGIN_USER="${LOGIN_USER:-admin}"
LOGIN_PASS="${LOGIN_PASS:-admin}"
TIMEOUT="${TIMEOUT:-15}"
EVAL_TIMEOUT="${EVAL_TIMEOUT:-70}"   # /api/anpr/eval + /reports?format=pdf are slow
TOKEN=""
PASS=0; FAIL=0; WARN=0
RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; CYN=$'\e[36m'; RST=$'\e[0m'

# ---- Demo IDs (override via env if your seed differs) -----------------------
PLATE="${PLATE:-MH04AB1234}"          # seeded in jnpa.vehicle_master
PLATE2="${PLATE2:-MH12AB1234}"        # unseeded -> exercises PROVISIONAL rung
DL="${DL:-MH0120200001234}"
DRIVER="${DRIVER:-DRV-001}"
DEVICE="${DEVICE:-TRK-000001}"
CONTAINER="${CONTAINER:-MSCU1234566}" # ISO-6346 valid (journey)
GATE="${GATE:-G-JNPCT}"
FACILITY="${FACILITY:-PF-01}"
RC="${RC:-$PLATE}"

hr(){ printf '%s\n' "------------------------------------------------------------------------"; }
section(){ hr; printf "${CYN}### %s${RST}\n" "$1"; hr; }

# classify <code> <method> <path>  [expected_alt]
classify(){
  local code="$1" method="$2" path="$3"
  local tag color; local hard=0
  case "$code" in
    200|201|204) tag="PASS         "; color=$GRN; PASS=$((PASS+1));;
    401|403)     tag="AUTH ISSUE   "; color=$YEL; WARN=$((WARN+1));;
    422|400)     tag="PAYLOAD ISSUE"; color=$YEL; WARN=$((WARN+1));;
    404)         tag="ROUTE/DATA   "; color=$RED; FAIL=$((FAIL+1)); hard=1;;
    500)         tag="BACKEND FAIL "; color=$RED; FAIL=$((FAIL+1)); hard=1;;
    502|503|504) tag="SERVICE FAIL "; color=$RED; FAIL=$((FAIL+1)); hard=1;;
    000)         tag="TIMEOUT/DOWN "; color=$RED; FAIL=$((FAIL+1)); hard=1;;
    *)           tag="HTTP $code    "; color=$YEL; WARN=$((WARN+1));;
  esac
  printf "${color}%s${RST} %-6s %s\n" "$tag" "$method" "$path"
  return $hard
}

auth_header(){ [ -n "$TOKEN" ] && printf 'Authorization: Bearer %s' "$TOKEN"; }

# hit <method> <path> [json_body|@form|MULTIPART] [timeout]
hit(){
  local method="$1" path="$2" body="${3:-}" to="${4:-$TIMEOUT}"
  local url="$GW$path" code hdr=(); local ah; ah="$(auth_header)"
  [ -n "$ah" ] && hdr+=(-H "$ah")
  if [ "$body" = "MULTIPART" ]; then
    # 1x1 px JPEG for image endpoints (violations/detect, anpr/infer)
    local img="$TMPDIR_SMOKE/px.jpg"
    code=$(curl -s -m "$to" -o /dev/null -w '%{http_code}' -X "$method" "${hdr[@]}" \
           -F "image=@${img};type=image/jpeg" "$url")
  elif [ "${body:0:1}" = "&" ]; then         # form-encoded (leading &)
    code=$(curl -s -m "$to" -o /dev/null -w '%{http_code}' -X "$method" "${hdr[@]}" \
           -H 'Content-Type: application/x-www-form-urlencoded' --data "${body:1}" "$url")
  elif [ -n "$body" ]; then                  # json
    code=$(curl -s -m "$to" -o /dev/null -w '%{http_code}' -X "$method" "${hdr[@]}" \
           -H 'Content-Type: application/json' -d "$body" "$url")
  else
    code=$(curl -s -m "$to" -o /dev/null -w '%{http_code}' -X "$method" "${hdr[@]}" "$url")
  fi
  classify "$code" "$method" "$path"
}

TMPDIR_SMOKE="$(mktemp -d)"; trap 'rm -rf "$TMPDIR_SMOKE"' EXIT
# minimal valid JPEG (base64) for multipart image posts
printf '/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAAAP/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AfwD/2Q==' | base64 -d > "$TMPDIR_SMOKE/px.jpg" 2>/dev/null

# =============================================================================
section "0. STACK & RUNTIME"
for probe in \
  "gateway|$GW/healthz" \
  "anpr|http://localhost:8301/healthz" \
  "congestion|http://localhost:8311/healthz" \
  "identity|http://localhost:8360/healthz" \
  "empty-container|http://localhost:8330/healthz" \
  "carbon|http://localhost:8340/healthz" \
  "gate-data|http://localhost:8350/healthz" \
  "parking|http://localhost:8370/healthz" \
  "scenarios|http://localhost:8400/healthz" \
  "truck-sim|http://localhost:8240/healthz" ; do
  name="${probe%%|*}"; u="${probe#*|}"
  code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$u")
  classify "$code" "GET" "$name ($u)" || true
done
if command -v docker >/dev/null 2>&1; then
  echo; echo "${CYN}docker containers:${RST}"
  docker ps --format 'table {{.Names}}\t{{.Status}}' 2>/dev/null | grep -i jnpa || echo "  (docker not accessible)"
fi

# =============================================================================
section "1. AUTH — mint token"
AENABLED=$(curl -s -m5 "$GW/healthz" | grep -o '"mode":"[^"]*"' || true)
echo "gateway mode: ${AENABLED:-unknown}"
TOKEN=$(curl -s -m5 -X POST "$GW/api/auth/dev-token" -H 'Content-Type: application/json' \
        -d "{\"role\":\"$ROLE\"}" | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')
if [ -z "$TOKEN" ]; then
  TOKEN=$(curl -s -m5 -X POST "$GW/api/auth/login" -H 'Content-Type: application/json' \
          -d "{\"username\":\"$LOGIN_USER\",\"password\":\"$LOGIN_PASS\"}" \
          | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')
fi
if [ -n "$TOKEN" ]; then echo "${GRN}token acquired (len ${#TOKEN})${RST}"; else echo "${YEL}no token (auth likely disabled — proceeding open)${RST}"; fi
hit GET  /api/auth/roles         # public
hit POST /api/auth/dev-token '{"role":"CUSTOMS"}'

# =============================================================================
section "2. AUTO GET SWEEP (from live OpenAPI, no-path-param routes)"
# Pull every GET path without a {param}; skip websockets & static file streamers.
# portable array fill (works on bash 3.2 / macOS; no mapfile dependency)
GETS=()
while IFS= read -r line; do [ -n "$line" ] && GETS+=("$line"); done < <(
  curl -s -m8 "$GW/openapi.json" | python3 -c '
import sys,json
d=json.load(sys.stdin)
for p,ops in d.get("paths",{}).items():
    if "{" in p: continue
    for m in ops:
        if m.lower()=="get":
            print(p)
' | sort -u)
echo "discovered ${#GETS[@]} parameterless GET routes"
for p in "${GETS[@]}"; do
  case "$p" in
    /api/anpr/eval|/api/reports/police) hit GET "$p" "" "$EVAL_TIMEOUT";;
    /api/fastag/transactions/history)   hit GET "$p?rc_number=$RC&limit=5";;  # needs param
    *)                                  hit GET "$p";;
  esac
done

# =============================================================================
section "3. GET WITH PATH PARAMS (real demo IDs)"
hit GET "/api/vahan/rc/$PLATE"
hit GET "/api/vahan/rc/$PLATE2"                 # expect PROVISIONAL rung
hit GET "/api/vahan/dl/$DL"
hit GET "/api/vahan/fastag/$PLATE"
hit GET "/api/vahan/vehicle-intel/$PLATE"
hit GET "/api/vahan/driver-intel/$DRIVER"
hit GET "/api/anpr/read/CAM-COR-01"
hit GET "/api/trucks/$DEVICE"
hit GET "/api/trucks/$DEVICE/route/latest"
hit GET "/api/ulip/proxy/$DEVICE"
hit GET "/api/kpi/gate_throughput"
hit GET "/api/gate-data/records/$CONTAINER"
hit GET "/api/identity/enrollments/$DRIVER"
hit GET "/api/identity/enrol-request/$DRIVER"
hit GET "/api/journey/container/$CONTAINER"     # <-- WILL 404 if journey router unmounted
hit GET "/api/scenarios/tfc1/timeline"          # handle-based; may 404 without a run

# =============================================================================
section "4. POST / PUT / DELETE — valid demo payloads"
# --- Auth/OTP
hit POST /api/auth/otp/request '{"mobile":"9876543210","device_id":"'"$DEVICE"'"}'
hit GET  "/api/auth/otp/session/$DEVICE"
# --- Vahan-adjacent enforcement chain
hit POST /api/violations/detect  MULTIPART
hit POST /api/violations/enforce MULTIPART
hit POST /api/anpr/infer         MULTIPART
# --- Identity (synthetic path is safe without ALLOW_REAL_BIOMETRICS)
hit POST /api/identity/verify   '{"driver_id":"'"$DRIVER"'","is_synthetic":true,"purpose":"GATE_VERIFICATION","simulate":"genuine"}'
hit POST /api/identity/enrol    '{"driver_id":"'"$DRIVER"'","is_synthetic":true,"purpose":"ENROLMENT"}'
# --- Parking
hit POST /api/parking/allocate  '{"facility_id":"'"$FACILITY"'","vehicle_id":"'"$PLATE"'","driver_id":"'"$DRIVER"'"}'
hit POST /api/parking/release   '{"vehicle_id":"'"$PLATE"'"}'
hit POST /api/parking/violation '{"vehicle_id":"'"$PLATE"'","facility_id":"'"$FACILITY"'","type":"NO_PARKING"}'
# --- Empty container
hit POST /api/empty/containers/allocate '{"container_type":"20GP","demand_id":"D-102","depot_id":"ECD-1"}'
# --- Carbon
hit POST /api/carbon/estimate   '{"vehicle_class":"HGV","distance_km":42.5,"payload_tonnes":18,"idle_minutes":12}'
# --- Traffic/geo
hit POST /api/geo/evaluate      '{"vehicle_id":"'"$PLATE"'","lat":18.9489,"lon":72.9492}'
hit POST /api/geo/events        '{"vehicle_id":"'"$PLATE"'","zone_id":"NP-1","violation_type":"NO_PARKING"}'
# --- FASTag (needs FASTAG_DEMO_MODE=true or a ULIP URL, else 500 config)
hit POST /api/fastag/balance      '{"rc_number":"'"$RC"'"}'
hit POST /api/fastag/transactions '{"rc_number":"'"$RC"'"}'
hit POST /api/fastag/toll-enroute '{"source_state":"Maharashtra","source_name":"Nhava Sheva","destination_state":"Maharashtra","destination_name":"Pune","vehicle_type":"TRUCK"}'
# --- Scenarios / workflow engine
hit POST /api/scenarios/tfc1/run '{"severity":"high"}'
hit POST /api/routing/best_alt_gate '{"exclude":["G-NSICT"],"eta_min":15}'
hit POST /api/echallan/issue     '{"plate":"'"$PLATE"'","kind":"WRONG_WAY"}'
hit POST /api/tas/reschedule     '{"gate_id":"'"$GATE"'","to_gate":"G-BMCT"}'
hit POST /api/scenario_step      '{"step":"demo","label":"smoke"}'
hit POST /api/workflows/evaluate '{"event":{"vehicle_speed":72,"congestion_p":0.4}}'   # <-- 404 if workflows unmounted
hit POST /api/ai/event           '{"event_type":"ILLEGAL_PARKING","vehicle_id":"'"$PLATE"'","severity":"warning"}'
# --- Trucks reroute (needs truck-sim up -> may 502)
hit POST "/api/trucks/$DEVICE/route" '{"gate_id":"'"$GATE"'","reason":"smoke test"}'
hit POST "/api/trucks/$DEVICE/route/ack" '{"state":"ACK"}'
# --- Push
hit POST /api/push/subscribe     '{"device_id":"'"$DEVICE"'","subscription":{"endpoint":"https://example.com/x","keys":{"p256dh":"x","auth":"y"}}}'
# --- Control (presenter fault inject) then clear
hit POST /api/control/fault/vahan '{"rung":"PROVISIONAL"}'
hit DELETE /api/control/fault
# --- Meta (404 if meta router unmounted)
hit GET /api/assumptions
hit GET /api/oss-inventory

# =============================================================================
section "SUMMARY"
printf "  ${GRN}PASS: %d${RST}   ${YEL}WARN(auth/payload): %d${RST}   ${RED}FAIL(hard): %d${RST}\n" "$PASS" "$WARN" "$FAIL"
echo "  (WARN 400/422 on POSTs usually = wrong demo ID for your seed, not a code bug)"
hr
exit "$FAIL"
