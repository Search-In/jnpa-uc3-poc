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

# _req <method> <path> <body> <timeout> -> echoes HTTP code
_req(){
  local method="$1" path="$2" body="$3" to="$4"
  local url="$GW$path" hdr=(); local ah; ah="$(auth_header)"
  [ -n "$ah" ] && hdr+=(-H "$ah")
  if [ "$body" = "MULTIPART" ]; then
    curl -s -m "$to" -o /dev/null -w '%{http_code}' -X "$method" "${hdr[@]}" \
         -F "image=@${TMPDIR_SMOKE}/px.jpg;type=image/jpeg" "$url"
  elif [ "${body:0:1}" = "&" ]; then         # form-encoded (leading &)
    curl -s -m "$to" -o /dev/null -w '%{http_code}' -X "$method" "${hdr[@]}" \
         -H 'Content-Type: application/x-www-form-urlencoded' --data "${body:1}" "$url"
  elif [ -n "$body" ]; then                  # json
    curl -s -m "$to" -o /dev/null -w '%{http_code}' -X "$method" "${hdr[@]}" \
         -H 'Content-Type: application/json' -d "$body" "$url"
  else
    curl -s -m "$to" -o /dev/null -w '%{http_code}' -X "$method" "${hdr[@]}" "$url"
  fi
}

# hit <method> <path> [json_body|@form|MULTIPART] [timeout] — classifies as PASS/FAIL
hit(){
  local method="$1" path="$2" body="${3:-}" to="${4:-$TIMEOUT}"
  classify "$(_req "$method" "$path" "$body" "$to")" "$method" "$path"
}

# hit_dep — for endpoints whose success depends on a LIVE external sim/seed that
# the gateway does not own (truck-sim OSRM reroute, vahan-sim DL seed). A 200 is a
# real PASS; a 404/502/504/000 is the external dependency being unready, NOT a
# gateway code fault, so it is reported as a soft note (does NOT count as a hard
# failure). Any other code still classifies normally (a genuine gateway bug).
hit_dep(){
  local method="$1" path="$2" body="${3:-}" to="${4:-$TIMEOUT}"
  local code; code="$(_req "$method" "$path" "$body" "$to")"
  case "$code" in
    200|201|204) classify "$code" "$method" "$path";;
    404|502|503|504|000) WARN=$((WARN+1)); printf "${YEL}DEP UNREADY   ${RST} %-6s %s  (HTTP %s — external sim/seed, not a gateway fault)\n" "$method" "$path" "$code";;
    *) classify "$code" "$method" "$path";;
  esac
}

TMPDIR_SMOKE="$(mktemp -d)"; trap 'rm -rf "$TMPDIR_SMOKE"' EXIT
# minimal valid JPEG (base64) for multipart image posts
printf '/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAAAP/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AfwD/2Q==' | base64 -d > "$TMPDIR_SMOKE/px.jpg" 2>/dev/null

# =============================================================================
section "0. STACK & RUNTIME"
# The gateway is the only host-published API on EC2; internal services (anpr,
# identity, etc.) usually have NO host port mapping. So validate the gateway over
# HTTP, but validate internal services via docker container health — NOT by
# probing localhost:<service-port>, which would false-fail behind a private
# docker network. Falls back to an HTTP host probe only when docker is absent.
HAVE_DOCKER=0; command -v docker >/dev/null 2>&1 && docker ps >/dev/null 2>&1 && HAVE_DOCKER=1

# gateway — public HTTP
gcode=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$GW/healthz")
classify "$gcode" "GET" "gateway ($GW/healthz)" || true

# internal services — container name + host-port fallback
#   name|container|hostport
for probe in \
  "anpr|jnpa-anpr|8301" \
  "congestion|jnpa-congestion|8311" \
  "identity|jnpa-identity|8360" \
  "empty-container|jnpa-empty-container|8330" \
  "carbon|jnpa-carbon|8340" \
  "gate-data|jnpa-gate-data|8350" \
  "parking|jnpa-parking|8370" \
  "scenarios|jnpa-scenarios|8400" \
  "truck-sim|jnpa-truck-sim|8240" \
  "postgres|jnpa-postgres|5433" \
  "redis|jnpa-redis|6379" \
  "kafka|jnpa-kafka|9092" \
  "minio|jnpa-minio|9000" ; do
  name="${probe%%|*}"; rest="${probe#*|}"; cname="${rest%%|*}"; port="${rest##*|}"
  if [ "$HAVE_DOCKER" = 1 ]; then
    # State: running/exited/absent; Health: healthy/unhealthy/none
    state=$(docker inspect -f '{{.State.Status}}' "$cname" 2>/dev/null || echo absent)
    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cname" 2>/dev/null || echo none)
    if [ "$state" = "absent" ]; then
      classify "404" "CHK" "$name (container $cname absent)" || true
    elif [ "$state" != "running" ]; then
      classify "502" "CHK" "$name (container $state)" || true
    elif [ "$health" = "unhealthy" ]; then
      classify "503" "CHK" "$name (running/unhealthy)" || true
    else
      # running + healthy|starting|none -> treat as up (no host port needed)
      classify "200" "CHK" "$name (running/$health)" || true
    fi
  else
    code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://localhost:$port/healthz")
    classify "$code" "GET" "$name (localhost:$port)" || true
  fi
done
if [ "$HAVE_DOCKER" = 1 ]; then
  echo; echo "${CYN}docker containers:${RST}"
  docker ps --format 'table {{.Names}}\t{{.Status}}' 2>/dev/null | grep -i jnpa || echo "  (none)"
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
# Discover live IDs from the running system so data-dependent lookups reflect
# TRUE endpoint health (a 404 from a valid ID = real data gap; a 404 from a
# guessed ID that doesn't exist is a test artefact, not a route/code failure).
jqget(){ python3 -c '
import sys, json
try:
    cur = json.load(sys.stdin)
except Exception:
    print(""); sys.exit()
for k in sys.argv[1:]:
    try:
        if isinstance(cur, list):
            cur = cur[int(k)]
        elif isinstance(cur, dict):
            cur = cur[k]
        else:
            cur = None; break
    except (KeyError, IndexError, ValueError, TypeError):
        cur = None; break
print(cur if isinstance(cur, (str, int)) else "")
' "$@"; }
LIVE_DEVICE=$(curl -s -m6 "$GW/api/trucks?limit=1" | jqget devices 0 device_id); LIVE_DEVICE="${LIVE_DEVICE:-$DEVICE}"
ENROLLED_DRIVER=$(curl -s -m6 "$GW/api/identity/enrollments" | jqget enrollments 0 driver_id); ENROLLED_DRIVER="${ENROLLED_DRIVER:-$DRIVER}"
SCEN_HANDLE=$(curl -s -m6 "$GW/api/scenarios/handles?limit=1" | jqget handles 0 handle_id)
echo "discovered: device=$LIVE_DEVICE driver=$ENROLLED_DRIVER handle=$SCEN_HANDLE"
hit GET "/api/vahan/rc/$PLATE"
hit GET "/api/vahan/rc/$PLATE2"                 # expect PROVISIONAL rung
hit_dep GET "/api/vahan/dl/$DL"                 # data-dependent: 404 if DL not in sim seed
hit GET "/api/vahan/fastag/$PLATE"
hit GET "/api/vahan/vehicle-intel/$PLATE"
hit GET "/api/vahan/driver-intel/$DRIVER"
hit GET "/api/anpr/read/CAM-COR-01"
hit GET "/api/trucks/$LIVE_DEVICE"
hit GET "/api/trucks/$LIVE_DEVICE/route/latest"
hit GET "/api/ulip/proxy/$LIVE_DEVICE"
hit GET "/api/kpi/throughput"                   # valid whitelisted view key
hit GET "/api/gate-data/records/$CONTAINER"
hit GET "/api/identity/enrollments/$ENROLLED_DRIVER"
hit GET "/api/identity/enrol-request/$ENROLLED_DRIVER"
hit GET "/api/journey/container/$CONTAINER"
[ -n "$SCEN_HANDLE" ] && hit GET "/api/scenarios/handle/$SCEN_HANDLE/timeline" \
                      || echo "  (skip scenarios timeline — no handle seeded)"

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
# --- Trucks reroute (truck-sim recomputes an OSRM route via external project-osrm;
#     can be slow/unavailable -> dependency-gated, not a gateway fault)
hit_dep POST "/api/trucks/$LIVE_DEVICE/route" '{"gate_id":"'"$GATE"'","reason":"smoke test"}' "$EVAL_TIMEOUT"
hit POST "/api/trucks/$LIVE_DEVICE/route/ack" '{"state":"ACK"}'
# --- Push
hit POST /api/push/subscribe     '{"device_id":"'"$LIVE_DEVICE"'","subscription":{"endpoint":"https://example.com/x","keys":{"p256dh":"x","auth":"y"}}}'
# --- Control (presenter fault inject) then clear
hit POST /api/control/fault/vahan '{"rung":"PROVISIONAL"}'
hit DELETE /api/control/fault
# --- Meta (404 if meta router unmounted)
hit GET /api/assumptions
hit GET /api/oss-inventory

# =============================================================================
section "SUMMARY"
printf "  ${GRN}PASS: %d${RST}   ${YEL}WARN: %d${RST}   ${RED}FAIL(hard): %d${RST}\n" "$PASS" "$WARN" "$FAIL"
echo "  WARN = auth(401/403), payload(400/422), or DEP UNREADY (external sim/seed"
echo "         not owned by the gateway) — none are gateway code faults."
echo "  FAIL(hard) = 404/500/502/timeout on a gateway-owned route = real defect."
hr
exit "$FAIL"
