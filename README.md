# JNPA Digital Twin — Use Case III PoC

**Traffic Monitoring & Vehicular Decongestion** along the NH-348 corridor from
JNPA (Jawaharlal Nehru Port Authority) to Karal Phata.

This repository is the **infrastructure skeleton + shared library + working
"hello-trace"** path. It brings up the full local stack (Postgres/TimescaleDB,
Kafka, Redis, MQTT, MinIO, Prometheus, Grafana) and proves the end-to-end data
path: a fake ANPR event is published to Kafka, persisted in Timescale, cached
in Redis, and an MQTT RFID message is round-tripped — all verified by a single
bootstrap self-test that prints `BOOTSTRAP OK`.

> All timestamps are stored internally in **Etc/UTC**. Convert to
> **Asia/Kolkata** only at the dashboard layer.

---

## One-command bring-up

```bash
cp .env.local.example .env.local && make venv && make up && make bootstrap-check
```

`make up` starts the stack in the background; give it ~25 s to become healthy
before running the self-test. A combined invocation (assuming `make venv` has
been run once to create the host virtualenv):

```bash
make up && sleep 25 && make bootstrap-check
```

A successful run ends with:

```
BOOTSTRAP OK
```

### Prerequisites

- A working Docker engine + the `docker compose` v2 plugin
  (Docker Desktop, **or** Colima: `brew install colima docker docker-compose && colima start`).
- Python 3.11+ on the host (for the host-side self-test and `pytest`).
- Host Python deps for the self-test: `make venv` creates `.venv` and installs
  the pinned `jnpa-shared` package (`pip install -e "shared[dev]"`). The
  Makefile auto-detects `.venv/bin/python` for `make test` / `make
  bootstrap-check`; if you prefer your own environment, run
  `pip install -e "shared[dev]"` there instead.

> **Host port note:** the container Postgres is published on host **5433**
> (not 5432) to avoid clashing with a Postgres you may already run locally.
> Containers on the `jnpa` network still use `postgres:5432`.

---

## Where to put API keys

Copy `.env.local.example` to `.env.local` and fill in the blanks. The example
file documents where to obtain each key:

| Variable                | Source                                            |
| ----------------------- | ------------------------------------------------- |
| `GOOGLE_MAPS_API_KEY`   | Google Cloud Console → Maps Platform              |
| `HERE_API_KEY`          | https://platform.here.com/                        |
| `TOMTOM_API_KEY`        | https://developer.tomtom.com/                     |
| `OPENWEATHER_API_KEY`   | https://openweathermap.org/api                    |
| `SUREPASS_API_TOKEN`    | https://surepass.io/ (Vahan / FASTag / RC)        |
| `ULIP_API_KEY`          | https://www.ulip.dpiit.gov.in/                    |
| `BHUVAN_API_KEY`        | https://bhuvan.nrsc.gov.in/                        |

The skeleton runs fully without any external keys — they are only needed once
the ingest/AI services start calling live providers in later PoC stages.

---

## Services, ports & inspection URLs

| Service              | Image                                   | Host port(s)        | Inspect at                                      |
| -------------------- | --------------------------------------- | ------------------- | ----------------------------------------------- |
| Postgres / Timescale | `timescale/timescaledb-ha:pg15-latest`  | `5433` → 5432       | `make psql`                                     |
| Redis                | `redis:7-alpine`                        | `6379`              | `make redis-cli`                                |
| Zookeeper            | `confluentinc/cp-zookeeper:7.6.0`       | (internal)          | —                                               |
| Kafka                | `confluentinc/cp-kafka:7.6.0`           | `9092` (int), `29092` (host) | via Kafka-UI                           |
| Kafka-UI             | `provectuslabs/kafka-ui:latest`         | `8080`              | http://localhost:8080                           |
| Mosquitto (MQTT)     | `eclipse-mosquitto:2`                   | `1883`, `9001` (ws) | `mosquitto_sub -h localhost -t '#'`             |
| MinIO                | `minio/minio:latest`                    | `9000` (API), `9101` (console) | http://localhost:9101 (minioadmin/minioadmin) |
| Prometheus           | `prom/prometheus:latest`                | `9090`              | http://localhost:9090                           |
| Grafana              | `grafana/grafana:latest`                | `3000`              | http://localhost:3000 (admin/admin)             |
| ANPR ingest          | `jnpa/anpr-ingest:0.1.0` (built)        | `9108` → 9101 (metrics) | http://localhost:9108/metrics               |
| ANPR + OCR inference | `jnpa/anpr-ai:0.1.0` (built)            | `8301`              | http://localhost:8301/healthz                   |
| Vahan simulator      | `jnpa/vahan-sim:0.1.0` (built)          | `8201`              | http://localhost:8201/healthz                   |
| Vahan live (Surepass)| `jnpa/vahan-live:0.1.0` (built)         | `8202`              | http://localhost:8202/healthz                   |
| Trucking-app sim     | `jnpa/trucking-app:0.1.0` (built)       | `8240`              | http://localhost:8240/devices                   |
| Congestion forecaster| `jnpa/congestion-ai:0.1.0` (built)      | `8311`              | http://localhost:8311/healthz                   |
| Anomaly detector     | `jnpa/anomaly-ai:0.1.0` (built)         | `8321`              | http://localhost:8321/health                    |
| API gateway          | `jnpa/gateway:0.1.0` (built)            | `8000`              | http://localhost:8000/healthz                   |
| Control-room + PWA   | `jnpa/web:0.1.0` (built)                | `3000`              | http://localhost:3000 · PWA at http://localhost:3000/pwa |

> **Kafka note:** containers on the `jnpa` network use `kafka:9092` (internal
> listener). Host processes — including the bootstrap self-test — use
> `localhost:29092` (external listener). `bootstrap_check.py` rewrites the
> targets automatically.

> **Port note:** MinIO's console is published on **9101** because Mosquitto's
> websocket listener already uses host port **9001**.

---

## Repository layout

```
jnpa-uc3-poc/
├── docker-compose.yml        # all infra services, single "jnpa" network
├── Makefile                  # up / down / logs / psql / redis-cli / test / bootstrap-check
├── infra/
│   ├── postgres/init.sql     # schema + hypertables + seed (4 gates, 18 cameras)
│   ├── mosquitto/mosquitto.conf
│   ├── prometheus/prometheus.yml
│   └── grafana/provisioning/ # datasource + dashboard providers
├── shared/                   # installable `jnpa-shared` package
│   └── jnpa_shared/          # config, schemas, corridor, kafka_io, db, redis_io, logging
├── scripts/
│   ├── bootstrap_check.py
│   ├── download_anpr_samples.sh   # fetch CC clips or synthesize 30s MP4s
│   ├── download_anpr_weights.sh   # fetch YOLO plate weights (degrades if offline)
│   └── _synth_clip.py
├── data/clips/               # bind-mounted into anpr-ingest (.mp4 clips)
├── ingest/anpr/              # ANPR ingestion service (replay -> YOLOv8n -> Kafka)
│   ├── Dockerfile  pyproject.toml
│   └── src/anpr_ingest/      # config, replay, detect, emit, weather, metrics, main
├── ingest/vahan_sim/         # Vahan/Sarathi/FASTag simulator (FastAPI :8201)
│   └── app.py seed.py config.py metrics.py  Dockerfile pyproject.toml
├── ingest/vahan_live/        # Surepass-backed live adapter (FastAPI :8202)
│   └── app.py mappers.py config.py  Dockerfile pyproject.toml
├── data/fixtures/            # known_plates.json (the 50 plates the demo queries)
├── ingest/trucking_app/      # 20k-device GPS telemetry simulator (FastAPI :8240)
│   ├── app.py                # control plane entrypoint (truck-sim)
│   ├── Dockerfile  pyproject.toml  README.md
│   └── trucking_app/         # config, gates, plates, routing, truck, fleet, sinks, simulator, metrics
├── ai/anpr/                  # ANPR + OCR inference service (FastAPI :8301)
│   ├── Dockerfile  pyproject.toml  README.md
│   ├── src/anpr/             # detect, ocr, postprocess, degradation, plategen,
│   │                         #   pipeline, evaluator, metrics, finetune, storage, app
│   ├── eval/bench.py         # held-out benchmark -> metrics.json + OCR_TARGET_MET
│   └── resources/            # indian_plate_chars.txt, state_codes.txt, sample_plate.jpg
├── ai/congestion/            # GraphSAGE+LSTM congestion forecaster (FastAPI :8311)
│   ├── Dockerfile  pyproject.toml  README.md
│   └── model.py graph.py features.py synthetic.py train.py infer.py sources/
├── ai/anomaly/               # behavioural anomaly detector (FastAPI :8321)
│   ├── Dockerfile  pyproject.toml  README.md
│   ├── engine.py app.py workers.py sink.py evidence.py storage.py route_lookup.py
│   ├── rules/                # wrongway, abandoned, parking, route_deviation
│   ├── autoencoder/          # 1D-conv trajectory AE (model.py, features.py)
│   └── track/bytetrack.py    # ByteTrack (supervision) + YOLOv8 over the frame bus
├── gateway/  web/  mobile-pwa/  scenarios/   # later PoC stages
└── tests/                    # test_bootstrap.py, test_anpr_ingest.py, test_anpr_ai.py, test_vahan_sim.py, test_rfid_ingest.py, test_trucking_app.py, test_congestion.py, test_anomaly.py
```

---

## ANPR ingestion service (`ingest/anpr/`)

Replays MP4 clips from `data/clips/` as virtual RTSP feeds, runs a YOLOv8n
vehicle detector (CPU; weights auto-downloaded on first run), crops
plate-candidate regions, and emits `AnprRead` events to Kafka topic
`anpr.reads`. Each frame is tagged with current weather
(fog / rain / dust / clear) pulled from OpenWeatherMap every 10 min.

- `DRY_RUN=true` (default): emit the raw crop only — no call to the AI ANPR
  service (built in Prompt 3.1).
- Zero clips present → the service stays alive and emits a `no_feed` health
  event every 5 s.
- If `ultralytics`/`torch` can't load, detection degrades to a full-frame
  candidate (`degraded=true`) so the pipeline keeps producing events.
- Prometheus metrics: `frames_processed_total`, `plates_emitted_total`,
  `kafka_errors_total`, `weather_pulls_total` — at http://localhost:9108/metrics.

**Get sample clips** (CC sources, else 30s synthetic fallback):

```bash
scripts/download_anpr_samples.sh
# or provide direct CC URLs:
ANPR_SAMPLE_URLS="https://.../a.mp4 https://.../b.mp4" scripts/download_anpr_samples.sh
```

**Verify the pipeline** (after `make up`):

```bash
make up
docker compose logs -f anpr-ingest | grep -m1 plates_emitted_total
# Confluent image: binary is `kafka-console-consumer` (no .sh), container `jnpa-kafka`:
docker exec -it jnpa-kafka kafka-console-consumer \
  --bootstrap-server localhost:9092 --topic anpr.reads --from-beginning --max-messages 5
```

---

## ANPR + OCR inference service (`ai/anpr/`, port 8301)

Sub-Criterion 2A. The bid commits to **≥ 95 % OCR accuracy** under port
conditions (dust, fog, night). Pipeline: **YOLOv8 plate detector → PaddleOCR
(PP-OCRv4, Indian fine-tune) → post-processor** (Indian plate regex + BH-series
+ state-code whitelist + confusion-fixer `{O→0, I→1, S→5, B→8, Z→2}` applied
only on digit positions). `ingest/anpr` POSTs each plate crop to
`http://anpr:8301/infer` when `DRY_RUN=false`.

```
POST /infer        multipart image     -> {plate, conf, bbox, valid, ...}
POST /infer_batch  JSON {images:[b64]} -> {count, results:[...]}
GET  /eval         held-out benchmark  -> metrics + OCR_TARGET_MET
GET  /healthz  GET /metrics
```

The container ships paddle + ultralytics, so the real stack runs and meets the
target. On a bare CPU host without them the service **degrades** to a classical
detector + a deterministic template OCR (so `/infer` and `/eval` still answer);
`/eval` reports an `engine` field (`paddle+yolo` | `fallback`) and a `degraded`
flag so the number is never misread.

```bash
# One-time: fetch YOLO plate weights (degrades gracefully if offline):
scripts/download_anpr_weights.sh
# Verify (after make up):
curl -s -F "image=@./ai/anpr/resources/sample_plate.jpg" http://localhost:8301/infer | jq .
curl -s http://localhost:8301/eval | jq .          # OCR_TARGET_MET=true on the real stack
make anpr-verify                                   # both of the above
make anpr-bench                                    # in-process benchmark -> metrics.json
```

The held-out benchmark (`ai/anpr/eval/bench.py`) scores three slices — clean
(char acc ≥ 97 %, exact ≥ 95 %), dust+haze (exact ≥ 92 %), night low-light
(exact ≥ 90 %) — against the 15 % tail of the shared Vahan plate fixture, and
prints `OCR_TARGET_MET=true|false` (combined weighted accuracy ≥ 95.0 %).
One-time PP-OCRv4 fine-tuning lives in `src/anpr/finetune.py` (~25 min on a T4;
CPU ships a pre-baked adapter / stock PP-OCRv4). See `ai/anpr/README.md`.

---

## Vahan / Sarathi / FASTag (`ingest/vahan_sim/` + `ingest/vahan_live/`)

The bid commits to integrating the Parivahan **Vahan** (RC), **Sarathi** (DL)
and **FASTag** (NETC) APIs; JNPA facilitates production credentials post-award.
For the PoC the same schema is served two ways so the rest of the system is
API-correct either way:

- **`vahan-sim`** (port **8201**) — a deterministic local simulator. Generates
  25,000 regex-valid Indian plates (MH-04/MH-43/MH-06/GJ-01/KA-01/TN-22/KL-07
  + a BH-series slice) with realistic anomaly rates (8% expired-fitness, 3%
  blacklisted, 5% FASTag-LOW, 1% FASTag-BLACKLISTED) and ~`100ms±50ms`
  artificial latency mimicking Parivahan.
- **`vahan-live`** (port **8202**) — proxies the same surface to Surepass.
  Returns `503 {"error":"live_disabled"}` unless `SUREPASS_API_TOKEN` is set;
  it never falls back to the simulator (that is the orchestrator's job).

Both expose `GET /vahan/rc/{plate}`, `GET /sarathi/dl/{dl_number}`,
`GET /fastag/balance/{plate}`, `POST /admin/seed`, `GET /healthz`; schemas live
once in `jnpa_shared.schemas`. Both register themselves in `jnpa.services`
(`name='vahan'`, `kind='sim'|'live'`) on startup for the fallback orchestrator
(Prompt 4), and every successful `/vahan/rc/*` upserts a verified row into
`jnpa.vehicle_master` (`provisional=false`) — the row the dashboard reads.

The 50 plates the demo (Prompt 9) queries are written to
`data/fixtures/known_plates.json` (25 benign, 25 with ≥1 issue). Regenerate
standalone with `make vahan-seed`.

**Verify** (after `make up`):

```bash
curl -s http://localhost:8201/vahan/rc/MH04AB1234 | jq .
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8202/vahan/rc/MH04AB1234   # 503 (no token)
make psql   # then: select count(*) from jnpa.vehicle_master;
make vahan-verify   # one-shot smoke test of all of the above
```

---

## Trucking-app telemetry simulator (`ingest/trucking_app/`)

A 20,000-device (hot-scalable to 30,000+) GPS telemetry simulator for the
trucking-app component (Appendix B5). Each device drives a realistic truck along
NH-348 into one of the 4 JNPA gates and back, with a state machine
(`EN_ROUTE_TO_PORT → AT_GATE_QUEUE → INSIDE_PORT → EN_ROUTE_HOME → IDLE`),
OSRM routing (HERE + dead-reckoning fallbacks), a 55/25/0 km/h speed model with
σ=4 km/h noise and Redis-driven queueing pressure, and GPS jitter (ε~N(0,6 m),
1 % 50 m outliers). Plates are linked to the Vahan simulator's dataset.

Each ping is published to MQTT `trucks/{device_id}/telemetry` (qos 0) **and**
Kafka `truck.telemetry`, and batch-written to `jnpa.truck_telemetry` via asyncpg
COPY every 30 s. Every 30 s an ETA-to-gate goes to `trucks/{device_id}/eta` and
Kafka `truck.eta`. A FastAPI control plane on **8240** owns the fleet:

```bash
curl -s http://localhost:8240/devices | jq '.population'          # population stats
mosquitto_sub -h localhost -t 'trucks/+/telemetry' -C 5           # live pings
curl -s -XPOST http://localhost:8240/devices/scale \
  -H 'content-type: application/json' -d '{"target":30000}' | jq . # hot-scale
curl -s -XPOST http://localhost:8240/devices/TRK-000001/route \
  -H 'content-type: application/json' -d '{"gate_id":"G-BMCT"}' | jq . # reroute (TFC-1)
make truck-verify
```

The compose service `truck-sim` reserves 3 CPUs; the engine sustains
~4,000 msg/s (20k × 5 s) via a single tick scheduler (no per-truck task),
qos=0 position updates, uvloop, and batched COPY. See
[ingest/trucking_app/README.md](ingest/trucking_app/README.md) for details.

---

## Behavioural anomaly detector (`ai/anomaly/`, port 8321)

Sub-Criterion 2C. A hybrid of **ByteTrack** (vehicle tracking), a **rule engine**
(wrong-way, abandoned, illegal-parking, route-deviation), and a **1D-conv
trajectory autoencoder** that catches behaviours the rules can't enumerate
(e.g. slow looping). Every alert is written to `jnpa.alerts` and the Kafka
`alerts` topic, with the offending frame saved to MinIO as
`evidence/{alert_id}.jpg` and its URL attached to `alert.payload`.

It ingests tracks from two sources (both producing the same `Track` type):

- **ByteTrack over the shared frame bus** — `ingest/anpr` mirrors sampled jpeg
  frames to Redis Streams `frames.{camera_id}` (5 fps, trimmed to the last 600);
  the tracker tails those and runs YOLOv8 → `sv.ByteTrack`.
- **Trucking-app telemetry** — tails Kafka `truck.telemetry`, maintains a
  per-device GPS track, and compares it to the assigned route from
  `GET /devices/{id}/route` for the route-deviation rule.

ByteTrack needs `supervision`/`ultralytics`/`torch`; if absent the service runs
rules + AE on the telemetry path and logs the tracker inactive (same graceful
degradation as the other AI services).

```bash
curl -s 'http://localhost:8321/alerts/recent?since=PT1H' | jq 'length'   # bid verify
curl -s -XPOST http://localhost:8321/train_ae -d '{"days":7}' -H 'content-type: application/json' | jq .
make anomaly-verify
```

The six no-parking polygons (`jnpa_shared.corridor.NO_PARK_ZONES`) and the
illegal-parking escalation (WARNING @5 min → CRITICAL @15 min →
REPORT_TO_POLICE @30 min) are documented in
[ai/anomaly/README.md](ai/anomaly/README.md).

### Shared camera frame bus

A lightweight Redis Streams bus (`jnpa_shared.frame_bus`) carries jpeg-encoded
frames on `frames.{camera_id}`, written by `ingest/anpr` at 5 fps (configurable
via `ANPR_PUBLISH_FRAMES` / `ANPR_FRAME_BUS_MAXLEN`) and trimmed to the last 600
entries to bound memory. Both `ai/anomaly` and (later) `ai/anpr` consume from it.

## Trucking-App PWA (`mobile-pwa/`, served at `:3000/pwa`)

Prompt 11. The driver-facing **ETA / re-route advisory** app — the channel that
pushes re-routes during **TFC-1** / **TFC-3**. Vite + React 18 + TS, installable
(`vite-plugin-pwa`). Bundled into the `web` image and served at
`http://localhost:3000/pwa`; an evaluator without a phone opens
`…/pwa?device=DEV-000001` to pair instantly and receive the re-route push live.

Screens: **Trip** (target gate, ETA, speed, traffic-ahead mini-map, "Slot at
Gate" widget from TAS-mock), **Re-route** (full-screen Accept → `state=ACK`),
**Inbox** (advisories/alerts/challans, 24 h IndexedDB cache), **Profile/Vehicle**
(VahanRecord via the gateway).

A re-route (`POST /api/trucks/{id}/route`) reaches the driver on three channels
for the 5 s SLA: a `type=reroute` WebSocket frame (filtered by `device_id` in a
dedicated worker), a **WebPush** notification (`pywebpush`; needs VAPID keys —
`make vapid-keys`), and an in-app polling fallback (`…/route/latest`).

```bash
make vapid-keys     # generate + store the WebPush VAPID keypair (optional)
make dev-pwa        # Vite dev server on :3002 (proxies /api -> gateway)
make pwa-build      # production bundle (base /pwa/)
make pwa-verify     # smoke-test /pwa + the push channel (stack up)
make pwa-e2e        # Playwright: pair, trigger a re-route, banner < 5 s
open http://localhost:3000/pwa     # verification command
```

With no VAPID keys, push is disabled and the PWA uses the WS + polling channels —
the demo never hard-depends on a key. See `mobile-pwa/README.md` for detail.

---

## Make targets

| Target                  | Action                                          |
| ----------------------- | ----------------------------------------------- |
| `make venv`             | create `.venv` + install `jnpa-shared[dev]`     |
| `make up`               | `docker compose up -d`                          |
| `make down`             | `docker compose down -v` (removes volumes)      |
| `make logs`             | tail all service logs                           |
| `make ps`               | container status                                |
| `make psql`             | open `psql` in the Postgres container           |
| `make redis-cli`        | open `redis-cli` in the Redis container         |
| `make install-shared`   | `pip install -e shared`                         |
| `make test`             | `pytest -x shared tests`                        |
| `make bootstrap-check`  | run the end-to-end self-test                    |
| `make vahan-seed`       | regenerate `data/fixtures/known_plates.json`    |
| `make vahan-verify`     | smoke-test the Vahan sim + live adapter         |
| `make rfid-verify`      | verify RFID reads + a vehicle.confirmed fired   |
| `make truck-verify`     | verify the trucking-app sim (population + pings) |
| `make vapid-keys`       | generate the PWA WebPush VAPID keypair          |
| `make dev-pwa`          | run the trucking-app PWA dev server (:3002)     |
| `make pwa-build`        | build the PWA bundle (`mobile-pwa/dist`)        |
| `make pwa-verify`       | smoke-test `/pwa` + the push channel            |
| `make pwa-e2e`          | Playwright: pair → re-route banner < 5 s        |

---

## What the bootstrap self-test verifies

1. `.env.local` is present and loads.
2. Postgres is reachable and `jnpa.gates` has exactly 4 rows.
3. An `AnprRead` JSON message publishes to topic `anpr.reads`.
4. A fresh consumer group reads that message back.
5. The record persists to `jnpa.anpr_reads` and reads back.
6. A key round-trips through Redis with a TTL.
7. An MQTT message to `rfid/readers/R-01` is received by a subscriber.

Exit code `0` + `BOOTSTRAP OK` only if all checks pass; otherwise a non-zero
exit and a per-check PASS/FAIL table.
