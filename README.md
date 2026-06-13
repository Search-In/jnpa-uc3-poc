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
│   └── _synth_clip.py
├── data/clips/               # bind-mounted into anpr-ingest (.mp4 clips)
├── ingest/anpr/              # ANPR ingestion service (replay -> YOLOv8n -> Kafka)
│   ├── Dockerfile  pyproject.toml
│   └── src/anpr_ingest/      # config, replay, detect, emit, weather, metrics, main
├── ai/  gateway/  web/  mobile-pwa/  scenarios/   # later PoC stages
└── tests/                    # test_bootstrap.py, test_anpr_ingest.py
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
