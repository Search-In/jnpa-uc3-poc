# API Gateway + Fallback Orchestrator (UC-III Sub-Criterion 3)

The single **public-facing** FastAPI service (host **8000**) that the dashboard
and the trucking-app PWA talk to. Every other service stays on the internal
`jnpa` network; the gateway is the only door in.

Its job is the **fallback orchestration** the bid spec requires: when an
upstream goes dark, the gateway transparently drops to the next rung and — this
is the demo's whole point — records *which* rung served each request so you can
prove the resilience on stage.

```
cp .env.local.example .env.local && make up
curl -s http://localhost:8000/api/vahan/rc/MH04AB1234 | jq .
curl -s http://localhost:8000/api/debug/decisions | jq '.[0]'
```

## Fallback chains

### 1. Camera / ANPR feed (per camera)
| Rung | When |
|------|------|
| `LIVE` | `ingest/anpr` healthy **and** the newest frame on the Redis Stream is < 2 s old |
| `CACHED` | last 60 s of frames replayed from `frames.{camera_id}` |
| `SYNTHETIC` | deterministic plate generator (text overlaid on a stock frame) |

Per-camera degradation is surfaced on **`/api/kpi/cameras`** and the System-Health
panel.

### 2. Vahan / Sarathi / FastTag
| Rung | When |
|------|------|
| `LIVE_PRIMARY` | `vahan-live` — only if `SUREPASS_API_TOKEN` is set |
| `LIVE_FALLBACK` | `vahan-sim` |
| `CACHED` | last response from Redis (TTL **12 h**) |
| `PROVISIONAL` | admit with `provisional=true` + a **24 h cure window**: write `jnpa.vehicle_master(provisional_until = now()+24h)` and emit `Alert(kind=PROVISIONAL_VEHICLE)` |

### 3. Trucking App
| Rung | Source | Scrutiny |
|------|--------|----------|
| `PRIMARY` | trucking-app GPS (MQTT `trucks/+/telemetry`) | normal |
| `SECONDARY` | ULIP relay GPS via `/api/ulip/proxy` (mock if no key) | **elevated** — `Alert(kind=ELEVATED_SCRUTINY)`, gate boom **+5 s** |
| `TERTIARY` | web check-in form at `/checkin` | **elevated** — as above |

## Endpoints

| Path | Purpose |
|------|---------|
| `GET /api/anpr/read/{camera_id}` | resolve a camera read through LIVE/CACHED/SYNTHETIC |
| `POST /api/anpr/infer` | proxy a multipart image to `ai/anpr` (degrades to synthetic) |
| `GET /api/anpr/cameras` | per-camera degradation level |
| `GET /api/vahan/rc/{plate}` | orchestrated RC lookup (4-rung chain) |
| `GET /api/vahan/dl/{dl}` | Sarathi DL (LIVE_PRIMARY → LIVE_FALLBACK → CACHED) |
| `GET /api/vahan/fastag/{plate}` | FastTag balance |
| `GET /api/traffic/predict?horizon_min=15` | corridor congestion (LIVE/CACHED/SYNTHETIC) |
| `GET /api/traffic/snapshots` | latest per-segment snapshots |
| `GET /api/trucks/{device_id}` | truck position (PRIMARY/SECONDARY/TERTIARY) |
| `GET /api/ulip/proxy/{device_id}` | ULIP relay (mock if no key) |
| `GET /api/alerts` | alerts from `ai/anomaly` (degrades to `jnpa.alerts`) |
| `GET /api/scenarios` | scenario driver (Prompt 9; degrades to `jnpa.scenarios`) |
| `GET /api/kpi` | all materialised KPI views |
| `GET /api/kpi/{view}` | one KPI view (`throughput`, `dwell`, `anpr_hourly`, `corridor_speed`, `alerts_by_kind`, `provisional_open`) |
| `GET /api/kpi/sources` | System-Health: `{source, state, last_ok, latency_p95}` |
| `GET /api/kpi/cameras` | per-camera ANPR degradation |
| `GET /api/debug/decisions` | last **1000** fallback decisions, newest first (demo evidence) |
| `GET /api/ws` | WebSocket fan-out |
| `GET /checkin` | TERTIARY manual check-in form |
| `GET /healthz`, `/metrics` | health + Prometheus |

## Cache layer

Every successful upstream response is written to Redis under the convention

```
jnpa:cache:{api}:{key}        e.g. jnpa:cache:vahan:MH04AB1234
```

with the appropriate TTL (Vahan 12 h, traffic 90 s). Values are wrapped with
their write timestamp so the `CACHED` rung can report how stale the served value
is.

## WebSocket fan-out (`/api/ws`)

```
{"type": "alert",          "payload": Alert}
{"type": "traffic",        "payload": Snapshot}
{"type": "truck_position", "payload": TruckTelemetry}   # sampled 1-in-50
{"type": "decision",       "payload": DecisionPath}      # only when a fallback fires
```

Alerts + traffic come off Kafka (`alerts`, `traffic.snapshots`); truck positions
come off MQTT (`trucks/+/telemetry`). All pumps are best-effort — a missing
broker just means that channel is quiet, the HTTP surface stays up.

## Decision evidence

Every orchestrated call funnels through `GatewayState.record_decision`, which:
1. stamps a `DecisionPath` (api, key, `decision_path`, latency, detail),
2. logs it structured with `decision_path=…`,
3. increments `gateway_decisions_total{api,decision_path}`,
4. pushes it on the 1000-entry ring buffer (`/api/debug/decisions`),
5. updates the per-source health table (`/api/kpi/sources`), and
6. broadcasts a `type=decision` WS frame **when a fallback rung fired**.

## Tests

`tests/test_gateway.py` runs in-process via Starlette's `TestClient` (no stack
needed) and proves the Vahan chain transitions:

* token present → `LIVE_PRIMARY`
* token dropped → `LIVE_FALLBACK`
* sim stopped → `CACHED`
* cache flushed → `PROVISIONAL` (+ `jnpa.vehicle_master` row when Postgres is up)

```
make gateway-verify     # smoke test against a running stack
.venv/bin/python -m pytest tests/test_gateway.py
```
