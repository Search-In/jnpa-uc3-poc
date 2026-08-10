# UC3-003 — CFS / ECY gate-event ingestion + empty-container TRT (KPI 3)

Loads the two real JNPA CODECO gate logs into `core.container_event`, materialises
the empty-container lifecycle from them, and computes **KPI 3 — "TRT for empty
containers from ECD"** (target **45 min**, baseline **72 min**) over the chains
that the corpus actually supports end to end.

Everything below is measured from the customer's own workbooks. No count is
configured in code except the target and the baseline, which come from
`shared/jnpa_shared/kpi.py` and are shared with the other three Appendix-C KPIs.

---

## 1. Source

| File | Rows | Legs |
|---|---|---|
| `Data/13-CFS-ECY/ECY-CODECO.xlsx` | **961** | 529 gate-OUT · 432 gate-IN |
| `Data/13-CFS-ECY/CFS-CODECO.xlsx` | **968** | 484 gate-IN · 484 gate-OUT |
| **Total** | **1 929** | |

Three columns — `Container Number`, `Timestamp` (`DD/MM/YYYY HH:MM`, IST, no
timezone) and `Mode` (`In`/`Out`). The **facility is carried by the filename**,
not by a column.

| file | Mode | `event_type` | `location_type` | `direction` |
|---|---|---|---|---|
| ECY | Out | `ECY_OUT` | `ECY` | `O` |
| ECY | In  | `ECY_IN`  | `ECY` | `I` |
| CFS | In  | `CFS_IN`  | `CFS` | `I` |
| CFS | Out | `CFS_OUT` | `CFS` | `O` |

---

## 2. The planted anomaly — detected, not patched

The ECY feed does not pair: **529 gate-OUT against 432 gate-IN**, a gap of **97**.
The reason is structural, not a transcription slip:

* the OUT block spans **01–12 Jul 2026** and the IN block **12–26 Jul 2026**;
* the two blocks share **zero** of their 961 containers.

Consequence worth knowing: the customer's own view **`mart.v_ecy_trt`** pairs an
`ECY_IN` with that container's next `ECY_OUT` — against this corpus it therefore
returns **no rows at all**. That view is left exactly as they wrote it. UC3-003
measures the leg the data does support (ECY → CFS) in a *separate* view.

Nothing is repaired. Every one of the 1 929 rows is imported verbatim: no row is
deleted, no timestamp or container number is altered, no partner event is
invented, and the one exact-duplicate source row is stored **twice** because the
source has it twice. What the importer does instead is record eight grouped
findings in the existing `core.dq_issue` ledger:

| issue_type | severity | what it says |
|---|---|---|
| `count_mismatch` | warn | ECY log unpaired: 529 OUT vs 432 IN (97 unpairable) |
| `disjoint_ranges` | warn | the OUT and IN blocks are date-disjoint, 0 shared containers |
| `missing_key` | warn | 287 ECY gate-OUTs never reach a CFS |
| `missing_key` | warn | 432 ECY gate-INs have no ECY gate-OUT |
| `missing_key` | warn | 241 CFS containers have no ECY origin |
| `too_clean` | info | the CFS log is perfectly paired (484/484) beside the unpaired ECY log |
| `duplicate` | warn | 1 exact-duplicate source row preserved with its multiplicity |
| `duplicate` | warn | 1 container with two CFS gate-OUTs |

Grouped, not one row per record — the per-container detail is served live by
`GET /api/cfs-ecy/empty-trt/anomalies/{code}`.

---

## 3. Lifecycle and KPI

A chain is **COMPLETE** when a container has `ECY_OUT` → `CFS_IN` → `CFS_OUT` in
that order. Against this corpus that is **242 containers** (the remainder: 528
PARTIAL, 432 ORPHAN — 1 202 containers in total).

```
TRT  = ECY gate-out  → CFS gate-in     (KPI 3, "ECD pickup to gate-in")
dwell= CFS gate-in   → CFS gate-out    (supporting)
cycle= ECY gate-out  → CFS gate-out    (supporting)
```

`trt_min` is the project's existing definition, not a new one:
`jnpa_shared.kpi.trt_empty_ecd_min()` is documented as *"mean empty-container
turn-round time from ECD pickup to gate-in"*, and the value is scored by
`compute_kpi("trt_empty_ecd", …)` like every other Appendix-C KPI.

Measured result over the 242 valid chains:

| | value |
|---|---|
| Mean TRT | **204.05 min** (3 h 24 m) |
| Median | 180 min |
| Fastest / slowest | 120 min / 300 min |
| Target | 45 min → **+159.05 min over** |
| Baseline | 72 min → **+132.05 min over (+183.4 %)** |
| Mean CFS dwell | 5 482.58 min |
| Mean full cycle | 5 686.63 min |

The KPI is **over target**. That is what the customer's data says; it is reported
as measured, with `source: "live"` and `n: 242`, and the excluded records are
shown beside it rather than averaged in.

Worked example — `ONEU2122848`:

```
ECY gate-out   01/07/2026 10:00 IST
CFS gate-in    01/07/2026 14:00 IST      TRT 240 min
CFS gate-out   07/07/2026 08:16 IST      dwell 8 296 min · cycle 8 536 min
```

---

## 4. What was built

| Layer | File |
|---|---|
| Migration | `infra/postgres/v3/0133_uc3_003_empty_container_trt.sql` |
| Importer | `scripts/import_uc3_003_cfs_ecy.py` |
| Repository / service | `services/cfs_ecy/trt_repository.py`, `trt_service.py` |
| DQ ledger service | `services/dq/` |
| Routers | `gateway/routers/cfs_ecy.py` (extended), `gateway/routers/dq.py` (new) |
| KPI strip | `gateway/routers/kpi.py` — `trt_empty_ecd` now prefers the real gate log |
| UI | `web/src/screens/cfs/EmptyTrtPanel.tsx`, tab added to `CfsEcyMovements.tsx` |
| Tests | `tests/test_uc3_003_cfs_ecy_trt.py` |

### Migration 0133

Purely additive and idempotent — three `CREATE INDEX IF NOT EXISTS` and two
`CREATE OR REPLACE VIEW`. No table is created, altered or dropped, no row is
written, and `mart.v_ecy_trt` is untouched.

* `mart.v_empty_container_chain` — per-container leg roll-up, aggregation only,
  with per-leg event **counts** so source duplicates stay visible.
* `mart.v_empty_container_trt` — adds `chain_status`, `trt_min`, `dwell_min`,
  `cycle_min` and `anomaly_codes`. A duration is `NULL` unless both endpoints
  exist *and* are correctly ordered, so a bad pair can never contribute a
  negative sample.

### Importer idempotency

`core.container_event` has **no** unique constraint, and must not gain one: the
corpus legitimately contains the same gate event twice. So the importer is
**multiplicity-aware** — for each distinct `(container_no, event_ts, event_type)`
it inserts `source_count − already_present_count` rows.

```
first run   events inserted = 1929   already present = 0
second run  events inserted = 0      already present = 1929
```

`core.ingest_file` is upserted on its natural key (`path`), and only this
importer's own `core.dq_issue` rows (`source_table = 'core.container_event'`
scoped to the two file ids) are refreshed on a re-run.

---

## 5. API

All read-only; `/api/cfs-ecy` and `/api/dq` are not in `gateway/auth.py._POLICY`,
so both inherit the default "any authenticated role" rule.

```
GET /api/cfs-ecy/events                      ?container=&location_type=&event_type=
                                             &direction=&from=&to=&sort=&order=&limit=&offset=
GET /api/cfs-ecy/empty-trt                   KPI + distribution + source inventory
                                             + anomalies + DQ findings + daily trend
GET /api/cfs-ecy/empty-trt/chains            ?container=&chain_status=&anomaly_code=&anomaly_only=
GET /api/cfs-ecy/empty-trt/anomalies/{code}  the containers behind one finding
GET /api/cfs-ecy/empty-trt/containers/{cn}   one container: legs, durations, raw events

GET /api/dq/issues                           ?source_table=&issue_type=&severity=&file_id=&q=
GET /api/dq/summary                          roll-up by severity / source table / issue type
```

`GET /api/kpi/strip` now sources `trt_empty_ecd` from the real gate log, falling
back to the empty-container service's estimate and then to the labelled baseline.

The existing `/api/cfs-ecy/movements`, `/stats`, `/dwell`, `/chains*` and
`/upload*` endpoints are unchanged — they serve `core.cfs_ecy_movement`, which
also holds uploaded CODECO batches, whereas KPI 3 must be computed from the
corpus gate log alone.

---

## 6. Running it

```bash
# 1. schema (additive, idempotent)
psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/v3/0133_uc3_003_empty_container_trt.sql

# 2. inspect the corpus without touching the database
python scripts/import_uc3_003_cfs_ecy.py --dry-run

# 3. import (safe to repeat)
POSTGRES_DSN='postgresql+asyncpg://…' python scripts/import_uc3_003_cfs_ecy.py
```

The corpus is discovered from `$JNPA_CORPUS_DIR`, `data/13-CFS-ECY`, or the usual
`Data/13-CFS-ECY` drop locations; override with `--corpus PATH`.

### Tests

```bash
pytest tests/test_uc3_003_cfs_ecy_trt.py                      # corpus + KPI + routers
UC3_TEST_DSN='postgresql+asyncpg://…' \
  pytest tests/test_uc3_003_cfs_ecy_trt.py                    # + the live-database layer
```

The live layer asserts the stored row counts, the preserved duplicate, the DQ
ledger, the 242 chains, the hero container's timeline, and that a re-import
inserts nothing.

---

## 7. UI

`/cfs-ecy` → **Empty TRT (KPI 3)** tab:

1. **KPI hero** — current TRT, target 45 min, baseline 72 min, variance against
   both, median/fastest/slowest, valid container count, measurement window, and a
   `Live · real CODECO corpus` provenance badge.
2. **Source & anomalies** — the registered source workbooks with their row counts
   and load time, the four leg totals (529 / 432 / 484 / 484) with the pairing
   gap spelled out, and the anomaly codes; clicking one lists the containers.
3. **Data Quality ledger** — the `core.dq_issue` findings verbatim, badged
   *Detected · not patched*.
4. **Container lookup** — search a container (e.g. `ONEU2122848`) to see its
   ECY-out → CFS-in → CFS-out chain, its durations, whether it counts toward the
   KPI, and the raw CODECO events with their source file and row number.
5. **Chains table** — Complete / Partial / Orphan, paginated server-side.

The dashboard KPI strip (`/live`, `/command-center`) shows the same TRT value.
