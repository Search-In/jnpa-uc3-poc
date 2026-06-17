# empty-container — empty-container supply-demand optimiser

A FastAPI service (port **8330**) implementing **Appendix C requirement #3**: an
empty-container supply-demand optimiser that produces a *probable allocation*
across **fleet owners / shipping line / CFS / Empty Container Depot (ECD)**,
including the **tanker / break-bulk / cement-bowser** cargo variants. The mean
estimated turn-round time over those allocations drives the **TRT for empty from
ECD** acceptance KPI (`trt_empty_ecd`).

## Endpoints

| Method | Path                | Returns                                             |
| ------ | ------------------- | --------------------------------------------------- |
| GET    | `/healthz`          | `{status, service, depots, demand}`                 |
| GET    | `/metrics`          | Prometheus exposition (mounted)                     |
| GET    | `/allocations`      | `{allocations:[...], count, unsatisfied}`           |
| GET    | `/supply`           | depots + empty-container stock                      |
| GET    | `/demand`           | open demand (seeded + injected)                     |
| GET    | `/kpi/trt_empty`    | `compute_kpi("trt_empty_ecd", <mean est_trt>)`      |
| POST   | `/demand/inject`    | add one synthetic demand (scenarios), deterministic |

## Deterministic books

`seed.py` builds two fully deterministic books — there is **no** `Date.now()` or
unseeded RNG, every value is a SHA-256 hash of a fixed `SEED` plus the record key
(mirroring `ingest/vahan_sim/seed.py`), so allocations and the KPI are identical
run-to-run and host-to-host:

- **Supply book** — ECD + CFS depots (`DEPOT_CATALOGUE`) near JNPA `[18.86, 73.0]`,
  each with empty-container stock by type **20GP / 40GP / 40HC / REEFER** and a
  current yard dwell. ECDs carry deeper stock and lower dwell than CFS yards;
  REEFER stock is deliberately scarce so some demand can go unsatisfied.
- **Demand book** — shipping-line bookings + fleet-owner requests, each with an
  origin / destination / priority and a `cargo_type` in
  `{container, oil_tanker, break_bulk, cement_bowser}`. The first records pin one
  of every cargo variant so the tanker / break-bulk / cement-bowser paths are
  always exercised.

## Optimiser (`optimizer.py`)

A pure, transparent, **explainable** cost-minimising matcher —
`allocate(supply, demand) -> list[Allocation]`:

```
cost = W_DISTANCE * haversine_km(depot, origin)
     + W_DWELL    * depot_dwell_min
     + W_PRIORITY * priority_penalty
```

The lowest-cost depot still holding the required container type wins; ties break
on `depot_id`; stock is decremented as demands are filled (high-priority first).
Each `Allocation` carries `{demand_id, supply_depot, container_type, cargo_type,
distance_km, est_trt_min, confidence}` plus the cost components, so an operator
can audit *why* a depot was chosen — no black box. Haversine is reused from
`jnpa_shared.corridor`.

`est_trt_min = drive_time(distance, ~28 km/h) + depot_dwell + fixed gate turn`,
and the mean over allocations feeds `jnpa_shared.kpi.compute_kpi("trt_empty_ecd",
…)` (target 45 min, baseline 72 min).

## Verify

```bash
PYTHONPATH=empty-container:shared python -m empty_container.seed
curl -s http://localhost:8330/healthz | jq .
curl -s http://localhost:8330/allocations | jq '.count, .unsatisfied'
curl -s http://localhost:8330/kpi/trt_empty | jq .
curl -s -X POST http://localhost:8330/demand/inject \
  -H 'content-type: application/json' \
  -d '{"cargo_type":"oil_tanker","priority":"high"}' | jq .
```
