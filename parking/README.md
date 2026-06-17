# parking — real-time parking-availability service

A FastAPI service (port **8370**) that implements the parking-availability half
of **Appendix C requirement #1**: real-time parking availability *within* the
geo-fenced JNPA port area. It serves a static facility inventory plus a live
availability count (capacity / occupied / available) per facility that the
dashboard's **parking-availability board** renders.

## Endpoints

| Method | Path             | Returns                                            |
| ------ | ---------------- | -------------------------------------------------- |
| GET    | `/availability`  | per-facility live availability board (snapshot)    |
| GET    | `/facilities`    | static facility inventory (capacity + geo)         |
| GET    | `/summary`       | roll-up totals for the board header                |
| GET    | `/healthz`       | liveness                                           |
| GET    | `/metrics`       | Prometheus exposition                              |

`/availability` and `/summary` accept an optional `?minute_of_day=NNN`
(`0..1439`) override; without it they use the current wall-clock minute
(`hour*60 + minute`).

## Facility inventory

`facilities.py` defines **six** parking facilities, all at realistic lat/lon
*inside* the geo-fenced port near the JNPA gates (NSICT/JNPCT/BMCT/NSIGT aprons
at ~`[18.95, 72.95]` down to the truck-holding yard and the Common Parking Plaza
toward `[18.86, 73.0]`):

| ID         | Name                        | Gate        | Capacity |
| ---------- | --------------------------- | ----------- | -------- |
| PK-NSICT   | NSICT Gate-1 truck lot      | GATE-NSICT  | 120      |
| PK-JNPCT   | JNPCT gate lot              | GATE-JNPCT  | 90       |
| PK-BMCT    | BMCT gate lot               | GATE-BMCT   | 110      |
| PK-NSIGT   | NSIGT gate lot              | GATE-NSIGT  | 100      |
| PK-HOLDING | Truck holding yard          | GATE-NSICT  | 300      |
| PK-CPP     | Common Parking Plaza (CPP)  | CPP         | 450      |

Coordinates reuse the gate-apron / no-park-zone geometry from
`jnpa_shared.corridor`; an import-time guard asserts no facility falls inside a
no-parking polygon.

## Deterministic occupancy

`occupancy(facility_id, minute_of_day)` is a **pure function** — a smooth
diurnal curve (overnight baseline + morning/afternoon gate-in surges) seeded
from a hash of the facility id, bounded by capacity. There is **no wall-clock
RNG**, so a given `minute_of_day` always yields the same board:

- `snapshot(minute_of_day)` → per-facility rows with `capacity`, `occupied`,
  `available`, `utilisation_pct` and a `status`.
- `status` ∈ `{AVAILABLE (>20% free), FILLING (5–20% free), FULL (<5% free)}`.
- `summary(minute_of_day)` → `{total_capacity, total_occupied, total_available,
  facilities, full_count}`.

## Metrics

| Metric                      | Type  | Meaning                                       |
| --------------------------- | ----- | --------------------------------------------- |
| `parking_available_total`   | gauge | total free spaces across all facilities       |
| `parking_full_facilities`   | gauge | number of facilities currently FULL           |

Both gauges are refreshed on each `/availability` and `/summary` request.

## Verify

```bash
curl -s "http://localhost:8370/availability?minute_of_day=600" | jq .
curl -s  http://localhost:8370/facilities | jq .
curl -s "http://localhost:8370/summary?minute_of_day=600" | jq .
curl -s  http://localhost:8370/healthz | jq .
```
