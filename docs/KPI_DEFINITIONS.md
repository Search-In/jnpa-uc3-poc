# KPI Definitions — JNPA Use Case III

Every KPI in the dashboard is computed by a **pure, unit-tested function** in
[`shared/jnpa_shared/kpi.py`](../shared/jnpa_shared/kpi.py) and returns the same shape:

```python
class KpiResult:
    key: str                 # stable id, e.g. "gate_queue_wait"
    label: str               # human label
    unit: str                # "min", "min/hr", "vph", "%"
    value: float             # current value
    target: float            # acceptance target (Appendix C / configurable baseline)
    baseline: float          # current-baseline-ops value the target improves on
    delta_pct: float         # % change vs baseline (negative = improvement for "lower-is-better")
    direction: str           # "lower_is_better" | "higher_is_better"
    on_target: bool          # value meets/beats target
    trend: list[float]       # recent samples (oldest -> newest) for the sparkline
```

The web layer mirrors this as `KpiResult` in [`web/src/kpi/types.ts`](../web/src/kpi/types.ts);
the same arithmetic is re-tested in Vitest so the on-screen number always matches the engine.

`delta_pct` is always expressed as **% improvement vs the current baseline ops** the Appendix-C
KPI table is scored on. For *lower-is-better* metrics a reduction is reported as a negative delta
(e.g. wait time down 22% → `delta_pct = -22.0`, `on_target = True`).

---

## Acceptance KPIs (Appendix C §2.3)

| key | Label | Unit | Direction | Target (default) | Definition |
|---|---|---|---|---|---|
| `gate_queue_wait` | Gate Queue Wait Time | min | lower | 8.0 | Mean minutes a vehicle spends in `AT_GATE_QUEUE` before the boom, per gate, per hour. Derived from telemetry dwell where `speed_kmh ≤ 3` inside the gate approach geofence. |
| `gate_txn_time` | Avg Gate Transaction Time | min | lower | 3.0 | Mean minutes from boom-arrival to boom-clear (ANPR read → gate-clear event). |
| `trt_empty_ecd` | TRT empty from ECD | min | lower | 45.0 | Turn-round time for an empty container from Empty-Container Depot pickup to gate-in, from the empty-container allocation timeline. |
| `tat_inside_port` | TAT inside port | min | lower | 90.0 | Turn-around time from gate-in to gate-out for a vehicle inside the port AoI. |

## Operational roll-ups (Bid §8.5.4)

| key | Label | Unit | Direction | Target (default) | Definition |
|---|---|---|---|---|---|
| `queue_length` | Queue Length | vehicles | lower | 25 | Count of vehicles currently in `AT_GATE_QUEUE` per gate (live). |
| `avg_dwell` | Avg Vehicle Dwell | min | lower | 12.0 | Mean dwell across all vehicles inside the AoI in the window. |
| `gate_throughput` | Gate Throughput | vph | higher | 60 | Distinct vehicles cleared per gate per hour (ANPR-clear events). |

---

## Targets & baselines

Targets and baselines live in `KPI_TARGETS` in `shared/jnpa_shared/kpi.py` and can be overridden
per-deployment without touching the arithmetic. The defaults above are the PoC demonstration
values; the production values come from the JNPA baseline study (jnport.gov.in Reports / NLDS) and
are recorded in [ASSUMPTIONS.md](ASSUMPTIONS.md).

## Why pure functions

- **Testable**: each KPI has a Vitest/pytest case asserting `value`, `delta_pct`, and `on_target`
  for fixed inputs — the on-screen number can never silently drift from the definition.
- **Adapter-agnostic**: the same function runs over mock fixtures (instant demo) or live Timescale
  rows (production), so `mock` and `live` modes show identically-shaped KPIs.
- **No hidden SQL**: the SQL `jnpa.kpi_*` views remain as fast pre-aggregations, but the
  target/delta/trend semantics the evaluator sees are defined in one place in code.
</content>
