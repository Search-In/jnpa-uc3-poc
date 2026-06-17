# carbon — carbon-emissions calculator (Appendix C #6)

A FastAPI service (port **8340**) that computes road-freight CO2e for the
trailers currently in the JNPA UC-III **Area of Interest (AoI)** — the NH-348
port → Karal Phata corridor plus CPP / parking-area dwell. It implements
Appendix C requirement **#6**: emissions from fleet-transporter trip activity
(distance × payload) plus idle/parking dwell, with an AoI rollup.

Emission factors are **published IPCC / GHG-Protocol road-freight factors**
(gCO₂e per tonne-km by vehicle class, plus a gCO₂e/idle-minute idling rate) —
documented constants in [`factors.py`](./factors.py), not invented numbers. The
fleet-transporter fuel/telematics *activity* is simulated (see
[`docs/ASSUMPTIONS.md`](../docs/ASSUMPTIONS.md) "Carbon (C6)"); the factors
applied to it are real.

## Endpoints

| Method | Path        | Returns                                                |
| ------ | ----------- | ------------------------------------------------------ |
| GET    | `/healthz`  | liveness `{status, service}`                           |
| GET    | `/metrics`  | Prometheus exposition                                  |
| GET    | `/rollup`   | AoI rollup: total CO₂e, by class, by moving/idle       |
| POST   | `/estimate` | emissions for one `{distance_km,payload_tonnes,idle_minutes,vehicle_class}` |

## Calculation

Pure functions live in [`calculator.py`](./calculator.py) (no I/O, no clock, no
randomness):

- `trip_emissions_kg(distance_km, payload_tonnes, vehicle_class)` — moving
  (well-to-wheel) emissions; linear in distance and payload.
- `idle_emissions_kg(idle_minutes, vehicle_class)` — CPP/parking dwell idling;
  linear in minutes.
- `vehicle_emissions_kg(...)` — moving + idle for one vehicle.
- `aoi_rollup(trips)` — `{total_kg, by_class, by_source:{moving,idle}, vehicle_count}`.

### Factors (documented constants)

| Class  | gCO₂e / tonne-km | gCO₂e / idle-min | Basis                                              |
| ------ | ---------------- | ---------------- | -------------------------------------------------- |
| HGV    | 62               | 134              | IPCC/DEFRA articulated HGV (well-to-wheel diesel)   |
| RIGID  | 85               | 134              | DEFRA rigid HGV band                                |
| LGV    | 110              | 60               | DEFRA van / light-commercial band                  |
| REEFER | 78               | 224              | HGV base + GLEC/DEFRA refrigeration uplift          |

## Deterministic AoI fleet

`/rollup` runs over a SHA-256-seeded synthetic fleet (`seed_aoi_fleet`, fixed
`SEED`) — distances, payloads and dwell times are all derived from the trip
index, so the figure is **identical across runs and hosts** with no unseeded
randomness. Size is tunable via `CARBON_AOI_FLEET_SIZE` (default 200).

## Prometheus

- `carbon_aoi_total_kg` (gauge) — total CO₂e for the in-AoI fleet.
- `carbon_estimates_total` (counter, by `vehicle_class`) — `/estimate` calls.

## Verify

```bash
curl -s http://localhost:8340/rollup | jq .
curl -s -X POST http://localhost:8340/estimate \
  -H 'content-type: application/json' \
  -d '{"distance_km":40,"payload_tonnes":20,"idle_minutes":60,"vehicle_class":"REEFER"}' | jq .
curl -s http://localhost:8340/healthz | jq .
```
