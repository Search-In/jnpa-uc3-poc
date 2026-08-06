# UC-3 FINAL IMPLEMENTATION REPORT

**Date:** 06 August 2026 · **Branch:** `dev_aniket` · **Baseline:** `f4b3cea`
**Scope:** close the findings in `UC3_CARGO_BACKEND_AUDIT_REPORT.md` and make the backend able to answer the JNPA What-If Notice scenarios.
**Constraint honoured:** no rewrite, no parallel system. Every change follows the existing Router → Service → Repository → Migration pattern, FastAPI + async raw SQL, the existing event bus, the existing RBAC, and the existing test style.

**Tests: 165 passed, 2 skipped** across the changed suites (`test_cargo.py` 86, `test_cargo_simulation.py` 55, `test_gate_documents.py` 24). Regression subset (gateway, auth/RBAC, container-job, capabilities, workflow-audit, quality): **126 passed, 1 skipped**. All four migrations parse under a real Postgres grammar (`pglast v8.4`) and are discovered by `scripts/migrate.py`.

---

## Completed Fixes

### W1 — `POST /api/cargo` could create an already-released container

| | |
|---|---|
| **File** | [services/cargo/service.py:262-283](services/cargo/service.py#L262-L283) |
| **Change** | `create_cargo` now raises `CargoTransitionError(cn, CREATED, RELEASED)` when `is_released` is truthy. A create always lands on the `CREATED` DB default, so it may not simultaneously claim RELEASED. |
| **File** | [gateway/routers/cargo.py:768-774](gateway/routers/cargo.py#L768-L774) |
| **Change** | Maps it to the same 409 `illegal_transition` envelope every other lifecycle refusal uses. |
| **Why it mattered** | This produced `is_released=true, lifecycle_status='CREATED'` — exactly the corruption migration `0115` had to backfill away (5 rows). `0115`'s header claimed "the service now refuses the same input"; the PUT path was fixed then, the POST path was not. |
| **Tests** | `test_create_rejects_release_true`, `test_create_still_accepts_is_released_false` |

Three pre-existing tests seeded released containers through this hole. They now walk the real lifecycle via a new `_seed_released()` helper — which is what a caller has to do in production too.

### W2 — release was not atomic

| | |
|---|---|
| **File** | [services/cargo/repository.py:534-600](services/cargo/repository.py#L534-L600) |
| **Change** | `transition_lifecycle` gained a `set_fields` parameter. Whitelisted writable columns are patched **in the same locked `UPDATE`** as the status change. Column identifiers come from the existing `_WRITABLE` tuple, never client input; every value is bound. |
| **File** | [services/cargo/service.py:520-545](services/cargo/service.py#L520-L545) |
| **Change** | `release_cargo` passes `set_fields={"is_released": True}` and the second `repo.update(...)` call is gone. One commit, or neither. |
| **Why it mattered** | Previously transaction #1 set `lifecycle_status='RELEASED'` and transaction #2 set `is_released=true`. A failure between them left a container released to the state machine but invisible to every `is_released` filter — including the driver role scope. |
| **Tests** | `test_release_is_atomic` (asserts exactly **one** write reaches the repository), `test_release_failure_leaves_no_divergence` (injects a failure; asserts neither field moved) |

### W3 — concurrent release race

| | |
|---|---|
| **File** | [services/cargo/service.py:340-380](services/cargo/service.py#L340-L380) |
| **Change** | `update_cargo`'s release branch routes the whole patch through `_advance` → `transition_lifecycle`, where the gate is evaluated **under `SELECT … FOR UPDATE`**. The old read → `can_transition` → unlocked `update()` sequence is gone. |
| **Why it mattered** | Two concurrent `PUT {is_released:true}` calls could both read a pre-release snapshot, both pass the gate, and both write. |
| **Tests** | `test_concurrent_release_produces_exactly_one_winner` — a **genuine** concurrency test: the fake repository models `FOR UPDATE` with an `asyncio.Lock` and yields inside it, then two `update_cargo` coroutines race via `asyncio.gather`. Exactly one wins, the loser gets `CargoTransitionError(current='RELEASED')`, and exactly one RELEASE audit row exists. Without the fix both would succeed. Plus `test_concurrent_release_gate_is_evaluated_under_the_lock` at the service layer. |

One deliberate semantic left as-is and now pinned by a test: a **repeated** `PUT {is_released:true}` on an already-released box is an idempotent no-op 200 (standard PUT semantics), while `POST /release` 409s on a duplicate — it is an explicit action. `test_duplicate_put_release_is_an_idempotent_no_op` asserts no second audit row is written either way.

### Y1 — yard congestion divided by a hardcoded constant

| | |
|---|---|
| **File** | [services/cargo/repository.py:510-530](services/cargo/repository.py#L510-L530) |
| **Change** | New `yard_block_capacity()` reads `core.yard_block`. Fail-soft: an un-migrated database returns `{}` rather than breaking an endpoint that worked before the table existed. |
| **File** | [services/cargo/service.py:600-670](services/cargo/service.py#L600-L670) |
| **Change** | `optimize_yard` divides by **real** capacity when the master answers. Every zone that has to fall back to the nominal 10 is named in an `assumptions` array in the response. Busiest block is now the most *saturated* zone (a 9/10 block beats a 12/500 one), and only the overflow above capacity is recommended for a move. |
| **File** | [gateway/routers/cargo.py:471-495](gateway/routers/cargo.py#L471-L495) |
| **Change** | `YardOptimizationOut` gains `occupied`, `capacity`, `capacity_source`, `block_utilisation`, `assumptions` — all optional, so the pre-0130 response shape stays valid. |
| **Tests** | `test_yard_capacity_uses_the_real_master_when_present`, `test_yard_capacity_declares_the_assumption_when_the_master_is_empty`, `test_yard_optimization_recommends_moving_the_overflow`, `test_yard_optimization_empty_yard_is_unchanged` |

### PENDENCY — the missing 11th lifecycle step

| | |
|---|---|
| **Files** | [services/cargo/service.py:74-100](services/cargo/service.py#L74-L100) (state + rank 15), [:432-448](services/cargo/service.py#L432-L448) (`record_pendency`), [gateway/routers/cargo.py:1105-1128](gateway/routers/cargo.py#L1105-L1128) (endpoint) |
| **Change** | `VESSEL_DISCHARGED → PENDENCY → YARD_ASSIGNED`, with a new `cargo.pendency_recorded` event distinct from the notification-driven `cargo.pendency_created`. |
| **Design call** | **PENDENCY is OPTIONAL, not a mandatory gate.** Making it mandatory would invalidate every container already recorded as `VESSEL_DISCHARGED → YARD_ASSIGNED` and break the existing demo path — a removal of working behaviour, which was out of scope. `assign_yard` now accepts `PENDENCY` as a predecessor. |
| **Tests** | 6, including `test_pendency_is_optional_so_the_legacy_path_still_works` and `test_pendency_state_machine_ranking` |

### G1 — EIR had no date window

| | |
|---|---|
| **Files** | [services/gate_documents/repository.py:51-58, 391-420](services/gate_documents/repository.py#L391-L420), [services/gate_documents/service.py:151-166](services/gate_documents/service.py#L151-L166), [gateway/routers/gate_documents.py:151-215](gateway/routers/gate_documents.py#L151-L215) |
| **Change** | `from_date` / `to_date` on `GET /api/gate-docs/eir`, a new `TIME_COL` map (EIR→`truck_in_time`, PIN→`issued_at`, FORM13→`captured_at`), and a new `GET /api/gate-docs/eir/profile?group_by=hour\|day` sharing the same `_doc_where` clause so a profile and the rows behind it can never disagree. 92-day window cap. |
| **Why it mattered** | Notice §1.d requires citing the API queries a figure rests on. Without a date filter, any answer about 1–3 August could only come from raw SQL outside the API. |

---

## New APIs

| Endpoint | Purpose |
|---|---|
| `GET /api/cargo/simulate/scenarios` | Catalog: each scenario's JNPA reference, the question it answers, its parameters, and the tables it reads — plus the response contract itself |
| `POST /api/cargo/simulate/berth-cascade` | **I-B** — an overrun of N hours → which calls are displaced, by how long, cumulative delay over 48h |
| `POST /api/cargo/simulate/crane-productivity` | **II-B** — gross moves/hour per call, a 25% cut, turnaround increase + berth-queue impact |
| `POST /api/cargo/simulate/modal-shift` | **II-A** — 20% rail→road, hourly gate profile before/after, first constraint to saturate |
| `POST /api/cargo/simulate/gate-slotting` | **III-A** — arrival pattern, saturated periods, slotting proposal + quantified peak flattening |
| `POST /api/cargo/simulate/driver-shortage` | **III-B** — trips/vehicle cut by a third → throughput, transporter + cargo-flow exposure, state on the report date |
| `POST /api/cargo/simulate/{scenario}` | Generic registry passthrough with ISO-8601 coercion |
| `GET /api/gate/hourly-profile` | Hourly (or daily) truck arrivals over **any** historical window — the endpoint the audit found missing |
| `POST /api/cargo/{cn}/pendency` | Record a discharged container as pending evacuation |
| `GET /api/gate-docs/eir/profile` | EIR gate-arrival counts bucketed by hour or day |
| `GET /api/gate-docs/eir?from_date=&to_date=` | Date-windowed EIR listing |

**RBAC: no new rules, and none needed.** `/api/cargo` writes are already restricted to control-room + customs by the method overlay in `gateway/auth.py:244`; `/api/gate/` is already control-room + customs (`auth.py:167`). The simulate endpoints inherit both. They are POSTs because the parameter set is a body — **not** because they mutate anything.

**Route ordering** — the simulate paths carry two segments after the prefix so `GET /api/cargo/{container_number}` (single segment) cannot capture them. The router is nonetheless registered *before* `cargo.router` in [gateway/main.py](gateway/main.py) so the ordering is explicit rather than incidental, and `test_simulate_routes_do_not_shadow_the_container_lookup` pins it.

---

## New Database Tables

| Migration | Object | Purpose |
|---|---|---|
| `0128_evacuation_mode.sql` | `core.cargo.evacuation_mode` + `evacuation_mode_source` | Per-container RAIL/ROAD/UNKNOWN attribution — II-A's premise was unaddressable without it. Backfilled weakest-evidence-first from LDB movements → job assignments → rake plans; anything left is honestly `UNKNOWN`, **not** defaulted to ROAD. `evacuation_mode_source` records provenance so a derived label is never presented as measured. |
| `0129_vessel_call_moves.sql` | `core.vessel_call_moves` | The missing **numerator** for II-B. `gross_moves` is a generated column; `data_origin` ∈ API/MANUAL/DERIVED. DERIVED rows are counted from `core.edi_vessel_container` per VCN. |
| `0130_yard_block.sql` | `core.yard_block` | Real per-block capacity (`capacity_teus`, `reefer_capacity`, `block_type`). **Seeds nothing** — the JNPA yard layout is not in this database, and an empty master is honest where a fabricated one would not be. |
| `0131_cargo_pendency_state.sql` | widened `cargo_lifecycle_status_check` + partial index | Adds `PENDENCY`. The CHECK is **widened** — every previously-legal value stays legal, so no existing row can be invalidated. |

**One correction made during implementation.** My first draft of `0129` joined `core.edi_vessel_container` to `core.berthing_record` on vessel name and voyage. Checking the actual DDL, the EDI table carries **neither** — it identifies a call only by `VCN`, and `berthing_record` carries no VCN. There is no reliable join between them in this database. Rather than invent one, the table now accepts **either** identity (VCN, or the berthing natural key), the service resolves in the order `berthing_record_id → vcn → (terminal, voyage, vessel)`, and a call matching none of them is reported as *"productivity not derivable"*.

---

## Scenario Coverage Matrix

| Scenario | Before | After |
|---|---|---|
| **I-A** Vessel Bunching | Substrate only; no costing function | **Costing function delivered.** `berth-cascade` answers "what would an alternative order cost against the same objective". Deliberately *not* auto-registered as I-A: the Notice leaves the objective to the bidder ("waiting time, total moves handled, line priority, or another basis"), and the backend must not choose it. |
| **I-B** Extended Berth Window | Timestamps only; no cascade calculator | **Answerable.** `POST /api/cargo/simulate/berth-cascade`. Per-berth exclusivity cascade, 48h horizon, per-call and cumulative delay. |
| **II-A** Rail→Road Modal Shift | No per-container mode; no historical hourly profile; no capacity comparison | **Answerable.** `evacuation_mode` (0128) + `mart`-free hourly profile over any window + TEU→trip conversion **derived from the same window** + first-constraint identification. |
| **II-B** Equipment Availability | **Numerator absent from the entire schema** | **Answerable, with declared provenance.** `core.vessel_call_moves` (0129). `moves/hour → −25% → new_hours = hours/(1−r) → cascade`. DERIVED counts are flagged per call and scenario-wide. |
| **III-A** Gate Approach Congestion | Live 24h view only (`mart.v_gate_throughput` pinned to `now()−24h`) | **Answerable.** Date-ranged profile, 4-tier sustained-rate derivation, saturated periods, forward-spill slotting with quantified peak reduction. |
| **III-B** Driver Shortage | Real trip data, no model | **Answerable.** Trips/vehicle/day from `core.eir`, floor-rounded reduction, throughput delta, dual exposure ranking (absolute + structural), backlog projected onto the state date, explicit evacuation-priority rule. |

**Before: 0 of 6 answerable end-to-end. After: 5 of 6 answerable end-to-end, with I-A's costing function supplied and its objective left where it belongs.**

---

## The Notice §1 contract, enforced in code

Every scenario returns the same envelope, and it is not optional — `SimulationResult` in [services/cargo/simulation/base.py](services/cargo/simulation/base.py) *is* the contract:

```json
{ "scenario": …, "method": "…reproducible prose…",
  "result": {…}, "figures": {…},
  "assumptions": [{"field":…, "value":…, "reason":…, "source":"MEASURED|DERIVED|ASSUMED|PARAMETER"}],
  "queries":     [{"purpose":…, "sql":…, "params":{…}, "api":…, "row_count":…}],
  "recommendations": […], "data_available": true, "notes": […] }
```

`test_every_scenario_returns_the_jnpa_contract` runs all five and asserts every key is present and every scenario published at least one query trace — **even when the data is empty**.

### Three rules that are enforced, not documented

1. **Read-only.** `SimulationRepository._read` refuses anything not starting `SELECT`/`WITH`, refuses a write verb anywhere (catching `WITH x AS (DELETE … RETURNING *)`), and only ever opens `engine.connect()` — never `begin()`, so there is no transaction to commit. Four tests, including one that walks *every* SQL constant on the class and one that greps the module for `.begin()`.
2. **Never fabricate.** Missing data returns `data_available: false` with a note naming the empty table. Five tests assert this per scenario — `test_crane_productivity_without_move_counts_reports_not_derivable` is the sharpest: no numerator, no productivity, and it says so instead of substituting a fleet average.
3. **Deterministic.** No clock, no randomness inside any calculation.

**One implementation detail worth flagging:** `percentile()` uses half-up rounding, not Python's `round`. `round(4.5) == 4` (banker's rounding) would have made the p90 of a six-hour window silently pick the fifth value instead of the sixth. The figure ends up in a JNPA answer, so the rule had to be the obvious one — [gate_slotting.py:38-50](services/cargo/simulation/gate_slotting.py#L38-L50), pinned by `test_percentile_is_nearest_rank_and_deterministic`.

---

## Remaining Risks

**P0 — data volume is still unverified.** The database is not reachable from this host (`.env:15` → `postgres:5432`, a docker-internal name). Every scenario is proven correct against fixtures; none has been run against real JNPA rows. **Before the demo, confirm row counts** in `core.eir`, `core.berthing_record`, `core.perf_daily_traffic`, `core.edi_vessel_container` and `core.cargo` for 1–6 August 2026. A correct engine over an empty table returns `data_available: false` — honest, but not a demo.

**P0 — migrations 0128–0131 have not been executed.** They parse and are discovered by `scripts/migrate.py`, but no run has happened. `0129`'s DERIVED backfill in particular is only as good as the EDI manifest volume.

**P1 — `core.yard_block` is empty by design.** Until the JNPA yard layout is loaded, every congestion figure carries an `ASSUMED` capacity entry. That is defensible under Notice §1.c and it is visible in the payload — but a seeded master is better than a declared assumption.

**P1 — II-B move counts are DERIVED.** A manifest line count excludes restows and anything handled outside the manifest, so the productivity figure is a **lower bound**. Flagged per call and scenario-wide. If JNPA can supply real move counts, insert them with `data_origin='API'` and the flag disappears on its own.

**P1 — the gate sustained rate is inferred unless TAS slots are provisioned.** The fallback chain ends at "p90 of observed arrivals", which understates capacity if the gate was never saturated in the window. The response says exactly this. Provisioning `core.tas_appointment` for the demo window upgrades it from DERIVED to MEASURED.

**P2 — unresolved from the original audit, out of this scope:** cargo *reads* still fall through to any authenticated role (`_POLICY` has no `/api/cargo` entry); `apply_workflow` still accepts APPROVE after REJECT (W4); the two reefer allocators still do not reconcile; `plan_rake` is still N+1; event tables still have no retention policy; `core.transporter` still has no migration (created at gateway boot by `uc3_ext.py`).

**P2 — cross-berth conflicts.** The cascade schedules per berth. Calls at other berths are marked `cross_berth` and passed through — the model does not reassign a displaced vessel to a free berth. That is a *recommendation* the scenario emits, not something it simulates.

---

## Production Readiness Score

| Dimension | Before | After | What moved it |
|---|---|---|---|
| Architecture | 8/10 | **9/10** | Simulation layer follows the established pattern exactly; read-only enforced structurally. Runtime DDL in `gateway/*_ext.py` still bypasses migrations. |
| API | 7/10 | **9/10** | 11 new endpoints; the analytics gap is closed; date-windowed EIR. |
| Database | 6/10 | **8/10** | 4 migrations supply every missing column the Notice needs. Not yet executed; `yard_block` unseeded. |
| Workflow | 8/10 | **9/10** | W1/W2/W3 closed, PENDENCY added, atomicity and locking now proven by test. W4 remains. |
| **What-if** | **2/10** | **9/10** | 0 → 5 scenarios answerable, with an enforced evidence contract. Held back from 10 by unverified data volume. |
| Performance | 6/10 | **6/10** | Unchanged — the simulation layer is read-only and well-indexed, but `plan_rake` N+1 and the unbounded `list_yarded_containers()` remain. |
| Security | 7/10 | **7/10** | Unchanged, deliberately. New endpoints inherit existing RBAC; cargo reads are still broadly visible. |
| Testing | 7/10 | **9/10** | +80 tests. Every scenario has a hand-computed expectation, a missing-data case, and an assumption-declaration case; plus a genuine concurrency test and four architectural invariants. |

---

## Final Verdict

# READY FOR JNPA DEMO — conditional on executing the migrations and verifying data volume.

**What is now true that was not.** All three critical lifecycle defects are closed, each with a test that fails without the fix — including a real `asyncio.gather` race for W3 rather than a serialised approximation. Five of the six Notice scenarios are answerable end-to-end from the API, each returning method, figures, assumptions and the SQL behind them. The sixth (I-A) has its costing function delivered; only the objective choice remains, and that is the bidder's to state.

**What stands between here and the demo** is two commands and a count, not more engineering:

1. `python scripts/migrate.py --target 0131` against the demo database.
2. Confirm rows exist for 1–6 August 2026 in `core.eir`, `core.berthing_record`, `core.perf_daily_traffic` and `core.edi_vessel_container`.

If (2) comes back thin, the scenarios will say so rather than fabricate — which is the correct behaviour and a poor demo. **Check the counts before you present, not during.**

**The honest caveat to carry into the room.** Two figures in the II-A/II-B answers are derived proxies, not measurements: manifest line counts standing in for crane moves, and a TEU-per-trip ratio inferred from the same window. Both are declared in every response that uses them. The Notice states plainly that "an assumption declared openly will be treated more favourably than a figure presented without one" — that clause is working in your favour here. Lead with the declaration rather than waiting to be asked.
