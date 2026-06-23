# JNPA Digital Twin — Use Case III (Traffic Monitoring & Vehicular Decongestion)

## Claude Code Prompt Pack — End-to-End PoC

**Project:** GeM/2026/B/7297343 — JNPA AI/ML-Enabled, Cyber-Aware Digital Twin
**Use Case III:** Traffic Monitoring & Vehicular Decongestion (10 marks, 5 × 2-mark sub-criteria)
**Corridor:** JNPA Gates (NSICT / JNPCT / NSIGT / BMCT) → Karal Phata on NH-348 (~35–40 km)
**Target audience:** PoC evaluators during QCBS scoring; ANPR ≥ 95%, GNN+LSTM F1 ≥ 0.85

---

## How to use this pack

1. Open VS Code in an empty folder, e.g. `~/projects/jnpa-uc3-poc/`.
2. Launch Claude Code in that folder.
3. Paste **Prompt 0** first and let it run to completion (~5–10 min). Verify the folder tree and `docker compose ps` shows green.
4. Paste **Prompts 1 → 9** in order. Do not skip ahead — each prompt depends on the previous. After each prompt, run the verification command Claude Code reports at the end of its run.
5. Final demo command is in **Prompt 9**. It records the eval-ready screen capture and exports the metrics CSV used in the technical-bid PoC evidence pack.

### Why each prompt looks heavy

Every prompt is **self-contained**: it states the goal, exact files to create, packages to install, API endpoints, success criteria, and the verification step. Claude Code can execute the whole prompt in one pass without re-asking you for context. That is deliberate — the bid's PoC marking is binary on "did it actually run end-to-end?"

### Real API accounts you must provision **before Prompt 1**

| API | Free tier | What we use it for | Sign-up link |
|---|---|---|---|
| Google Maps Platform | $200/month credit | Real-time traffic on NH-348, Distance Matrix, Roads, Geocoding | https://console.cloud.google.com/google/maps-apis |
| HERE Maps | 1,000 txns/day | Fallback traffic + truck-restricted routing | https://platform.here.com |
| TomTom Traffic | 2,500 txns/day | Second fallback traffic | https://developer.tomtom.com |
| OpenWeatherMap | 1,000 calls/day | Fog/dust/rain for ANPR degradation justification | https://openweathermap.org/api |
| Surepass / Setu (Vahan RC verify) | Trial credits | Optional — only used to show schema compatibility | https://surepass.io or https://setu.co |
| ULIP Sandbox | Free, govt | Schema compatibility for hinterland data | https://www.ulipindia.in (developer portal) |
| Bhuvan API (ISRO) | Free | Indian basemap tiles for offline-capable map | https://bhuvan.nrsc.gov.in/api |

> **Note on Vahan/Sarathi/FastTag:** The bid expressly says JNPA "to facilitate production API access" post-award. For the PoC you build a **schema-faithful simulator** plus an adapter to a commercial proxy (Surepass/Setu) so we can demonstrate both code paths. Evaluator-facing screen shows the simulator; the live-API path is wired but not enabled by default.

Save your keys in a file called `.env.local` at the project root — Prompt 1 will template it.

---

## Architecture at a glance

```
                ┌───────────────────────────────────────────────────────────┐
                │                    React Dashboard (Vite)                 │
                │   Heatmap • Gate-throughput • Alerts • What-If Console    │
                └───────────────┬───────────────────────────────────────────┘
                                │ REST + WebSocket
                ┌───────────────▼───────────────────────────────────────────┐
                │       FastAPI Gateway (with Fallback Orchestrator)        │
                └───┬────────────┬───────────────┬────────────┬─────────────┘
                    │            │               │            │
        ┌───────────▼─┐  ┌───────▼──────┐  ┌─────▼──────┐  ┌──▼───────────┐
        │  ANPR svc   │  │ Congestion   │  │ Anomaly    │  │ ETA / Driver │
        │  YOLOv8 +   │  │ GNN + LSTM   │  │ ByteTrack  │  │ Advisory     │
        │  PaddleOCR  │  │ (PyG)        │  │ + AE       │  │              │
        └─────────────┘  └──────────────┘  └────────────┘  └──────────────┘
                                │            │            │
                ┌───────────────▼────────────▼────────────▼─────────────────┐
                │   Kafka (events) + Redis (cache) + Postgres/Timescale     │
                └───────────────▲───────────────────────────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────────────────────────┐
        │ Ingestors (all run as containers)                                 │
        │  • ANPR camera replay (RTSP from disk)                            │
        │  • Vahan/Sarathi/FastTag simulator (FastAPI w/ govt schema)       │
        │  • RFID MQTT broker + reader emulator                             │
        │  • Trucking-App telemetry (20k device GPS simulator over MQTT)    │
        │  • External traffic adapters: Google / HERE / TomTom              │
        └────────────────────────────────────────────────────────────────────┘
```

Mapped to the sub-criteria:

| Sub-criterion | Built in | Files / Services |
|---|---|---|
| 1. Multi-source data integration | Prompts 1 + 2 | `ingest/anpr`, `ingest/vahan_sim`, `ingest/rfid_mqtt`, `ingest/trucking_app` |
| 2. AI/ML tools usage | Prompt 3 | `ai/anpr`, `ai/congestion`, `ai/anomaly`, `ai/eta` |
| 3. API & fallback | Prompt 4 | `gateway/`, `gateway/fallback.py`, Redis cache |
| 4. Dashboard & KPI monitoring | Prompts 5 + 7 | `web/`, `mobile-pwa/` |
| 5. What-if + reactive workflow | Prompt 6 | `scenarios/tfc1.py`, `tfc2.py`, `tfc3.py` |

---

# PROMPT 0 — Repository, Infrastructure, and Configuration Bootstrap

> Paste the block below into Claude Code. **Do not edit** anything other than the values in `.env.local` afterwards.

```text
You are building an end-to-end Proof-of-Concept for the JNPA Digital Twin
project, Use Case III (Traffic Monitoring & Vehicular Decongestion). This
PoC will be demonstrated live to QCBS evaluators. Do everything below in
one pass and finish with a self-test that prints "BOOTSTRAP OK".

OBJECTIVE OF THIS PROMPT
Create the full repository skeleton, docker-compose infra, shared config,
shared Python package, and a working "hello-trace" path that emits a fake
ANPR event to Kafka, persists it in Postgres/Timescale, caches it in
Redis, and reads it back via a FastAPI gateway. Nothing in the PoC will
work without this skeleton, so it must be correct and complete.

DIRECTORY LAYOUT (create exactly this)

jnpa-uc3-poc/
├── .env.local.example
├── .gitignore
├── README.md
├── docker-compose.yml
├── Makefile
├── infra/
│   ├── postgres/init.sql
│   ├── mosquitto/mosquitto.conf
│   └── grafana/provisioning/
├── shared/
│   ├── pyproject.toml
│   └── jnpa_shared/
│       ├── __init__.py
│       ├── config.py        # pydantic-settings, reads .env.local
│       ├── kafka_io.py      # confluent-kafka producer/consumer helpers
│       ├── db.py            # SQLAlchemy + asyncpg engine
│       ├── redis_io.py
│       ├── schemas.py       # pydantic models for ANPR, Vahan, RFID, Telemetry
│       ├── corridor.py      # geometry of NH-348 corridor + 4 gates
│       └── logging.py       # structlog config
├── ingest/
│   └── _placeholder.md
├── ai/
│   └── _placeholder.md
├── gateway/
│   └── _placeholder.md
├── web/
│   └── _placeholder.md
├── mobile-pwa/
│   └── _placeholder.md
├── scenarios/
│   └── _placeholder.md
└── tests/
    └── test_bootstrap.py

DOCKER COMPOSE (docker-compose.yml)
Define services with explicit images and pinned versions. Use a single
network "jnpa". Expose:
  - postgres (timescale/timescaledb-ha:pg15-latest) on 5432, password from env
  - redis (redis:7-alpine) on 6379
  - zookeeper (confluentinc/cp-zookeeper:7.6.0) and
    kafka (confluentinc/cp-kafka:7.6.0) on 9092, single broker
  - mosquitto (eclipse-mosquitto:2) on 1883 with anonymous local access
  - kafka-ui (provectuslabs/kafka-ui:latest) on 8080 for inspection
  - minio (minio/minio:latest) on 9000 + 9001 with default creds, used
    later for storing model artefacts and ANPR snapshots
  - prometheus, grafana for metrics (latest images)
Add healthchecks on Postgres and Kafka. Mount infra/postgres/init.sql
into /docker-entrypoint-initdb.d/.

POSTGRES INIT (infra/postgres/init.sql)
- CREATE EXTENSION timescaledb
- CREATE SCHEMA jnpa
- Tables (all in jnpa schema, snake_case):
    gates(id text pk, name text, lat double precision, lon double precision)
    cameras(id text pk, gate_id text references gates(id), name text,
            lat double precision, lon double precision, role text
            check(role in ('entry','exit','overview','ptz','thermal','anpr')),
            installed_at timestamptz default now())
    anpr_reads(ts timestamptz, camera_id text, plate text, conf real,
               vehicle_class text, image_url text, weather text,
               degraded boolean default false)
        -> SELECT create_hypertable('jnpa.anpr_reads','ts')
    vehicle_master(plate text pk, rc_type text, owner_hash text,
                   fitness_valid_to date, puc_valid_to date, fastag_status text,
                   provisional boolean default false, provisional_until timestamptz)
    rfid_reads(ts timestamptz, reader_id text, tag_id text, rssi real)
        -> hypertable
    truck_telemetry(ts timestamptz, device_id text, plate text, lat double precision,
                    lon double precision, speed_kmh real, heading real,
                    battery real, accuracy_m real)
        -> hypertable
    traffic_snapshots(ts timestamptz, segment_id text, speed_kmh real,
                      jam_factor real, source text)
        -> hypertable
    alerts(id uuid pk default gen_random_uuid(), ts timestamptz default now(),
           kind text, severity text, gate_id text, plate text, payload jsonb,
           ack boolean default false)
    scenarios(id text pk, name text, started_at timestamptz, ended_at timestamptz,
              params jsonb)
Insert seed rows for 4 gates with realistic JNPA coordinates:
    G-NSICT  18.9489, 72.9492
    G-JNPCT  18.9512, 72.9505
    G-NSIGT  18.9457, 72.9531
    G-BMCT   18.9420, 72.9560
Insert 12 cameras (3 per gate: entry, exit, overview) plus 6 corridor
cameras between the gates and Karal Phata.

SHARED PYTHON PACKAGE (shared/jnpa_shared/)
- config.py: pydantic Settings, reads .env.local, exposes constants for
  Kafka brokers, Postgres DSN, Redis URL, MQTT broker, Google/HERE/TomTom
  keys, JNPA corridor coords.
- corridor.py: hard-code a polyline of ~24 waypoints from JNPA Gate-1
  (18.9489,72.9492) down NH-348 to Karal Phata (18.78,73.08). Provide
  segments[] list each ~1.5–2 km. Helper: nearest_segment(lat, lon).
- schemas.py: pydantic v2 models — AnprRead, VahanRecord, FastagPing,
  RfidRead, TruckTelemetry, TrafficSnapshot, Alert, Scenario.
- kafka_io.py: get_producer() and consume(topic, group, handler) using
  confluent-kafka, JSON values, snappy compression.
- db.py: SQLAlchemy 2.0 async engine factory, plus a tiny CRUD helper.
- redis_io.py: aioredis client with TTL helpers (cache_set, cache_get).
- logging.py: structlog JSON formatter, attaches trace_id from env.
Use Python 3.11. Pin versions. Package name "jnpa-shared". The shared
folder must be installable via `pip install -e .` from any service.

.env.local.example MUST CONTAIN (with comments showing where to get each)
POSTGRES_PASSWORD=jnpa_pw
POSTGRES_DSN=postgresql+asyncpg://postgres:jnpa_pw@postgres:5432/postgres
REDIS_URL=redis://redis:6379/0
KAFKA_BROKERS=kafka:9092
MQTT_BROKER=mosquitto:1883
MINIO_ENDPOINT=minio:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
GOOGLE_MAPS_API_KEY=
HERE_API_KEY=
TOMTOM_API_KEY=
OPENWEATHER_API_KEY=
SUREPASS_API_TOKEN=
ULIP_API_KEY=
BHUVAN_API_KEY=
CORRIDOR_NAME=NH-348 JNPA to Karal Phata
PORT_LAT=18.9489
PORT_LON=72.9492
KARAL_LAT=18.78
KARAL_LON=73.08

MAKEFILE TARGETS
make up         -> docker compose up -d
make down       -> docker compose down -v
make logs       -> docker compose logs -f
make psql       -> opens psql in the postgres container
make redis-cli  -> opens redis-cli
make test       -> runs pytest -x in shared/ and tests/
make bootstrap-check -> runs scripts/bootstrap_check.py

BOOTSTRAP SELF-TEST (scripts/bootstrap_check.py)
This script:
  1. Reads .env.local (fail loudly if missing)
  2. Connects to Postgres and verifies the gates table has 4 rows
  3. Publishes one AnprRead JSON message to topic "anpr.reads"
  4. Consumes it back from the same topic with a fresh consumer group
  5. Writes the same record into jnpa.anpr_reads and reads it back
  6. Caches and reads a key in Redis
  7. Publishes one MQTT message to topic "rfid/readers/R-01" and confirms
     a subscriber receives it
  8. Prints "BOOTSTRAP OK" only if all 7 checks pass; non-zero exit code
     otherwise. Print a clear table of each check pass/fail.

README.md MUST DOCUMENT
- one-command bring-up: `cp .env.local.example .env.local && make up && make bootstrap-check`
- where to put API keys
- ports of each service and their inspection URLs

RULES
- Use `pip install --break-system-packages` only where required inside
  containers. Inside docker images use proper venvs / pyproject.toml.
- Every service Dockerfile must use the python:3.11-slim base.
- Do not introduce any package not pinned in a pyproject.toml or
  requirements.txt.
- The compose file must not depend on the user having a GPU; CPU is fine
  for the PoC. We will swap to GPU edge nodes for production.
- Use Etc/UTC for all timestamps internally; convert to Asia/Kolkata only
  at the dashboard layer.

When done, run `make up && sleep 25 && make bootstrap-check` and confirm
"BOOTSTRAP OK" is printed. If anything fails, fix it and re-run before
finishing the turn.
```

Verification after Prompt 0 finishes:

```bash
make bootstrap-check   # must print BOOTSTRAP OK
docker compose ps      # all services Up (healthy)
```

---

# PROMPT 1 — Sub-Criterion 1A: ANPR/OCR Ingestion Pipeline

> This prompt builds the *ingestion side* (RTSP replay + Kafka topic). The actual model is built in Prompt 3.1.

```text
Build the ANPR ingestion service for the JNPA UC-III PoC.

CONTEXT
- The bid (Corrigendum 3, Appendix C §2.3) requires multi-source data
  integration from ANPR/OCR feeds at port gates and along the
  port-to-NH corridor. Real cameras feed RTSP. For PoC we replay
  pre-recorded MP4 clips simulating dust, fog, and night conditions.
- Target OCR accuracy ≥ 95% (built in Prompt 3.1). This service just has
  to deliver clean per-frame events plus per-second snapshots to Kafka.

DELIVERABLES (folder ingest/anpr/)
- Dockerfile (python:3.11-slim, ffmpeg installed via apt)
- pyproject.toml with: opencv-python-headless==4.10.*, ultralytics==8.3.*,
  confluent-kafka==2.5.*, pydantic==2.*, jnpa-shared (local path),
  numpy, pillow, structlog, prometheus-client, aiortc (optional).
- src/anpr_ingest/main.py: long-running asyncio loop.
- src/anpr_ingest/replay.py: cycles MP4 files at /data/clips/ and exposes
  each as a virtual RTSP-like generator yielding (camera_id, frame, ts).
- src/anpr_ingest/detect.py: vehicle detector (YOLOv8n weights downloaded
  on first run from ultralytics) that crops candidate plates and pushes
  them to the AI ANPR service over HTTP (the AI service is built in
  Prompt 3.1; for now expose a feature flag DRY_RUN=true that emits the
  raw crop only).
- src/anpr_ingest/emit.py: produces JSON to Kafka topic "anpr.reads" with
  the schema AnprRead from jnpa_shared.schemas.
- src/anpr_ingest/weather.py: pulls OpenWeatherMap "Current Weather Data"
  for PORT_LAT/PORT_LON every 10 minutes. Tags each frame as
  weather="fog" if visibility < 1000 m, "rain" if rain>0, "dust" if
  pm10>120 (from optional OpenAQ fallback), else "clear".
- /data/clips/ in compose volume — bind-mount a folder
  ./data/clips/ on host. Put 4 placeholder files (we will add real clips
  later); the service must run even with zero clips by emitting a
  "no_feed" health event every 5 s.

SAMPLE CLIPS (provide a one-time downloader script)
- scripts/download_anpr_samples.sh: pulls Creative-Commons licensed
  Indian highway dashcam footage from these candidate sources (use the
  first that responds 200; do not hard-code one):
    * https://www.pexels.com/search/videos/indian%20traffic/
    * https://www.pond5.com/free
  Save 4 files into ./data/clips/ named cam_g1_entry.mp4,
  cam_g1_exit.mp4, cam_corridor_km5.mp4, cam_corridor_km30.mp4.
  If both sources fail, generate a 30-second synthetic MP4 of a static
  Indian-plate image at random positions and brightness — anything
  non-zero is fine for the pipeline.

OBSERVABILITY
- /metrics endpoint (port 9101) with prometheus_client counters:
  frames_processed_total, plates_emitted_total, kafka_errors_total,
  weather_pulls_total.
- Structured logs only.

COMPOSE
Add this service to docker-compose.yml as "anpr-ingest", with the volume
mount and OPENWEATHER_API_KEY from env.

TESTS (tests/test_anpr_ingest.py)
- A synthetic clip is generated in tmpdir.
- The service is started inproc with DRY_RUN=true.
- Assert at least N>0 messages produced to the Kafka topic within 10s.

VERIFICATION CMD AT END
make up
docker compose logs -f anpr-ingest | grep -m1 plates_emitted_total
docker exec -it kafka kafka-console-consumer.sh --topic anpr.reads --from-beginning --max-messages 5
```

---

# PROMPT 2 — Sub-Criterion 1B: Vahan / Sarathi / FastTag Schema Simulator + Live Adapter

```text
Build a Vahan/Sarathi/FastTag schema-faithful simulator AND a live-API
adapter for the JNPA UC-III PoC.

CONTEXT
- The bid commits to integrating Vahan (RC), Sarathi (DL), and FastTag
  (NETC) APIs. JNPA will facilitate production credentials post-award.
- For the PoC we expose the same schema via a local simulator so the
  rest of the system is API-correct. We also wire a commercial proxy
  (Surepass) as an optional live path, gated by SUREPASS_API_TOKEN.

DELIVERABLES (folder ingest/vahan_sim/)
- FastAPI app on port 8201 exposing:
    GET  /vahan/rc/{plate}        -> VahanRecord (mirrors Parivahan schema)
    GET  /sarathi/dl/{dl_number}  -> SarathiRecord
    GET  /fastag/balance/{plate}  -> FastagPing
    POST /admin/seed              -> reseed deterministic dataset
    GET  /healthz
- Schemas placed in jnpa_shared.schemas (re-use; do not redefine).
  VahanRecord fields: rc_number, owner_name_masked, vehicle_class,
  fuel_type, fitness_valid_to, puc_valid_to, insurance_valid_to,
  registration_date, state, rto_code, blacklist_status.
- ingest/vahan_sim/seed.py generates 25,000 deterministic Indian plates
  across MH-04, MH-43, MH-06, GJ-01, KA-01, TN-22, KL-07 series — must
  pass the Indian plate regex (CCC-04-AA-9999 or new BH-series).
- Realistic distributions: 8% expired-fitness, 3% blacklisted, 5%
  fastag-LOW-BALANCE, 1% fastag-BLACKLISTED.
- Add 100ms±50ms artificial latency to mimic Parivahan's real behaviour.

LIVE ADAPTER (ingest/vahan_live/)
- Same FastAPI surface, but proxies to Surepass:
    https://kyc-api.surepass.io/api/v1/rc/rc-full  (RC)
    https://kyc-api.surepass.io/api/v1/driving-license/driving-license
    https://kyc-api.surepass.io/api/v1/fastag/fastag-search
  Uses SUREPASS_API_TOKEN. If the env var is missing or empty, return
  HTTP 503 with body {"error":"live_disabled"} — DO NOT fall back to the
  simulator at this layer (fallback is the orchestrator's job, Prompt 4).

GATEWAY ROUTING
Both services register themselves in Postgres on startup
(table jnpa.services). The fallback orchestrator (Prompt 4) reads this.

DETERMINISTIC TEST FIXTURE
At seed time, write to ./data/fixtures/known_plates.json the 50 plates
that the demo script (Prompt 9) will query. Half MUST be benign, half
MUST include at least one issue (expired/blacklisted/low-fastag).

VEHICLE-MASTER WRITEBACK
Every successful /vahan/rc/* response is upserted into
jnpa.vehicle_master with provisional=false and a refreshed
provisional_until=null. This is what the dashboard reads when showing
"verified" trucks at the gate.

TESTS
- pytest hits the simulator on 1000 random plates; latency p95 < 400 ms.
- Live adapter returns 503 when token absent.
- vehicle_master row count grows after a batch query.

ADD TO docker-compose.yml: vahan-sim, vahan-live as separate services.

VERIFICATION CMD
curl -s http://localhost:8201/vahan/rc/MH04AB1234 | jq .
psql ... -c "select count(*) from jnpa.vehicle_master;"
```

---

# PROMPT 3 — Sub-Criterion 1C: RFID Reader Emulator + MQTT Ingestion

```text
Build an RFID reader emulator and an MQTT consumer that lands reads in
Timescale for the JNPA UC-III PoC.

CONTEXT
- Appendix A2 references NLDS-installed RFID readers for port-hinterland
  visibility. For the PoC we emulate 25 readers (10 at the 4 gates, 15
  along the 40-km corridor) publishing UHF-like reads over MQTT.

DELIVERABLES (folder ingest/rfid/)
- emulator.py: starts 25 logical readers, each owning a Poisson process
  of vehicle pass-throughs. Reader rate higher at peak hours
  (08:00–11:00 IST, 18:00–21:00 IST). Publishes JSON to
    topic: rfid/readers/{reader_id}
    payload: {"ts": ISO8601, "reader_id":"R-08", "tag_id":"E2801160...",
              "rssi": -42.3}
  Tag IDs are drawn from a fixed pool of 12,000 — must be consistent so
  the same truck shows up at multiple readers as it moves.
- consumer.py: subscribes to rfid/readers/+, validates against RfidRead
  schema, writes to jnpa.rfid_reads, also publishes to Kafka topic
  "rfid.reads" for downstream consumers.
- Both must be resilient to broker restart with auto-reconnect/backoff.
- Reader positions placed along corridor (use jnpa_shared.corridor for
  segment midpoints).

CORRELATION TO ANPR
- A separate background job correlator.py joins rfid.reads with
  anpr.reads inside a 5-second window per gate. Emits a confirmed
  vehicle event to Kafka topic "vehicle.confirmed":
    {"ts","plate","rfid_tag","camera_id","gate_id","confidence":0.97}
  This is what feeds the boom-barrier decision and the gate-throughput
  KPI on the dashboard.

DOCKER COMPOSE
Add services: rfid-emulator, rfid-consumer, rfid-correlator (all share
one image to keep things small).

TESTS
- Start emulator + consumer for 30 s, assert ≥ 50 rows in rfid_reads.
- Inject one synthetic ANPR with matching tag and assert
  vehicle.confirmed message arrives within 6 s.

VERIFICATION CMD
docker compose logs -f rfid-correlator | grep -m1 vehicle.confirmed
psql ... -c "select reader_id, count(*) from jnpa.rfid_reads group by 1 order by 2 desc limit 5;"
```

---

# PROMPT 4 — Sub-Criterion 1D: Trucking-App Telemetry Simulator (20k devices, scalable to 30k)

```text
Build a 20,000-device GPS telemetry simulator for the trucking-app
component of the JNPA UC-III PoC.

CONTEXT
- Appendix B5 specifies 20,000 concurrent installs, scalable to 30,000+.
- Telemetry must look real: realistic speeds, snapping to NH-348/348A,
  pause-at-gates behaviour, occasional GPS jitter.

DELIVERABLES (folder ingest/trucking_app/)
- Python service using asyncio + aiomqtt.
- Loads 20,000 truck profiles at start (deterministic seed). Each truck
  has: plate (linked to a Vahan-sim plate), device_id, current_position,
  destination (round-robin over JNPA 4 gates), state machine
  {EN_ROUTE_TO_PORT, AT_GATE_QUEUE, INSIDE_PORT, EN_ROUTE_HOME, IDLE}.
- Position update interval per truck: 5 s default, 2 s when AT_GATE_QUEUE.
- Position evolves along an OSRM route from a random origin within 100 km
  to a JNPA gate. Use the public OSRM demo
  https://router.project-osrm.org/route/v1/driving/ as primary; HERE
  Routing API as fallback; if both fail, fall back to straight-line
  bearing-based dead reckoning on the corridor polyline.
- Speed model: free-flow 55 km/h on highway, 25 km/h in port roads,
  0 when AT_GATE_QUEUE. Apply Gaussian noise σ=4 km/h. Apply queueing
  pressure from the dashboard's congestion score for that segment
  (read via Redis key traffic:segment:{id}:jam_factor).
- GPS noise: ε ~ N(0, 6 m) on lat/lon; 1% outlier at 50 m.
- Publishes to MQTT topic trucks/{device_id}/telemetry AND to Kafka
  topic "truck.telemetry" for analytics. Writes to
  jnpa.truck_telemetry every 30 s in batched COPY for performance.

CONTROL PLANE
- FastAPI on port 8240:
    GET  /devices?n=20000  current population stats
    POST /devices/scale {target:30000}  hot-scale population
    POST /devices/{device_id}/route  override route (used by Prompt 8
                                     for the TFC-1 gate-closure scenario)
- Health endpoint and Prometheus metrics.

ETA PUBLISHING
- For each truck, every 30 s, compute ETA-to-target-gate using OSRM
  current duration. Publish to MQTT topic
  trucks/{device_id}/eta and Kafka "truck.eta".

PERFORMANCE TARGET
- At 20k devices x 5 s interval = 4,000 msg/s sustained. Service must
  hold steady on a 4-core laptop. Use uvloop, aiomqtt with qos=0 for
  position updates, qos=1 for state changes.

DOCKER COMPOSE
Add service truck-sim with cpus="3.0" reservation.

TESTS
- Start with N=500 devices for CI, assert ≥ 90% of devices publish
  within 10 s.
- Scale via POST /devices/scale and assert population reaches target
  within 30 s.

VERIFICATION CMD
curl -s http://localhost:8240/devices | jq '.population'
mosquitto_sub -h localhost -t 'trucks/+/telemetry' -C 5
```

---

# PROMPT 5 — Sub-Criterion 2A: ANPR + OCR Engine (≥ 95% accuracy)

```text
Build the ANPR + OCR inference service for the JNPA UC-III PoC, with
formal accuracy measurement on a benchmark slice.

CONTEXT
- Bid commits to ≥ 95% OCR accuracy under port operating conditions
  (dust, fog, night).
- Pipeline: YOLOv8 detector for plate ROI -> PaddleOCR (PP-OCRv4)
  fine-tuned on Indian plates -> post-processor (regex + state-code
  whitelist + character-confusion fixer 0/O, 1/I, 8/B etc.).
- This service is consumed by ingest/anpr from Prompt 1.

DELIVERABLES (folder ai/anpr/)
- Dockerfile (python:3.11-slim; install opencv-python-headless,
  paddleocr==2.8.*, paddlepaddle==2.6.*, ultralytics==8.3.*, fastapi).
- FastAPI on port 8301:
    POST /infer       multipart image -> {plate, conf, bbox}
    POST /infer_batch JSON list of base64 images -> list of results
    GET  /eval        runs the held-out test set and returns metrics
- src/anpr/detect.py: YOLOv8 plate detector. We do not retrain from
  scratch; download yolov8n-license-plate weights from
  https://github.com/computervisioneng/automatic-number-plate-recognition-python-yolov8
  (already publicly released by the author). On startup, hash-verify the
  weights file.
- src/anpr/ocr.py: PaddleOCR with custom rec_char_dict_path pointing to
  ./resources/indian_plate_chars.txt (A-Z 0-9 + state codes).
- src/anpr/postprocess.py: applies the Indian plate regex
  ^([A-Z]{2})[ -]?([0-9]{1,2})[ -]?([A-Z]{1,3})[ -]?([0-9]{4})$
  plus the BH-series ^([0-9]{2})BH([0-9]{4})([A-Z]{1,2})$.
  Confusion-fix table {O->0, I->1, S->5, B->8, Z->2} applied only on
  positions that the regex says must be digits.
- src/anpr/finetune.py: ONE-TIME fine-tuning script that pulls a public
  Indian-plate dataset (preference order, use the first that responds):
    1. https://www.kaggle.com/datasets/sarthakvajpayee/indian-vehicle-dataset
    2. https://github.com/sanchit2843/Indian_LPR
    3. https://github.com/Rishit-dagli/Vehicle-License-Plate-Detection
  Train PP-OCRv4 recognizer for 30 epochs, freeze backbone for first 10.
  Save adapter to ./resources/rec_indian/. Document expected GPU time
  (~25 min on T4; CPU fallback: pre-baked adapter is shipped in repo).
- src/anpr/degradation.py: programmatic image augmenter (gaussian blur,
  haze layer, low-light gamma) used by the evaluator.

EVALUATION SUITE (ai/anpr/eval/)
- bench.py runs three tests:
    (a) Clean test set (held-out 15% split), expect ≥ 97% plate
        character accuracy (CER < 3%) and ≥ 95% exact-match.
    (b) Synthetic dust+haze augmented set, expect ≥ 92% exact-match.
    (c) Synthetic night-low-light augmented set, expect ≥ 90%
        exact-match.
- Outputs metrics.json with per-class detection metrics and OCR CER/WER.
- Prints a final line "OCR_TARGET_MET=true|false" — true requires
  combined weighted accuracy ≥ 95.0%.

ALIGNMENT WITH PROMPT 1
- Update ingest/anpr to POST each plate crop to http://anpr:8301/infer
  when DRY_RUN=false. Persist the full enriched event to Kafka.

COMPOSE
Add service "anpr" depending on minio; persist weights to minio bucket
"models".

VERIFICATION CMD
curl -s -F "image=@./resources/sample_plate.jpg" http://localhost:8301/infer | jq .
curl -s http://localhost:8301/eval | jq .   # must show OCR_TARGET_MET=true
```

---

# PROMPT 6 — Sub-Criterion 2B: Traffic Congestion Forecaster (GNN + LSTM, F1 ≥ 0.85)

```text
Build the traffic congestion forecaster for the JNPA UC-III PoC.

CONTEXT
- Bid spec: graph neural network over the port road network
  (gates -> Karal Phata, ~40 km multi-lane) + LSTM on historical loops;
  F1 ≥ 0.85 on congestion onset.

DATA STRATEGY
- Real-time inputs:
    * Google Maps Distance Matrix or Roads API (every 60 s for each of
      ~24 corridor segments)
    * HERE Traffic Flow v7
    * TomTom Traffic Flow
    * RFID + ANPR derived counts (last 5 min per segment)
    * Trucking-App-derived speed median per segment
- Historical training:
    * Bootstrap with 14 days of synthetic but plausible data generated
      by running the system in "fast-forward" mode (5x real-time) before
      the demo. The synthetic generator must respect known commute
      peaks for the JNPA-Karal corridor.
    * Optionally enrich with one-time pull of historical traffic from
      HERE Traffic API "tile" archive for the Karal Phata bbox if the
      key has access.

DELIVERABLES (folder ai/congestion/)
- model.py: PyTorch model = GraphSAGE encoder over the corridor graph
  (nodes = segments, edges = adjacency + lane-count + signalised flag),
  feeding a 2-layer LSTM with 30-step input window of 60-s aggregates,
  output = P(congested in next 15 min) per segment.
- graph.py: builds the corridor PyG graph from jnpa_shared.corridor.
- features.py: builds the rolling feature window per segment.
- train.py: trains on the synthetic+real history; uses binary cross
  entropy with class weights; reports F1, precision, recall, ROC-AUC on
  the held-out last 24 h.
- infer.py: FastAPI on port 8311 with:
    POST /predict {horizon_min:15}  -> {segment_id: prob}
    GET  /metrics                   -> training metrics summary
    POST /backfill {hours:24}       -> rebuilds features and stores
- Persist trained weights to minio bucket "models" under congestion/.
- All segment predictions are continuously published to Kafka topic
  "traffic.predictions" once per minute by a background scheduler.

EXTERNAL TRAFFIC ADAPTERS (folder ai/congestion/sources/)
- google.py, here.py, tomtom.py — each implements get_segment_speed(seg).
- A SourceManager.get(seg) tries google -> here -> tomtom in order, with
  a 1-second timeout each, and caches the answer in Redis for 90 s. If
  ALL three fail, returns the last cached value with a "stale=true"
  marker. This is the foundation for Sub-Criterion 3 fallback.

TARGET METRICS (printed at end of train.py)
- congestion_onset_f1  >= 0.85
- precision            >= 0.80
- recall               >= 0.80
If under target, automatically re-run with class_weight up-adjusted; if
still under, exit non-zero so the bid team can investigate.

COMPOSE
Add service "congestion" depending on postgres, redis, minio.

VERIFICATION CMD
curl -s -XPOST http://localhost:8311/predict -d '{"horizon_min":15}' -H 'content-type: application/json' | jq '. | length'
curl -s http://localhost:8311/metrics | jq '.congestion_onset_f1'
```

---

# PROMPT 7 — Sub-Criterion 2C: Behavioural Anomaly Detector (ByteTrack + Rule + Autoencoder)

```text
Build the behavioural anomaly detector for the JNPA UC-III PoC.

CONTEXT
- Bid spec covers: wrong-way, abandoned, illegal parking, route
  deviation. Hybrid approach = ByteTrack + rule engine + autoencoder
  on trajectory embeddings.

DELIVERABLES (folder ai/anomaly/)
- track/bytetrack.py: ByteTrack via the official supervisory wrapper
  from supervision==0.22.* with YOLOv8 vehicle detector. Inputs come
  from ingest/anpr stream (we add a shared frame bus, see below).
- rules/wrongway.py: per-camera "allowed bearing range"; if a track's
  heading diverges by >120° for >2 s -> WRONG_WAY alert.
- rules/abandoned.py: a stationary track in a non-parking polygon for
  >120 s -> ABANDONED.
- rules/parking.py: stationary track in any of the 6 named no-parking
  polygons (defined in jnpa_shared.corridor.NO_PARK_ZONES) for >300 s
  -> ILLEGAL_PARKING with duration-based escalation
  (WARNING@5min, CRITICAL@15min, REPORT_TO_POLICE@30min).
- rules/route_deviation.py: compares a truck's trucking-app GPS path to
  the assigned route from /devices/{id}/route. Cosine distance > 0.4 or
  off-route > 800 m for > 90 s -> ROUTE_DEVIATION.
- autoencoder/model.py: a small 1D-conv autoencoder on per-track
  trajectory features (speed series, heading series, dwell pattern).
  Reconstruction error above the 99th-percentile training threshold ->
  ANOMALOUS_TRAJECTORY (catches behaviours the rule engine cannot
  enumerate, e.g., slow looping).
- All alerts produced as Alert records written to jnpa.alerts and
  published to Kafka topic "alerts".
- Service exposes FastAPI on port 8321 with:
    GET /alerts/recent?since=...  -> list[Alert]
    POST /train_ae                -> trains AE on last N days of tracks
    GET /health, /metrics

SHARED FRAME BUS
- A new lightweight Redis Streams stream "frames.{camera_id}" carries
  jpeg-encoded frames written by ingest/anpr at 5 fps (configurable).
  Both ai/anomaly and (later) ai/anpr consume from there. Keep stream
  trimmed to the last 600 entries to bound memory.

EVIDENCE PIPELINE
- On every alert, the detector saves the offending frame to MinIO under
  evidence/{alert_id}.jpg and attaches the URL to alerts.payload.
  This is required for the TFC-2 wrong-way scenario in Prompt 8.

TESTS
- Synthetic track files inject one wrong-way, one abandoned, one
  illegal-park scenario. Each must produce exactly one alert of the
  correct kind within tolerance.

VERIFICATION CMD
curl -s http://localhost:8321/alerts/recent?since=PT1H | jq 'length'
```

---

# PROMPT 8 — Sub-Criterion 3: API Gateway, Fallback Orchestrator, Provisional Vehicle Flow

```text
Build the API gateway with the fallback orchestrator and the
provisional-vehicle workflow for the JNPA UC-III PoC.

OBJECTIVE
- Single FastAPI service on port 8000 that the dashboard and the
  trucking-app PWA talk to. It is the only public-facing service.
- It encodes all the fallback behaviour required by Sub-Criterion 3.

DELIVERABLES (folder gateway/)
- main.py: FastAPI app, mounts these routers:
    /api/anpr      -> proxies to ai/anpr
    /api/vahan     -> orchestrated (see below)
    /api/traffic   -> orchestrated
    /api/trucks    -> proxies to ingest/trucking_app
    /api/alerts    -> from ai/anomaly
    /api/scenarios -> Prompt 9
    /api/kpi       -> reads materialised KPI views from Timescale
    /api/ws        -> WebSocket: alerts + traffic snapshots fan-out
- fallback.py: implements the chain decisions below.

FALLBACK CHAINS (must match the bid spec)

1) Camera / ANPR feed:
   LIVE  -> ingest/anpr healthy AND <2 s lag
   CACHED -> last 60 s of frames replayed from Redis Stream
   SYNTHETIC -> synthetic plate generator (text overlaid on stock frame)
   Per-camera degradation level surfaced via /api/kpi/cameras and shown
   on the dashboard.

2) Vahan / Sarathi / FastTag:
   LIVE_PRIMARY  -> vahan-live (if SUREPASS_API_TOKEN set)
   LIVE_FALLBACK -> vahan-sim
   CACHED        -> last response from Redis (TTL 12 h)
   PROVISIONAL   -> vehicle is admitted with provisional=true and a
                    24-hour cure window. Write a row to
                    jnpa.vehicle_master with provisional_until = now()+24h
                    and emit an Alert kind=PROVISIONAL_VEHICLE.
   The decision is logged with a structured field decision_path so the
   demo can show which path was used per request.

3) Trucking App:
   PRIMARY   -> trucking-app GPS via MQTT trucks/+/telemetry
   SECONDARY -> ULIP relay GPS via /api/ulip/proxy (mock if no key)
   TERTIARY  -> web check-in form at /checkin (a tiny HTML page).
   Vehicle is allowed to progress through gate but with elevated
   scrutiny (alert kind=ELEVATED_SCRUTINY raised, gate boom delay+5s).

CACHE LAYER
- Every successful upstream response is written to Redis with
  appropriate TTL. The cache_key convention is
  jnpa:cache:{api}:{key} (e.g. jnpa:cache:vahan:MH04AB1234).

DEGRADATION TELEMETRY
- /api/kpi/sources returns a table of {source, state, last_ok, latency_p95}
  used by the dashboard "System Health" panel.
- Decision_path is also exposed for the LAST 1000 calls via
  /api/debug/decisions (kept in a ring buffer; demo evidence).

WEBSOCKET FAN-OUT
- /api/ws emits:
    type=alert          payload=Alert
    type=traffic        payload=Snapshot
    type=truck_position payload=TruckTelemetry (sampled 1 in 50 for
                         bandwidth)
    type=decision       payload=DecisionPath (only when fallback fires)

TESTS
- Pull the Surepass token; assert /api/vahan/rc/{plate} uses
  LIVE_PRIMARY.
- Drop the token via env override; assert LIVE_FALLBACK.
- Stop vahan-sim; assert CACHED.
- Flush Redis; assert PROVISIONAL with vehicle_master row written.

VERIFICATION CMD
curl -s http://localhost:8000/api/vahan/rc/MH04AB1234 | jq .
curl -s http://localhost:8000/api/debug/decisions | jq '.[0]'
```

---

# PROMPT 9 — Sub-Criterion 4: Dashboard, Heatmap, Geo-fence Alerts, Police Reports

```text
Build the JNPA UC-III dashboard: a React + Vite + TypeScript SPA that
talks to the gateway from Prompt 8.

OBJECTIVE
- Visual representation of every sub-criterion. The evaluator will see
  this screen during scoring; it must look polished and respond <1 s
  to interactions.

DELIVERABLES (folder web/)
- Vite + React 18 + TypeScript + Tailwind + shadcn/ui.
- Maps: maplibre-gl-js with two basemap providers:
    Primary  -> Mapbox style (env var MAPBOX_TOKEN, free tier OK)
    Fallback -> Bhuvan (ISRO) WMS tiles
- Charts: recharts.
- Data fetching: TanStack Query against /api on the gateway.
- WebSocket subscription against /api/ws for live alerts + positions.

SCREENS
1. Live Operations
   - Full-screen MapLibre showing:
       * 4 gates as markers (colour by current throughput vs target)
       * The 40 km corridor polyline coloured by jam_factor
         (green < 0.3, amber < 0.6, red >= 0.6)
       * Live truck dots (sampled 1:50) with trails fading over 5 min
       * A traffic heatmap overlay built from segment jam factors
   - Side panel: top 10 active alerts with severity colour, click ->
     pans the map and opens the evidence image from MinIO.
   - Top KPI row: average dwell at each gate; gate-wise throughput
     (last 60 min); queue length live (computed from truck-state ==
     AT_GATE_QUEUE counts).

2. Driver Advisory
   - List of trucks AT_GATE_QUEUE with computed ETA-to-gate and
     re-routing recommendation. A button "Push Re-route" calls
     /api/trucks/{id}/route to force a new path. (Used in TFC-3
     scenario.)

3. Geo-fencing Manager
   - Map editor (using terra-draw) to add/edit no-parking polygons
     and restricted zones. PUT /api/zones writes back to Postgres
     and the anomaly service picks them up live.
   - Escalation timeline UI (5 / 15 / 30 min thresholds) is editable.

4. Traffic-Police Reports
   - Tabular view of alerts with kind in {WRONG_WAY, ILLEGAL_PARKING,
     OVERSPEEDING, ROUTE_DEVIATION}. Filters by date, gate, severity.
   - "Export PDF" button calls /api/reports/police?format=pdf which
     compiles a one-page PDF per incident with the photographic
     evidence, plate, RC info, and recommended action (e-Challan
     payload pre-filled). Use a server-side renderer (Playwright)
     in the gateway.

5. System Health
   - Live status of every source: ANPR (per camera), Vahan, Sarathi,
     FastTag, Google/HERE/TomTom, RFID, Trucking App.
   - For each source: current decision-path state, last_ok, latency
     p50/p95. Coloured chips. Click -> log drawer.

6. What-If Console (Prompt 10 will populate this; just scaffold here)

ROUTING & SHELL
- Sidebar nav with the 6 screens.
- Header shows current scenario (none / TFC-1 / TFC-2 / TFC-3) and a
  "Reset to baseline" button.

ACCESSIBILITY
- Colour-blind safe palette for severity. WCAG AA contrast everywhere.

BUILD & SERVE
- Production build served by nginx in a small docker image on port 3000.
- Dev mode reachable on port 5173 via `make dev-web`.

TESTS
- Playwright e2e: load /live, expect map canvas, expect at least one
  alert chip to appear within 30 s on a freshly booted stack.

VERIFICATION CMD
open http://localhost:3000/live
```

---

# PROMPT 10 — Sub-Criterion 5: What-If Scenarios TFC-1, TFC-2, TFC-3 + Reactive Workflow

```text
Build the three what-if scenarios for the JNPA UC-III PoC and the
reactive workflow plumbing that ties them across the system.

OBJECTIVE
The bid specifies three named scenarios. Each must be:
- Triggerable from the dashboard's What-If Console
- Visible end-to-end: a sequence of automated downstream actions
- Reversible via "Reset to baseline"
- Logged as a Scenario row with timestamps so we can replay it

DELIVERABLES (folder scenarios/)
- Each scenario is a Python module exposing
    async def run(params: dict) -> ScenarioHandle
    async def reset(handle: ScenarioHandle) -> None
  and registered via entry-points in scenarios/__init__.py.
- One scheduler service "scenarios-runner" on port 8400 with
    POST /scenarios/{name}/run   -> {handle_id}
    POST /scenarios/{name}/reset -> ok
    GET  /scenarios/{handle_id}/timeline -> event-by-event log
  Every event the scenario causes is also pushed to /api/ws (type=
  scenario_step) so the dashboard can paint a step-by-step storyline
  beneath the map.

SCENARIO TFC-1 — Gate Closure
- Params: {gate_id: "G-NSICT", duration_minutes: 120}
- Actions:
    1. Mark gate closed in jnpa.gates (closed_at = now()).
    2. Inject a synthetic high-volume of AT_GATE_QUEUE state changes
       at G-NSICT for the first 20 minutes.
    3. The congestion forecaster (Prompt 6) detects the build-up at
       G-NSICT and predicts spillover to G-JNPCT and G-NSIGT within
       15 min — assert the prediction reaches P>=0.7 at both.
    4. The trucking-app simulator (Prompt 4) auto-re-routes trucks
       whose state=EN_ROUTE_TO_PORT and target=G-NSICT; new target
       is chosen by /api/routing/best_alt_gate which picks the gate
       with lowest predicted queue at ETA.
    5. The TAS sub-system mock (a stub in gateway/tas_mock.py) marks
       the corresponding slots as RESCHEDULED.
    6. Dashboard timeline shows steps 1-5 with timestamps and links to
       the affected trucks.

SCENARIO TFC-2 — Wrong-Way Detection at NH Junction
- Params: {camera_id: "C-KARAL-EXIT"}
- Actions:
    1. Inject a synthetic wrong-way track into the frame bus
       (Prompt 7's anomaly service picks it up).
    2. Anomaly service emits WRONG_WAY alert with evidence URL.
    3. Gateway calls e-Challan workflow stub /api/echallan/issue
       with the plate (resolved via Vahan adapter chain — must show
       fallback if vahan-live unavailable). The stub returns a fake
       e-Challan ID and PDF.
    4. Alert payload now includes echallan_id and echallan_pdf_url.
    5. Dashboard plays the evidence MP4 (last 10 s from the frame
       bus) inside the alert detail drawer.

SCENARIO TFC-3 — Cargo Surge Cross-Twin (Use Case II <-> III)
- Params: {dpd_release_spike: 2.5}  // 2.5x baseline
- Actions:
    1. Publish a synthetic spike event to Kafka topic
       "cargo.dpd_release" (this is the cross-twin link; UC-II would
       normally produce this).
    2. A scenarios/uc2_bridge.py listener translates it into expected
       upstream truck demand: bursts of 600 trucks/h released over
       40 min. The trucking-app simulator instantiates these trucks
       on the corridor.
    3. The congestion forecaster predicts build-up on NH-348 segments
       8-14 within 30 min — assert at least 5 segments cross P>=0.6.
    4. The driver-advisory engine reissues gate-slot windows via
       /api/trucks/{id}/route. Affected trucks receive PWA push
       notification (Prompt 11).
    5. Dashboard timeline shows the cross-twin link explicitly with
       a labelled arrow from "UC-II DPD release" to "UC-III demand".

REACTIVE WORKFLOW GUARANTEES
- Every step must be idempotent and recorded with its trigger source
  in jnpa.scenarios.params.steps[].
- "Reset to baseline" must restore:
    * gate state
    * artificially injected trucks removed
    * synthetic alerts marked resolved
    * Redis caches re-warmed by forcing a fresh poll cycle

OBSERVABILITY
- Each scenario also emits OpenTelemetry traces spanning the chain
  ingest -> AI -> alert -> action so an evaluator can open Jaeger
  (add jaeger:1.59 to compose) and see the full causal chain.

TESTS
- Each scenario is wrapped in a pytest that runs it end-to-end,
  asserts all 5 dashboard steps fire, and resets cleanly.

VERIFICATION CMD
curl -s -XPOST http://localhost:8400/scenarios/tfc1/run -d '{"gate_id":"G-NSICT","duration_minutes":120}' -H 'content-type: application/json'
# then open http://localhost:3000/whatif and watch the timeline
```

---

# PROMPT 11 — Trucking-App PWA (driver-side ETA / advisory, mobile + web)

```text
Build the Trucking-App PWA for the JNPA UC-III PoC.

CONTEXT
- Driver-facing ETA / advisory engine pushed to "Trucking App and web
  platform" per the bid spec.
- This is the channel for re-routing during TFC-1 and TFC-3 scenarios.

DELIVERABLES (folder mobile-pwa/)
- Vite + React 18 + TypeScript PWA. service-worker via vite-plugin-pwa.
- Authentication is a simple device_id pairing (QR + 6-digit code) —
  no real OTP for PoC, but the screen exists and looks right.
- Screens:
    1. Trip — current target gate, ETA, current speed, traffic ahead
       (mini-map). Big "Slot at Gate" widget showing next allocated
       window from TAS-mock.
    2. Re-route — when a /api/trucks/{id}/route push arrives via
       WebPush or a fallback in-app polling, show a full-screen
       confirmation. "Accept" sends a state=ACK back.
    3. Inbox — list of advisories, alerts and challans.
    4. Profile / Vehicle — pulls VahanRecord via gateway.
- Realtime: a small worker that opens a WebSocket to
  wss://gateway/api/ws and filters messages by device_id.
- WebPush:
    * Service worker registers with the gateway /api/push/subscribe
    * Gateway uses pywebpush to deliver re-route notifications
    * VAPID keys generated by `make vapid-keys` and stored in env
- Offline: last 24 h of advisories cached via IndexedDB.

PERF TARGETS
- Lighthouse PWA score >= 90 on a Galaxy A5x baseline emulation.
- First contentful paint < 1.5 s on a throttled Fast 3G profile.

WEB VARIANT
- The same app at /pwa is served from web/ in Prompt 9 with a query
  parameter ?device=TRK-... — used so an evaluator without a phone
  can still receive the re-route push live during the demo.

TESTS
- Playwright e2e: pair a device, trigger TFC-1, assert the re-route
  banner appears within 5 s.

VERIFICATION CMD
open http://localhost:3000/pwa
```

---

# PROMPT 12 — Integration, Demo Script, and Evaluator Evidence Pack

```text
Final integration pass for the JNPA UC-III PoC: end-to-end smoke,
demo-script automation, and an evidence pack the bid team can include
in the technical-bid PoC annexure.

DELIVERABLES

1. End-to-end smoke
- tests/e2e/test_full_pipeline.py: starts the entire stack, waits
  for steady state (60 s), then sequentially verifies:
    a) ANPR ingestion is emitting at least 5 events/s
    b) Vahan adapter chain serves a known plate via LIVE_FALLBACK
    c) RFID + ANPR correlator emits vehicle.confirmed
    d) Congestion forecaster /metrics shows F1 >= 0.85
    e) ANPR /eval reports OCR_TARGET_MET = true
    f) Each of TFC-1, TFC-2, TFC-3 runs and resets cleanly
- Exit code 0 means all assertions passed.

2. Demo automation (scripts/demo_drive.py)
- A pretty CLI that walks an operator through the on-screen demo:
    Step 1: "Open http://localhost:3000/live — confirm map is live"
    Step 2: "Now triggering TFC-1 in 5 s..." -> automatically posts
    Step 3: "Watch trucking-app re-route at http://localhost:3000/pwa"
    ... etc. for TFC-2 and TFC-3.
- Each step also takes a Playwright screenshot saved to
  ./evidence/screenshots/{step}.png with a timestamp overlay.

3. Evidence pack (./evidence/)
- metrics.json containing:
    ocr_clean_accuracy, ocr_dust_accuracy, ocr_night_accuracy,
    congestion_f1, anomaly_precision, anomaly_recall,
    e2e_latency_p50, e2e_latency_p95, throughput_msgs_per_sec.
- screenshots/ (from demo_drive)
- trace_*.json from Jaeger for each scenario
- A one-page Markdown summary suitable for inclusion under the
  Technical Bid PoC annexure (`evidence/POC_SUMMARY.md`).

4. Hard-coded sanity checks
- Refuse to launch the demo if any of these are missing:
    * GOOGLE_MAPS_API_KEY or HERE_API_KEY (need at least one)
    * OPENWEATHER_API_KEY
    * Sample ANPR clips in ./data/clips/
- Exit with a human-readable error and a link to the README section.

5. Reset script
- `make demo-reset` returns the stack to a clean baseline after the
  evaluator walks away. Wipes ephemeral data, keeps trained models.

DOCUMENT THE TARGET KPIS IN README
| KPI                              | Target  | Evidence                |
| -------------------------------- | ------- | ----------------------- |
| ANPR exact-match (clean)         | >= 95%  | ai/anpr /eval           |
| ANPR exact-match (dust/fog)      | >= 92%  | ai/anpr /eval           |
| Congestion onset F1              | >= 0.85 | ai/congestion /metrics  |
| Wrong-way detection precision    | >= 0.95 | ai/anomaly test         |
| End-to-end alert latency p95     | <= 6 s  | e2e test                |
| Trucking-app device count        | 20,000  | ingest/trucking_app GET |
| Trucking-app scalable to         | 30,000+ | scale endpoint test     |
| Decision-path log retention      | 1000    | gateway /api/debug      |

VERIFICATION CMD (the one Aniket will run before any evaluator visit)
make up && sleep 60 && python scripts/demo_drive.py --record
open ./evidence/POC_SUMMARY.md
```

---

## Appendix A — Risk register for this PoC (read before Day-1 of build)

| Risk | Likelihood | Mitigation |
|---|---|---|
| Google Maps free tier exhausts mid-demo | Medium | HERE + TomTom adapters wired by default; congestion forecaster reads from cache for 90 s during outage. |
| OSRM demo server throttles 20k truck simulator | High | Pre-compute routes for 20k device profiles at startup and replay locally; fall back to bearing-based dead reckoning. |
| Indian-plate dataset for ANPR fine-tuning unavailable on demo machine | Medium | Ship a pre-baked PaddleOCR adapter in the repo. CI verifies hash; bench reproduces ≥ 95% without internet. |
| Surepass token absent | Certain at PoC | Demo defaults to LIVE_FALLBACK via vahan-sim. Decision path screen explicitly shows this so evaluator sees both code paths exist. |
| Vahan/Sarathi/FastTag schemas drift before contract signature | Low | Schema versioned in `jnpa_shared.schemas`. A nightly contract test pings Surepass with one known plate to detect breaking changes. |
| 20k MQTT devices crash on low-RAM laptop | Medium | `truck-sim` defaults to N=4000 in dev profile; `make up-full` for 20k; `make up-scale30k` for 30k+. Documented in README. |
| ByteTrack false positives for wrong-way on curved camera angles | Medium | Per-camera "allowed bearing range" configurable; default is wide; tuned during dry runs. |
| OCR drops below 95% under genuinely poor real-world clips | Low for PoC | The eval set is the contractual reference. Real-world tuning is a deliverable in the post-award implementation phase, not the PoC. |

---

## Appendix B — Order of operations and time estimate

| Step | Prompt | Build time | Cumulative |
|---|---|---|---|
| Bootstrap & infra | Prompt 0 | 5–10 min | 0:10 |
| ANPR ingest | Prompt 1 | 10–15 min | 0:25 |
| Vahan/Sarathi sim + live | Prompt 2 | 10–15 min | 0:40 |
| RFID emulator + consumer | Prompt 3 | 8–12 min | 0:52 |
| Trucking-app telemetry sim | Prompt 4 | 15–20 min | 1:12 |
| ANPR + OCR engine | Prompt 5 | 25–40 min (incl. fine-tune; skip with pre-baked) | 1:52 |
| Congestion forecaster | Prompt 6 | 20–30 min | 2:22 |
| Behavioural anomaly | Prompt 7 | 15–25 min | 2:47 |
| Gateway + fallback orchestrator | Prompt 8 | 15–20 min | 3:07 |
| Dashboard + heatmap | Prompt 9 | 30–45 min | 3:52 |
| Scenarios TFC-1/2/3 | Prompt 10 | 20–30 min | 4:22 |
| Trucking-app PWA | Prompt 11 | 25–35 min | 4:57 |
| Integration + evidence pack | Prompt 12 | 15–20 min | 5:17 |

Total: roughly half a day of supervised Claude Code work assuming API keys and the OCR pre-baked adapter are in place.

---

## Appendix C — Mapping each prompt back to the bid spec

| Bid sub-criterion (Appendix C §2.3) | Built in | Marks |
|---|---|---|
| 8.5.1 Multi-source data integration (ANPR, Vahan/Sarathi/FastTag, RFID, Trucking App 20k–30k) | Prompts 1–5 | 2 |
| 8.5.2 AI/ML tools (ANPR≥95%, GNN+LSTM F1≥0.85, ByteTrack+rule+AE, ETA engine) | Prompts 5–7 + 11 | 2 |
| 8.5.3 API / Data integration & Fallback (camera, Vahan, Trucking-App chains; provisional 24-h cure) | Prompt 8 | 2 |
| 8.5.4 Dashboard & KPI monitoring (40 km heatmap, queue, ETA, geo-fence, police reports) | Prompt 9 | 2 |
| 8.5.5 What-If + reactive workflow (TFC-1, TFC-2, TFC-3 cross-twin) | Prompt 10 | 2 |

---

## Appendix D — Things to NOT do

- **Do not use real JNPA TOS, FOIS, ICEGATE, SCADA, or CCTV data.** The bid explicitly reserves these for post-award. Your PoC must run entirely on the simulators, open-source APIs, and ULIP public schema as specified above. Touching real operational data pre-award is a disqualifier.
- **Do not promise live Vahan/Sarathi/FastTag access during the demo unless Surepass tokens are funded for the day.** The simulator path is the safe default and is explicitly anticipated by the bid.
- **Do not show JNPA-branded data sources you don't have access to.** Every screen must show "data: simulator (PoC)" where applicable. Evaluators reward honesty about source.
- **Do not exceed Google Maps free-tier quota during a single demo.** Pre-warm caches; the fallback chain is in place precisely so a quota hit does not break the screen.
- **Do not skip the evidence pack.** The technical bid will reference these screenshots and metrics; missing evidence undermines the verbal claim of ≥95% / F1≥0.85.

---

*End of prompt pack. Save this file at the root of your PoC repo as
`docs/CLAUDE_CODE_PROMPTS.md` so it stays version-controlled alongside
the build.*
