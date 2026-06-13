# RFID emulator + MQTT ingestion + ANPR correlation (UC-III Sub-Criterion 1C)

Emulates the NLDS-installed UHF RFID readers referenced in Appendix A2 and lands
their reads in Timescale, then correlates them with ANPR plate reads to produce
confirmed-vehicle events for the boom-barrier decision and the gate-throughput
KPI.

One Python package (`rfid_ingest`) backs three console scripts / compose
services that share a single Docker image:

| Service          | Script             | Role                                                        |
| ---------------- | ------------------ | ----------------------------------------------------------- |
| `rfid-emulator`  | `rfid-emulator`    | 25 logical readers → MQTT `rfid/readers/{reader_id}`        |
| `rfid-consumer`  | `rfid-consumer`    | MQTT `rfid/readers/+` → `jnpa.rfid_reads` + Kafka `rfid.reads` |
| `rfid-correlator`| `rfid-correlator`  | join `rfid.reads` × `anpr.reads` (5 s/gate) → `vehicle.confirmed` |

## Topology (25 readers)

- **10 gate readers** spread round-robin across the 4 JNPA gates
  (`G-NSICT`, `G-JNPCT`, `G-NSIGT`, `G-BMCT`). These carry a `gate_id`, so they
  participate in the per-gate RFID↔ANPR join.
- **15 corridor readers** dropped on evenly-spaced segment midpoints of the
  NH-348 corridor from `jnpa_shared.corridor` (SEG-00 … SEG-12). No `gate_id`;
  they only feed Timescale.

Reader ids are `R-01` … `R-25` (gate readers first). See
[rfid_ingest/topology.py](rfid_ingest/topology.py).

## Tag pool & truck movement

Tag ids are drawn from a **fixed, seeded pool of 12,000** UHF EPCs
(`E2801160…`, 24 hex chars). Because the pool is deterministic, the *same truck*
(tag) is consistent everywhere. The emulator models lightweight **truck
journeys**: a truck enters at a gate reader, then is re-seen at successive
corridor readers ~2–6 s apart as it travels — so a tag genuinely appears across
multiple readers within a few seconds, which is what the correlator relies on.

## Rates & peak hours

Each reader is an independent **Poisson** source of pass-throughs. The base mean
rate (`RFID_BASE_RATE`, reads/s/reader) is multiplied by `RFID_PEAK_MULTIPLIER`
during the IST peak windows **08:00–11:00** and **18:00–21:00**
(`peak_windows_ist`). RSSI is sampled around `RFID_RSSI_MEAN` (default −55 dBm).

## Payload (MQTT)

```json
{ "ts": "2026-06-13T08:14:02.123456+00:00",
  "reader_id": "R-08", "tag_id": "E2801160...", "rssi": -42.3 }
```

Validated by `jnpa_shared.schemas.RfidRead` on the consumer side.

## vehicle.confirmed (Kafka)

```json
{ "ts": "...", "plate": "MH04AB1234", "rfid_tag": "E2801160...",
  "camera_id": "CAM-NSICT-ENT", "gate_id": "G-NSICT", "confidence": 0.97 }
```

Emitted when a tag read and a plate read land at the **same gate within 5 s**.
Camera→gate is loaded from `jnpa.cameras` (static fallback if Postgres is down);
reader→gate comes from the emulator topology.

## Resilience

Both MQTT clients use `connect_async` + `loop_start` with exponential
reconnect backoff (1 s → 30 s), and the consumer re-subscribes on every
(re)connect — a broker restart self-heals with no intervention. The consumer
also retries Postgres connect and insert batches, and the correlator restarts
its Kafka consume loops on error.

## Run / verify

```bash
make up                          # brings up mosquitto, kafka, postgres + these 3 services

# confirmed-vehicle event flowing:
docker compose logs -f rfid-correlator | grep -m1 vehicle.confirmed

# reads landing in Timescale, busiest readers first:
make psql -c "select reader_id, count(*) from jnpa.rfid_reads group by 1 order by 2 desc limit 5;"
```

## Configuration (env)

| Var | Default | Meaning |
| --- | --- | --- |
| `RFID_GATE_READERS` / `RFID_CORRIDOR_READERS` | 10 / 15 | fleet size |
| `RFID_TAG_POOL_SIZE` | 12000 | distinct truck identities |
| `RFID_BASE_RATE` | 0.15 | off-peak reads/s/reader |
| `RFID_PEAK_MULTIPLIER` | 3.0 | peak-hour rate multiplier |
| `RFID_CORRELATION_WINDOW_S` | 5.0 | per-gate join window |
| `RFID_CONFIRM_CONFIDENCE` | 0.97 | confidence stamped on `vehicle.confirmed` |
| `MQTT_HOST` / `MQTT_PORT` | mosquitto / 1883 | broker |
| `KAFKA_BROKERS` | kafka:9092 | broker |
| `POSTGRES_DSN_LIBPQ` | (derived) | asyncpg libpq DSN |
| `METRICS_PORT` | 9102 | Prometheus exposition |

Metrics are exposed on `:9102/metrics` (published per service on distinct host
ports — see `docker-compose.yml`).
