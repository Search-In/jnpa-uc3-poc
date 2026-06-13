# Trucking-app telemetry simulator (`ingest/trucking_app/`)

A 20,000-device (hot-scalable to 30,000+) GPS telemetry simulator for the
trucking-app component of the JNPA UC-III PoC (Sub-Criterion 1D, Appendix B5).

Each simulated device drives a realistic truck along the NH-348 corridor into
one of the four JNPA gates and back home, publishing telemetry to **MQTT** and
**Kafka** and batch-writing it to **Timescale** — at a sustained ~4,000 msg/s.

```
asyncio + uvloop + aiomqtt
        │
  Fleet (20k Trucks) ── Router (OSRM → HERE → dead-reckoning)
        │                     └─ Redis jam_factor (queueing pressure)
   Simulator (tick wheel)
        ├─ MQTT   trucks/{device_id}/telemetry   (qos 0)   + .../eta (qos 1)
        ├─ Kafka  truck.telemetry  +  truck.eta
        └─ Timescale  jnpa.truck_telemetry  (batched COPY every 30 s)
   FastAPI control plane :8240  (/devices, /devices/scale, /devices/{id}/route)
```

## What each device does

A truck has a **plate** (linked to the Vahan simulator — `trucking_app.plates`
mirrors `vahan_sim`'s deterministic generator so every plate resolves via
`GET /vahan/rc/{plate}`), a `device_id` (`TRK-000001` …), a **target gate**
(round-robin over the 4 JNPA gates), a random **origin** within 100 km, and a
state machine:

```
EN_ROUTE_TO_PORT → AT_GATE_QUEUE → INSIDE_PORT → EN_ROUTE_HOME → IDLE ↺
```

- **Position update interval**: 5 s default, **2 s** when `AT_GATE_QUEUE`.
- **Route**: an OSRM route origin→gate (primary: the public OSRM demo;
  fallback: HERE if `HERE_API_KEY` is set; final fallback: straight-line
  bearing dead-reckoning snapped onto the NH-348 corridor polyline).
- **Speed model**: 55 km/h free-flow on highway, 25 km/h on port roads, 0 when
  `AT_GATE_QUEUE`; Gaussian noise σ=4 km/h; scaled down by the dashboard's
  congestion score for the current segment (Redis
  `traffic:segment:{id}:jam_factor`).
- **GPS noise**: ε ~ N(0, 6 m) on lat/lon, with a 1 % chance of a 50 m outlier.
- **ETA**: every 30 s, compute ETA-to-target-gate from the live OSRM duration
  (falling back to a speed-based estimate) and publish to
  `trucks/{device_id}/eta` and Kafka `truck.eta`.

## Control plane (FastAPI, port 8240)

| Method | Path                         | Purpose                                          |
| ------ | ---------------------------- | ------------------------------------------------ |
| GET    | `/devices?n=20000`           | current population stats (count + by-state)      |
| POST   | `/devices/scale {target:N}`  | hot-scale the population (bounded by max_devices)|
| POST   | `/devices/{device_id}/route` | override a device's route (TFC-1 gate closure)   |
| GET    | `/devices/{device_id}`       | one device's live snapshot                       |
| GET    | `/healthz`                   | liveness                                         |
| GET    | `/metrics`                   | Prometheus exposition                            |

The route-override body takes either `{"gate_id": "G-BMCT"}` (reroute to a JNPA
gate) or `{"lat": .., "lon": ..}`, with an optional `force_state`. This is the
hook Prompt 8's TFC-1 gate-closure scenario uses to divert trucks off a closed
gate.

## Performance

20k devices × a 5 s interval ≈ **4,000 msg/s** sustained. The engine avoids
20k asyncio tasks: a single **tick scheduler** (1 s cadence) processes only the
trucks whose per-device deadline has passed, with deadlines spread evenly across
the interval so each tick handles ~population/interval trucks. Position updates
use MQTT **qos=0** (lossy-OK at rate), ETA/state use **qos=1**. The Kafka
producer is non-blocking (JSON+snappy); Timescale writes go through asyncpg
binary **COPY** every 30 s. `uvloop` is installed when available.

Tunables (env): `TRUCK_NUM_DEVICES`, `TRUCK_MAX_DEVICES`, `TRUCK_INTERVAL_S`,
`TRUCK_INTERVAL_GATE_S`, `TRUCK_ETA_INTERVAL_S`, `TRUCK_DB_FLUSH_S`,
`TRUCK_ROUTE_CONCURRENCY`, `OSRM_BASE_URL`, `HERE_API_KEY`, `TRUCK_SEED`.

## Run

```bash
# In the stack (compose service truck-sim, 3 CPU reservation):
make up

# Verify:
curl -s http://localhost:8240/devices | jq '.population'
mosquitto_sub -h localhost -t 'trucks/+/telemetry' -C 5

# Hot-scale to 30k:
curl -s -XPOST http://localhost:8240/devices/scale \
  -H 'content-type: application/json' -d '{"target":30000}' | jq .

# Reroute one device (TFC-1 gate-closure example):
curl -s -XPOST http://localhost:8240/devices/TRK-000001/route \
  -H 'content-type: application/json' -d '{"gate_id":"G-BMCT"}' | jq .

# One-shot smoke test:
make truck-verify
```

## Tests

`tests/test_trucking_app.py` (run by `make test`, skipped if infra is down):

1. **CI scale (N=500)** — start the simulator with 500 devices and assert ≥ 90 %
   publish telemetry within 10 s.
2. **Hot-scale** — `POST /devices/scale {target}` and assert the population
   reaches the target within 30 s.

Pure-logic tests (state machine, plate↔Vahan linkage, GPS noise, dead-reckoning)
run without any infra.
