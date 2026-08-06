# UC-3 Cargo Backend Audit Report

**Auditor role:** Principal Backend Architect / JNPA Digital Twin Technical Auditor
**Date:** 06 August 2026
**Branch audited:** `dev_aniket` @ `f4b3cea`
**Method:** static verification against source. Every claim below cites a file, line, endpoint, table or migration that was read. The database was **not** reachable from the audit host (`.env:15` → `postgres:5432`, a docker-internal name), so **row-count / data-volume claims are explicitly marked UNVERIFIED** rather than assumed.

---

## Overall Verdict

### **NOT READY** — for the What-If scenarios in the 05-Aug-2026 Notice.
### **READY** — for the cargo lifecycle demo (create → discharge → yard → scan → verify → release).

The split matters. The cargo **transactional spine is genuinely well built** — a real state machine, atomic transitions, an append-only audit log, 69 tests. What does not exist, anywhere in the backend, is an **analytical layer**: there is no code that computes a before/after comparison, no crane-productivity derivation, no hourly gate profile over a historical date range, and no simulation service. `services/cargo/simulation/` does not exist. Neither does any equivalent.

The What-If capability that *does* exist (`scenarios/`, `web/src/whatif/`) is a **live-injection demo harness**, not a calculator: `scenarios/tfc1.py:53-90` closes a gate and injects 80 synthetic trucks into the running simulator. It mutates state; it does not answer "what would it cost".

---

## Score

| Dimension | Score | One-line justification |
|---|---|---|
| Architecture | **8/10** | Clean router → service → repository separation, no ORM, no SQL in routers. Marred by runtime DDL in `gateway/*_ext.py` bypassing migrations. |
| API | **7/10** | 24 cargo endpoints, coherent contract, correct route ordering. No analytics/aggregate endpoints at all. |
| Database | **6/10** | Cargo spine + indexes are sound. Missing every column the Notice's scenarios need (moves, crane, evacuation mode, yard capacity). |
| Workflow | **8/10** | Real forward-only state machine with mandatory gates, `FOR UPDATE` locking, atomic audit. Three concrete defects found (below). |
| What-If Capability | **2/10** | No calculation engine exists. Scenario runner is a state mutator, not a simulator. |
| Performance | **6/10** | Fully async, well-indexed, but N+1 writes in rake planning and no cargo-volume load test. |
| Security | **7/10** | JWT + path RBAC + method-scoped write overlay + PII masking + fail-fast prod posture. Cargo reads are open to every authenticated role. |
| Testing | **7/10** | 69 cargo tests incl. real-DB lifecycle. **Zero** tests for any Notice scenario. |

---

## 1. Project Structure Audit

| Folder | Purpose | Status |
|---|---|---|
| `services/cargo/` | Cargo domain: `service.py` (634 L, state machine + orchestration), `repository.py` (700 L, raw SQL) | **Complete** |
| `services/container_job/` | UC-III job spine: assignment, gate/yard/scan events (`service.py` 31 KB, `repository.py` 23 KB) | **Complete** |
| `services/export_lifecycle/` | Export leg: booking → Form-13 → VGM → LEO → loaded | **Complete** |
| `services/cfs_ecy/` | Off-dock CODECO movements + `chain_service.py` (ECY↔CFS chain) | **Complete** |
| `services/rail/` | FOIS / Form-11 / CTO ingest + `repository.py` (27 KB) | **Complete (ingest only)** |
| `services/performance/` | JNPA daily-report + LDB PDF ingest → `core.perf_*` | **Complete (ingest only)** |
| `services/berthing/` | Vessel-call records + lifecycle projection | **Complete** |
| `services/gate_documents/` | EIR / PIN / Form-13 parse + store | **Complete** |
| `services/crosstwin/` | UC-II ↔ UC-III bridge (deferred arrival windows) | **Complete** |
| `services/cargo/simulation/` | **What-if calculation engine** | **DOES NOT EXIST** |
| `gateway/routers/` | 76 routers; `cargo.py` (1215 L) is the cargo surface | **Complete** |
| `gateway/*_ext.py` | 14 modules issuing `CREATE TABLE IF NOT EXISTS` at **boot** | **Architectural risk** |
| `infra/postgres/v3/` | 27 forward migrations `0101`→`0127` + backfills + hotfixes | **Complete** |
| `tests/` | 125 test files; `test_cargo.py` = 1275 L / 69 tests | **Complete for CRUD, absent for scenarios** |
| Docker | `docker-compose.yml` (69 KB, 30 services: postgres, kafka, redis, minio, gateway, scenarios, web…) | **Complete** |
| **"models" / "schemas" folders** | — | **Do not exist by that name.** DTOs are Pydantic v2 classes inline in each router (`gateway/routers/cargo.py:180-350`). This is a deliberate, consistent choice, not a gap. |

**Finding A1 — schema drift risk (P1).** `core.transporter` — referenced as a foreign key by `core.container_job_assignment` (`infra/postgres/v3/0113_container_job.sql`) — has **no `CREATE TABLE` in any migration**. It is created at gateway boot by `gateway/uc3_ext.py:59`. A database migrated with `scripts/migrate.py` alone, without booting the gateway first, will fail `0113`. Thirty-nine tables follow this runtime-DDL pattern.

---

## 2. API Audit

### Cargo domain — `gateway/routers/cargo.py` (prefix `/api/cargo`)

| Endpoint | Method | Purpose | Request | Response | DB | Status |
|---|---|---|---|---|---|---|
| `/api/cargo` | POST | Create cargo | `CargoCreate` (ISO-6346 validated) | `CargoOut` 201 / 409 | `core.cargo` + `core.cargo_lifecycle_event` | ⚠️ **Defect — see W1** |
| `/api/cargo` | GET | List + filter + paginate + role scope | 11 query filters | `CargoOut[]` + `X-Total-Count` | `core.cargo` | ✅ Complete |
| `/api/cargo/events` | GET | Poll lifecycle events (`since` cursor) | `container_number, event, since, limit` | `CargoEventOut[]` | `core.cargo_event` | ✅ Complete |
| `/api/cargo/{cn}` | GET | One container | path | `CargoOut` / 404 | `core.cargo` | ✅ Complete |
| `/api/cargo/{cn}` | PUT | Patch writable columns | `CargoUpdate` | `CargoOut` | `core.cargo` | ✅ Complete (VERIFY gate enforced, `service.py:337`) |
| `/api/cargo/{cn}` | DELETE | Remove | path | `{deleted}` / 404 | `core.cargo` | ✅ Complete |
| `/api/cargo/{cn}/discharge` | POST | CREATED → VESSEL_DISCHARGED | `{vessel_name, discharge_time}` | `DischargeOut` / 409 | `core.cargo` (+ audit) | ✅ Complete |
| `/api/cargo/{cn}/yard-assignment` | PUT | Assign yard block | `{yard_block}` | `YardAssignmentOut` | `core.cargo` | ✅ Complete |
| `/api/cargo/{cn}/yard-position` | POST | Allocate block/row/slot/position | `{yard_block,row,slot,position,priority}` | `YardPositionOut` | `core.cargo_yard_plan` | ✅ Complete |
| `/api/cargo/{cn}/verify` | POST | Customs/scan verification | `{verified, remarks}` | `VerifyOut` / 409 | `core.cargo_scan_verification` | ✅ Complete |
| `/api/cargo/{cn}/release` | POST | Gated release (requires VERIFIED) | `{note}` | `ReleaseOut` / 409 | `core.cargo` | ⚠️ **Defect — see W2** |
| `/api/cargo/{cn}/lifecycle` | GET | Append-only transition history | paginated | `LifecycleHistoryOut[]` | `core.cargo_lifecycle_event` | ✅ Complete |
| `/api/cargo/scan-queue` | GET | Boxes awaiting scan | `limit/offset` | `ScanQueueItemOut[]` | `core.cargo` (derived) | ✅ Complete |
| `/api/cargo/{cn}/workflow` | POST | TRIGGER / APPROVE / REJECT | `{action, comment}` | `WorkflowOut` | `core.cargo_workflow_event` | ✅ Complete |
| `/api/cargo/{cn}/workflow/history` | GET | Workflow audit | paginated | list | `core.cargo_workflow_event` | ✅ Complete |
| `/api/cargo/notifications` | POST / GET | Stakeholder notifications | `{type,severity,message,stakeholders}` | `NotificationOut` | `core.cargo_notification` | ✅ Complete |
| `/api/cargo/yard-planning` | POST | Allocate next free slot in block | `{preferred_block, priority}` | `YardPlanOut` | `core.cargo_yard_plan` | ✅ Complete |
| `/api/cargo/yard-optimization` | GET | Congestion score + move recs | — | `{yard_congestion, recommendations}` | `core.cargo` | ⚠️ **Hardcoded capacity — see Y1** |
| `/api/cargo/rake-planning` | POST / GET | Rail rake grouping | `{rake_id, containers[]}` | `RakePlanOut` | `core.cargo_rake_plan` | ⚠️ **N+1 — see P1** |
| `/api/cargo/reefer-planning` | POST | Allocate powered reefer slot | `{temperature, power_required}` | `ReeferPlanOut` | `core.cargo_reefer_plan` | ✅ Complete |
| `/api/cargo/{cn}/shipping-line` | GET | Read-only IAL/EAL enrichment | path | line details | `mart.v_shipping_line_container` | ✅ Complete |

### Adjacent surfaces that the cargo lifecycle depends on

| Endpoint family | Router | Status |
|---|---|---|
| `/api/jobs`, `/api/gate/events`, `/api/yard/movements`, `/api/scan/events`, `/api/scan/status/{cn}` | `container_job.py` (17 routes) | ✅ Complete |
| `/api/export/*` | `export_lifecycle.py` | ✅ Complete |
| `/api/gate-docs/eir|pin|form13|tat|container|truck` | `gate_documents.py` (13 routes) | ✅ Complete — ⚠️ **no date filter, see G1** |
| `/api/rail/summary|fois|form11|cto|container/{n}` | `rail.py` (6 routes) | ✅ Read-only |
| `/api/reefer/slots|availability|allocate|release` | `reefer.py` (5 routes) | ✅ Complete, real SQL occupancy |
| `/api/performance/*` (16 routes incl. `/routes` modal share, `/dwell`, `/congestion`) | `performance.py` | ✅ Complete |
| `/api/kpi/{view}` — 10 whitelisted mart views | `kpi.py` | ✅ Complete |
| `/api/bottlenecks` | `bottlenecks.py` | ✅ Complete (corridor, not gate) |
| `/api/scenarios/{name}/run|reset`, `/api/scenario_step` | `scenarios.py`, `scenario_ext.py` | ✅ Complete (**injection**, not simulation) |
| **Any what-if / simulate / project endpoint** | — | ❌ **DOES NOT EXIST** |

**Finding G1 (P0 for Notice).** `GET /api/gate-docs/eir` (`gateway/routers/gate_documents.py:151-166`) accepts only `container`, `truck`, `terminal`, `limit≤500`, `offset`. There is **no `from`/`to` date filter and no aggregation**. `core.eir` carries `truck_in_time` / `truck_out_time` / `company` / `terminal` — exactly the arrival-time substrate scenario III-A asks for — but the API cannot select a date range or bucket by hour. Answering III-A today requires raw SQL outside the API surface, which fails the Notice's requirement (§1.d) to cite *the API queries used*.

---

## 3. Database Audit

### Cargo tables (all in `core` schema, migration `0101_core_operational_ext.sql` unless noted)

| Table | Purpose | Key columns | Relationships | Indexes |
|---|---|---|---|---|
| `core.cargo` | The container record (PK = ISO-6346) | `container_number` PK, `vessel_name`, `customs_status`, `yard_block`, `is_released`, `vehicle_number`, `gate`, `camera_id`, `eta`, `eseal_*`, `pre_document_status`, `origin_stream`, `workflow_status`, `lifecycle_status`, `direction` (0115), `source_igm_no` (0115) | Soft (by-value) to every child on `container_number` | 9 btree: customs_status, eseal_status, eta DESC, is_released, lifecycle_status, origin_stream, pre_document_status, vehicle (partial), yard_block; + `(direction, lifecycle_status)` and partial `source_igm_no` (0115) |
| `core.cargo_lifecycle_event` | **Append-only transition audit** | `action, old_status, new_status, actor_role, note` | by container | container, `id DESC` |
| `core.cargo_event` | Notification/event log (`payload jsonb`) | `event, payload` | by container | container, event, `id DESC` |
| `core.cargo_workflow_event` | TRIGGER/APPROVE/REJECT log | CHECK on action | by container | container, `id DESC` |
| `core.cargo_yard_plan` | Yard slot + physical position | `preferred_block, assigned_block, priority, yard_row, yard_slot, yard_position` | by container | assigned_block, container, `id DESC` |
| `core.cargo_rake_plan` | Rail rake grouping | `rake_id, containers jsonb, planned_containers` | jsonb array — **no FK, not joinable** | rake_id, `id DESC` |
| `core.cargo_reefer_plan` | Reefer allocation | `temperature, power_required, slot` | by container | container, `id DESC` |
| `core.cargo_scan_verification` | Scan/customs verification records | `verified, remarks, actor_role` | by container | container, `id DESC` |
| `core.cargo_notification` | Stakeholder notifications | `type, severity, stakeholders jsonb, status` | by container | 5 indexes |
| `core.cargo_movement_event` (0113) | Yard pickup/drop/move | `movement_type, vehicle_id, driver_id, yard_location, occurred_at` | FK → `container_job_assignment` | 4 indexes incl. `(container_number, occurred_at DESC)` |
| `core.container_job_assignment` (0113) | Truck/driver job spine | `move_type, document_type, status`, 8-state CHECK | FK → `core.transporter` | — |
| `core.scan_event` (0113) | Scanner results | `machine_code, result, scanned_at` | FK → `scanner_machine`, job | — |
| `core.export_booking` (0115) | Export leg documentary facts | `booking_no, form13_no, pod, via_no` | by container | — |
| `core.eir` (0112) | **Real JNPA gate documents** | `truck_in_time, truck_out_time, tat_minutes` (GENERATED), `company`, `terminal`, `container_number`, `gross_weight_mt` | by container/truck | — |

### Supporting analytical tables (ingested from JNPA daily reports)

| Table | Carries | Grain |
|---|---|---|
| `core.perf_daily_traffic` | `vessels, imp_teus, exp_teus, total_teus, rakes, rail_dis_teus, rail_ldg_teus, rail_total_teus` | **day × terminal** |
| `core.perf_daily_terminal_status` | `yard_*_teus, yard_usable_capacity_teus, yard_occupancy_pct, gate_in_teus, gate_out_teus, reefer_total/occupied/available_slots, icd_pendency_teus, cfs_pendency_teus` | **day × terminal** |
| `core.perf_daily_vessel` | `berth_no, via_no, vessel_name, berthed_on, expected_completion` | day × terminal × berth |
| `core.perf_ldb_route_movement` | `transport_mode (TRAIN\|TRUCK), route_name, pct_share` | **month** |
| `core.berthing_record` | `eta, ata, berthing_time, departure_time, cargo_operation_start, cargo_operation_end` | vessel call |

### Missing fields, by scenario requirement

| Requirement | Verdict | Evidence |
|---|---|---|
| **Vessel operations** | ⚠️ Partial | `berthing_record` has operation start/end timestamps → **hours worked is derivable**. But **no move count anywhere**. `perf_daily_vessel` has no `moves`, `containers_handled`, or `teus` column. |
| **Equipment / crane** | ❌ **Missing entirely** | Grep for `crane`, `gross_moves`, `moves_per_hour`, `gmph`, `equipment` across the whole repo returns **zero** hits. No `core.crane`, no `core.equipment`, no `core.vessel_move`. |
| **Rail movement** | ⚠️ Partial | `perf_daily_traffic.rail_total_teus` (daily aggregate) + `core.fois_train_intimation` / `core.cto_manifest_entry` (rake manifests). But `core.cargo` has **no `evacuation_mode` column** — an individual container cannot be tagged RAIL vs ROAD. `cargo_rake_plan.containers` is an unjoinable jsonb array. |
| **Road evacuation** | ⚠️ Partial | `core.eir` (truck in/out + company), `core.tt_trip` (per-vehicle trip sequence), `core.trt_record` (gate→park→load→out phases). Good substrate; **no per-container mode attribution**. |
| **Yard congestion** | ⚠️ Partial | `perf_daily_terminal_status.yard_occupancy_pct` is real, daily, terminal-level. Per-block capacity is a **hardcoded constant** (`services/cargo/service.py:132`, `_YARD_BLOCK_CAPACITY = 10`). No `core.yard_block` capacity master. |
| **Gate impact** | ⚠️ Partial | `core.gate_event` (4 event types) + `mart.v_gate_queue_wait` / `v_gate_txn_time` / `v_tat_inside_port` + `core.tas_appointment` (`capacity`, `booked`). **But** `mart.v_gate_throughput` is hardcoded `WHERE ts > now() - '24:00:00'` (`0103_mart_views.sql:62`) — it **cannot be queried for 1–3 August**. |
| **Equipment productivity** | ❌ **Missing** | No numerator (moves). See above. |

---

## 4. Workflow State Machine Audit

### Is the cargo lifecycle production ready? — **YES, with three defects to fix.**

**State definitions** — `services/cargo/service.py:74-94`. Nine import states with integer ranks; seven export states added by `0115`. Mirrored as a DB `CHECK` constraint (`0115_lifecycle_completion.sql`), so the DB independently rejects an illegal value.

**Transition rules** — `can_transition()` (`service.py:107-115`): strictly forward (`tr > cr`) and may not skip a **mandatory gate** (`_MANDATORY_STATES` = CREATED, VESSEL_DISCHARGED, YARD_ASSIGNED, VERIFIED, RELEASED). Optional planning states (position/reefer/rake/scan-pending) are skippable. This is a correct, minimal, well-chosen model.

**Invalid transition handling** — `CargoTransitionError` (`repository.py:116-128`) carries `current` + `target`; the router renders a precise 409 with `{error:"illegal_transition", current_status, attempted_status}`.

**Transaction safety** — `transition_lifecycle()` (`repository.py:534-589`) takes `SELECT … FOR UPDATE` on the cargo row, then does the `UPDATE` **and** the audit `INSERT` inside one `engine.begin()`. State and audit trail cannot diverge. `create()` (`repository.py:194-222`) writes the opening `NULL → CREATED` audit row in the same transaction as the INSERT. `record_workflow()` also locks `FOR UPDATE`. This is genuinely correct.

**Audit logging / history** — one writer, `record_lifecycle_event()` (`repository.py:166-192`), enforced by a test that greps the repository source for competing writers (`tests/test_cargo.py:1216`). Exposed via `GET /api/cargo/{cn}/lifecycle`.

### Defects found

**W1 — `POST /api/cargo` can create an already-released container (P0).**
`CargoCreate.is_released: bool = Field(default=False)` (`gateway/routers/cargo.py:185`) **accepts `true`**. `CargoService.create_cargo` (`service.py:253-270`) applies no gate. `CargoRepository.create` lists `is_released` in `_WRITABLE` (`repository.py:52`) and writes it straight through, while `lifecycle_status` takes the DB default `'CREATED'`.

Result: `POST /api/cargo {"container_number":"…","is_released":true}` produces `is_released=true, lifecycle_status='CREATED'` — a box released without ever being discharged, yarded, scanned or verified. This is **precisely the corruption migration `0115` was written to clean up**; its header states *"the service now refuses the same input"* (`0115_lifecycle_completion.sql:22`). **It does not.** The PUT path was fixed (`service.py:337-341`); the POST path was not. No test covers it.

**W2 — release is not atomic (P1).**
`release_cargo()` (`service.py:490-511`) calls `_advance(...)` — which commits transaction #1 (status → RELEASED + audit row) — and *then* `self._repo.update(cn, {"is_released": True})` in transaction #2. A crash, connection drop, or pool timeout between them leaves `lifecycle_status='RELEASED'` with `is_released=false`: the row is released to the state machine but invisible to every legacy `is_released` filter, including the driver role scope (`service.py:143`).

**W3 — TOCTOU on the PUT release gate (P1).**
`update_cargo()` (`service.py:328-343`) does `get()` → evaluate `can_transition` → `update()`, with **no row lock** between the read and the write. Two concurrent `PUT {is_released:true}` calls can both read a pre-release snapshot, both pass the gate, and both write. `transition_lifecycle` would then reject the second `_advance`, but the second `is_released` write has already landed — same divergence as W2.

**W4 — `apply_workflow` has no transition validation (P2).**
`WORKFLOW_TRANSITIONS` (`service.py:124-128`) is a flat action→status map. `APPROVE` is accepted from any state, including `REJECTED`, and `TRIGGER` can follow `APPROVED`. Unlike the lifecycle machine, there is no predecessor set. The DB CHECK constrains the *action* vocabulary only, not the ordering.

### Lifecycle coverage vs. your stated expectation

| Expected step | Exists? | Evidence |
|---|---|---|
| Cargo Creation | ✅ | `POST /api/cargo` → `LC_CREATED` |
| Movement | ✅ | `core.cargo_movement_event` (0113) + `POST /api/yard/movements`; also `core.container_movement_history` |
| Vessel Discharge | ✅ | `POST /{cn}/discharge` → `LC_VESSEL_DISCHARGED` |
| **Pendency** | ⚠️ **Not a lifecycle state** | Only exists as `perf_daily_terminal_status.icd_pendency_teus` / `cfs_pendency_teus` (a daily aggregate from JNPA reports) and as an *event name* `cargo.pendency_created` fired by the notification path (`service.py:529`). There is no `PENDENCY` state and no per-container pendency computation. |
| Yard Assignment | ✅ | `PUT /{cn}/yard-assignment` → `LC_YARD_ASSIGNED` |
| Yard Position Allocation | ✅ | `POST /{cn}/yard-position` → `LC_YARD_POSITION_ALLOCATED` |
| Reefer Planning | ✅ | `POST /reefer-planning` → `LC_REEFER_PLANNED` (+ real slot inventory at `/api/reefer/*`) |
| Rail Planning | ✅ | `POST /rake-planning` → `LC_RAKE_ASSIGNED` |
| Scan Queue | ✅ | `GET /scan-queue` — **derived**, not a stored state (`repository.py:603-620`) |
| Verification | ✅ | `POST /{cn}/verify` → `LC_VERIFIED` |
| Release | ✅ | `POST /{cn}/release` → `LC_RELEASED` (gated on VERIFIED) |

**Verdict: 10 of 11 steps exist in code. Pendency is the gap.**

---

## 5. JNPA What-If Scenario Audit

### Scenario II-A — Rail to Road Modal Shift (1–3 Aug 2026)

> *20% of containers currently evacuated by rail move to road. Determine whether the gate absorbs the additional load. Present the hourly gate profile before and after, and identify the first constraint to saturate.*

| Capability | Verdict | Evidence |
|---|---|---|
| Transport mode | ❌ **Missing** | `core.cargo` has no `evacuation_mode` / `transport_mode` column. `perf_ldb_route_movement.transport_mode` exists but is **monthly percentage share**, not per-container. |
| Rail containers | ⚠️ Partial | `perf_daily_traffic.rail_total_teus` + `rakes` (daily, per terminal); `core.cto_manifest_entry` / `core.form11_entry` (rake manifests). Not linkable to `core.cargo` rows. |
| Road containers | ⚠️ Partial | `core.eir` (real truck in/out), `core.gate_event`, `core.tt_trip`. Not linkable to a rail/road decision. |
| Evacuation history | ⚠️ Partial | `core.ldb_movement.mode` exists; `core.cargo_lifecycle_event` records the state chain but no mode. |
| Gate impact calculation | ❌ **Missing** | No code computes gate capacity vs. offered load. `core.tas_appointment` has `capacity` and `booked` per slot — the ingredients, never combined. |
| Hourly profile | ❌ **Missing** | `mart.v_gate_throughput` buckets hourly but is **pinned to `now() - 24h`** (`0103_mart_views.sql:62`) — it cannot address 1–3 August. `core.eir` has the timestamps; no endpoint exposes a date range or hourly bucket (Finding G1). |

**Supported:** raw substrate (EIR gate timestamps, daily rail TEU totals, TAS slot capacity).
**Partial:** rail volumes at daily/terminal grain only.
**Missing:** per-container evacuation mode; any hourly profile over a historical window; any capacity-vs-load comparison; any before/after machinery.

**Required code changes**

1. **Migration `0128_evacuation_mode.sql`**
   `ALTER TABLE core.cargo ADD COLUMN evacuation_mode text CHECK (evacuation_mode IN ('RAIL','ROAD','UNKNOWN'));`
   Backfill: `RAIL` where a `cargo_rake_plan.containers` jsonb entry matches; `ROAD` where a `container_job_assignment` with `move_type='IMPORT_PICK'` exists; else `UNKNOWN`. Index `(evacuation_mode, lifecycle_status)`.
2. **New parameterised view** `mart.v_gate_hourly_profile(from_ts, to_ts)` — replace the `now()-24h` pin. Source `core.eir.truck_in_time` (real data) with `core.gate_event` as fallback; group by `date_trunc('hour', …)` and `terminal`.
3. **New service** `services/cargo/simulation/modal_shift.py` implementing
   `simulate(from_date, to_date, shift_pct) -> {baseline_profile[], shifted_profile[], gate_capacity_per_hour, saturated_hours[], first_constraint}`.
   Gate sustained rate must be **derived**, not assumed: `count(gate_event WHERE event_type='GATE_IN') / hours` at the observed peak, or `tas_appointment.capacity` summed per hour — and whichever you choose must be declared as an assumption per Notice §1.c.
4. **New endpoints** `POST /api/cargo/simulate/modal-shift` and `GET /api/gate/hourly-profile?from=&to=&terminal=`.
5. **Extend** `GET /api/gate-docs/eir` with `from` / `to` / `group_by=hour`.

---

### Scenario II-B — Equipment Availability (6 Aug 2026)

> *Derive effective crane productivity per vessel call as gross moves per hour worked. Model a 25% reduction for one call; state the effect on turnaround and the berth queue behind it.*

| Required input | Verdict | Evidence |
|---|---|---|
| Vessel moves (**the numerator**) | ❌ **Missing entirely** | No table, no column, no parser anywhere in the repo produces a move/box count per vessel call. `perf_daily_vessel` columns: `report_date, terminal_code, berth_no, via_no, vessel_name, cargo_commodity, berthed_on, expected_completion`. `core.berthing_record` has no volume column either. |
| Equipment / crane registry | ❌ **Missing** | Zero occurrences of `crane` in the codebase. |
| Crane assignment per call | ❌ **Missing** | — |
| Operation hours (**the denominator**) | ✅ **Available** | `core.berthing_record.cargo_operation_start` / `cargo_operation_end` → hours worked is a direct subtraction. |
| Productivity calculation | ❌ **Missing** | No function computes `moves / hours` anywhere. |
| Berth queue model | ⚠️ Partial | `services/berthing/lifecycle.py` + `core.berthing_record` (eta/ata/berthing_time/departure_time) give the queue *sequence*; there is no cascade/displacement calculator. |

**Supported:** hours worked (denominator), berth queue sequence.
**Partial:** vessel-call identity and timing.
**Missing:** **the numerator.** Without a move count, `Gross Moves / Working Hours` is not computable from this database at any grain.

Can the backend calculate before-productivity, after-25%-reduction, turnaround impact, berth-queue impact, cargo delay? **No — none of the four.**

**Required code changes**

1. **Migration `0129_vessel_moves.sql`**
   ```sql
   CREATE TABLE core.vessel_call_moves (
     id bigserial PRIMARY KEY,
     berthing_record_id bigint REFERENCES core.berthing_record(id),
     terminal text NOT NULL, vessel_name text, voyage_number text, via_no text,
     discharge_moves integer, load_moves integer,
     gross_moves integer GENERATED ALWAYS AS
       (COALESCE(discharge_moves,0) + COALESCE(load_moves,0)) STORED,
     restow_moves integer, cranes_deployed integer,
     data_origin text NOT NULL DEFAULT 'DERIVED',   -- API | MANUAL | DERIVED
     UNIQUE (terminal, voyage_number, vessel_name)
   );
   ```
   Two population paths, both honest: (a) parse move counts from the JNPA daily-report PDFs via `services/performance/pdf_parsers.py` if the column exists there; (b) **derive** `gross_moves` by counting `core.edi_vessel_container` rows per call (migration `0125` already stores EDI vessel-container manifests) and stamp `data_origin='DERIVED'`.
2. **New service** `services/cargo/simulation/crane_productivity.py`:
   - `baseline(call) -> gross_moves / hours_worked` where `hours_worked = cargo_operation_end - cargo_operation_start`
   - `apply_reduction(call, pct) -> new_hours = gross_moves / (rate × (1 − pct))`
   - `cascade(terminal, from_ts, delta_hours) -> [{vessel, original_berthing, new_berthing, delay_hours}]` over the 48 h queue.
3. **New endpoints** `GET /api/berthing/productivity?date=&terminal=` and `POST /api/cargo/simulate/crane-productivity`.
4. **Reuse for I-B** — the `cascade()` function is exactly what Scenario I-B (Extended Berth Window, 2 Aug) needs. Build it once.

---

### Yard Congestion

| Check | Verdict | Evidence |
|---|---|---|
| Yard capacity | ⚠️ Partial | Real at terminal/day grain: `perf_daily_terminal_status.yard_usable_capacity_teus`. **Fake at block grain:** `_YARD_BLOCK_CAPACITY = 10` hardcoded (`services/cargo/service.py:132`). |
| Occupancy | ✅ | `yard_occupancy_pct` (real, daily) + live count from `core.cargo.yard_block`. |
| Slot availability | ⚠️ Partial | `next_yard_slot()` (`repository.py:481-493`) counts existing rows +1 — it **never checks a capacity ceiling**, so it will happily allocate slot 500 in a 40-slot block. |
| Container location | ✅ | `core.cargo_yard_plan.yard_row/yard_slot/yard_position` (0023/0101) + `core.cargo_movement_event.yard_location`. |
| Pending cargo | ✅ | `GET /api/cargo/scan-queue`; `lifecycle_status` filters. |

**Can the system identify congestion? Partially — and the number it reports is not defensible.**
`GET /api/cargo/yard-optimization` returns `congestion = min(1.0, total_containers / (zone_count × 10))` (`service.py:585`). With a made-up denominator, that figure would not survive a JNPA evaluator asking "10 what, and from where?"

**Fix (P1):** add `core.yard_block (terminal, block_code, capacity_teus, reefer_plugs, block_type)` seeded from terminal layout, and rewrite `optimize_yard()` to divide by real capacity. If layout data is unavailable, keep the constant but **surface it in the response** as `{"assumption": "block_capacity=10 (nominal, not from JNPA data)"}` — the Notice explicitly rewards a declared assumption over an undeclared figure (§1.c).

---

### Gate Impact

| Check | Verdict | Evidence |
|---|---|---|
| Cargo release → truck movement | ✅ | `cargo.released` on the bus (`_BUS_EVENTS`, `service.py:59`) → `services/lifecycle_bus.publish` → Kafka `jnpa.uc3.lifecycle` + WebSocket. `core.container_job_assignment` links the released box to a vehicle + driver. |
| Gate load | ⚠️ Partial | `core.gate_event` (GATE_ARRIVAL / TXN_START / GATE_IN / GATE_OUT) + `mart.v_gate_throughput` (**24 h window only**). Real historical load lives in `core.eir` but is not exposed as a profile. |
| Appointment | ✅ | `core.tas_appointment` (`capacity`, `booked`, `window_start/end`) + `core.deferred_arrival_window` (0115, durable) + `/api/tas/slots`, `/api/tas/reschedule`, `/api/tas/deferred-windows`. |
| Queue | ⚠️ Partial | `mart.v_gate_queue_wait` (15-min buckets, avg wait) is **observational**. There is no queueing model — no arrival-rate vs. service-rate comparison, no predicted queue length. |

**Net:** the backend *observes* gate state well and *reacts* well (TFC-1 reroutes trucks). It cannot *predict* gate saturation from a proposed cargo-release plan — which is what II-A and III-A both ask for.

---

### Reefer Handling

| Check | Verdict | Evidence |
|---|---|---|
| Reefer requirement | ✅ | `core.cargo_reefer_plan.power_required` |
| Temperature | ✅ | `cargo_reefer_plan.temperature`; `core.reefer_slot.set_temperature` / `current_temperature` |
| Power availability | ✅ | `core.reefer_slot.powered` + `GET /api/reefer/availability` returns `powered_available` per facility, **computed in SQL** (`gateway/routers/reefer.py:71-119`) |
| Reefer planning | ✅ | `POST /api/cargo/reefer-planning` (→ `LC_REEFER_PLANNED`) and `POST /api/reefer/allocate` (atomic claim of first AVAILABLE+powered slot) |
| Capacity data | ✅ | `perf_daily_terminal_status.reefer_total/occupied/available_slots` — real JNPA figures |

**Reefer is the most complete sub-domain in the audit.** The one wrinkle: two parallel allocators (`cargo_reefer_plan.slot` = `REEFER-A01…` from `next_reefer_index()`, vs. `core.reefer_slot` real inventory) that do not reconcile. `POST /api/cargo/reefer-planning` can hand out a slot that `core.reefer_slot` says is OCCUPIED. **P2 — unify or document.**

---

## 6. What-If Simulation Capability

Required chain: **Scenario Input → Calculation Engine → Impact Prediction → Recommendation**

| Stage | Present? | What actually exists |
|---|---|---|
| Scenario Input | ⚠️ Partial | `POST /api/scenarios/{name}/run` with a `params` dict — but only 4 registered names (`tfc1`, `tfc2`, `tfc3`, `monsoon_friday`), all traffic, none cargo (`scenarios/__init__.py:31-36`). |
| Calculation Engine | ❌ **Absent** | Grep for `simulate` / `simulation` / `forecast` / `projection` across `gateway/`, `services/`, `scenarios/`, `shared/`: the only hits are the traffic ML forecaster (`ai/congestion`, `GET /api/traffic/predict`) and the *marine* projection (a state derivation, not a simulation). No cargo simulation exists. |
| Impact Prediction | ❌ **Absent** | No before/after structure is produced anywhere. |
| Recommendation | ⚠️ Partial | `optimize_yard()` emits `{action:"MOVE", reason:"reduce congestion"}` — the only recommendation generator in the cargo domain, and it rests on the hardcoded capacity. |

**What `scenarios/` really is.** `scenarios/tfc1.py:53-120` closes a gate in `core.gate`, injects 80 synthetic `AT_GATE_QUEUE` trucks into truck-sim, nudges corridor segments, polls the forecaster, and reroutes trucks. It **mutates live state** and has a `reset()`. That is a compelling *demo*, and it is genuinely reactive — but it answers "watch the system respond", not "what would 20% modal shift cost". The Notice asks for the latter, with figures (§1.b) and traceable API queries (§1.d).

### Suggested architecture

```
services/cargo/simulation/
├── __init__.py            # SimulationService — single entry point, mirrors CargoService
├── base.py                # Scenario protocol: baseline() → apply() → impact() → recommend()
│                          # + an Assumption dataclass, so every declared assumption is
│                          #   carried in the response (Notice §1.c is worth marks)
├── repository.py          # read-only aggregate SQL; NEVER writes to core.cargo
├── modal_shift.py         # II-A: rail→road, hourly gate profile before/after
├── crane_productivity.py  # II-B: gross moves/hr, −25%, turnaround + queue cascade
├── berth_cascade.py       # I-B: +6 h overrun → displaced calls over 48 h (reuses cascade)
├── gate_slotting.py       # III-A: arrival pattern → appointment plan → flattened peak
└── driver_shortage.py     # III-B: trips/vehicle × ⅔ → throughput by transporter & flow
```

**Two non-negotiable design rules:**

1. **Read-only.** The simulation repository must never write to `core.cargo`. `scenarios/` mutates live state and needs `reset()`; a what-if answer must be reproducible and must not corrupt the demo database mid-evaluation.
2. **Assumptions are first-class.** Every response returns `{result, figures, assumptions: [{field, assumed_value, reason, source}], queries: [...]}`. The Notice states an openly declared assumption is treated more favourably than an undeclared figure — and several of these scenarios (gate sustained rate, block capacity, move counts) **cannot** be answered without one.

Endpoint surface: `POST /api/cargo/simulate/{scenario}` + `GET /api/cargo/simulate/scenarios` (catalog).

---

## 7. Event and Notification Audit

| Stage | Verdict | Evidence |
|---|---|---|
| Event creation | ✅ | 18 typed constants (`service.py:33-54`); `_emit()` writes `core.cargo_event` and publishes 5 milestone topics to the bus. |
| Detection | ✅ | `GET /api/cargo/events?since={id}` — monotonic-id cursor poll, indexed `id DESC`. |
| Notification | ✅ | `core.cargo_notification` + `POST/GET /api/cargo/notifications`. Multi-transport: WebPush + WebSocket + FCM (`gateway/firebase.py`, `gateway/routers/push.py`). |
| Assignment | ✅ | `core.container_job_assignment` with a validated pre-condition chain (`container_job/service.py:134`): vehicle exists/ACTIVE/no open job, driver exists, PDP permit valid, transporter not blacklisted. |
| Resolution | ✅ | `cargo_notification.status` CHECK: `CREATED → ACKNOWLEDGED → RESOLVED`. |
| Closure | ✅ | Job `COMPLETED`/`CANCELLED` terminal states + `core.container_job_event` history. |
| Audit trail | ✅ | `core.cargo_lifecycle_event`, `core.cargo_workflow_event`, `core.decision_audit`, `core.case_audit`, `core.api_audit_log`. |
| Kafka | ✅ | `services/lifecycle_bus.py` → topic `jnpa.uc3.lifecycle`, best-effort by construction (a broker outage cannot fail a release, `service.py:194-199`), degrades to WS-only when no broker is configured. Kafka + Zookeeper + kafka-ui in compose. |

**This section is the strongest in the audit.** One note: only 5 of 18 events reach the bus (`_BUS_EVENTS`, `service.py:59-62`) — a deliberate, documented choice ("handover signals, not CRUD chatter"), correct for the design.

---

## 8. Performance Audit

| Check | Verdict | Evidence |
|---|---|---|
| Async | ✅ | End-to-end async: SQLAlchemy async engine + asyncpg, cached via `get_engine` lru_cache. No sync DB calls in any cargo path. |
| Database queries | ⚠️ | Parameterised throughout, whitelisted filter columns. `_where()` (`repository.py:241`) builds WHERE from a fixed identifier tuple — injection-safe by construction. |
| Indexes | ✅ | 9 on `core.cargo`, every filterable column covered, all event tables have `(container, id DESC)`. Migration `0116_timeseries_indexes.sql` adds time-series indexes. |
| Transactions | ⚠️ | Correct where they matter (`transition_lifecycle`, `record_workflow`, `create`) — **but see W2/W3**, where release spans two transactions. |
| Locks | ⚠️ | `FOR UPDATE` in `transition_lifecycle` and `record_workflow`. **Absent** in `update_cargo`'s read-check-write (W3). |
| Concurrent updates | ⚠️ | Safe on the lifecycle path; racy on the PUT-release path. |

**N+1 (P1).** `plan_rake()` (`service.py:600-614`) loops over containers issuing, per container, one `_emit` (own transaction), one `transition_lifecycle` (own `begin()`), and a second `_emit`. A 90-container rake = **~270 sequential round-trips**. `create_rake_plan` already writes the containers as jsonb in one statement; the per-container work should be one batched `UPDATE … WHERE container_number = ANY(:cns)` plus one multi-row audit `INSERT`.

**Also:** `GET /api/cargo` issues two queries (list + count for `X-Total-Count`) on every request, including unfiltered ones — a full `count(*)` over `core.cargo`. At 100k rows this becomes the endpoint's dominant cost. Consider an estimated count above a threshold.

| Load target | Verdict |
|---|---|
| **1,000 containers** | ✅ **Yes.** Indexed reads, `limit ≤ 1000`, async pool. No concern. |
| **10,000 containers** | ⚠️ **Yes, with caveats.** List+count is fine; `optimize_yard()` (`list_yarded_containers()`, `repository.py:510`) fetches **every yarded container with no LIMIT** and groups in Python — at 10k rows that is a full scan into application memory on every call. Rake planning of a large batch will be slow. |
| **High event volume** | ⚠️ **Partial.** `core.cargo_event` is well-indexed and cursor-paged, and Kafka publishing is non-blocking. But there is **no retention/partitioning policy** on `cargo_event` / `cargo_lifecycle_event` / `anpr_read`; these grow unbounded. |

**No cargo load test exists.** `tests/test_scale_offline_latency.py` tests the *truck simulator* fleet (30k devices), not cargo throughput. Note also the known environment issue: the full suite aborts natively at `test_performance` on this machine — run file subsets.

---

## 9. Security Audit

| Check | Verdict | Evidence |
|---|---|---|
| Authentication | ✅ | JWT bearer via `AuthMiddleware` (`gateway/auth.py:520`), globally installed (`main.py:633`). `validate_auth_config()` runs **before** the app is constructed (`main.py:138`) and **refuses to start an unauthenticated gateway outside local dev** (`auth.py:480`). Strong posture. |
| Authorization | ✅ | Longest-prefix path policy, ~35 rules (`_POLICY`, `auth.py:113-200`). |
| RBAC | ⚠️ **Gap on cargo reads** | Cargo **writes** are restricted: `("/api/cargo", _WRITE, CONTROL_ROOM \| CUSTOMS)` (`auth.py:244`). Cargo **reads** have **no `_POLICY` entry**, so they fall through to `ALL_ROLES` — a DRIVER or TRAFFIC_POLICE token can list every container in the port. Partially mitigated by `_ROLE_SCOPES` (`service.py:142-145`), which hard-scopes a driver to released boxes and customs to unreleased ones — but police and any unlisted role still see everything. |
| Secrets | ✅ | Env-driven, `secrets/` gitignored, `validate_auth_config` rejects default/weak JWT secrets in prod-like environments. |
| Input validation | ✅ | Pydantic v2 + **ISO-6346 check-digit validation on the PK** (`_clean_container_no`, `cargo.py:110-118`) — genuinely rigorous; invalid payloads map to 400 via a shared handler. |
| Audit logs | ✅ | `core.api_audit_log`, `core.decision_audit`, `core.case_audit`, plus the two cargo audit tables. `actor_role` is captured from the principal (`cargo.py:745-748`) and persisted on every transition. |
| PII | ✅ | `gateway/pii.py` + `gateway/dpdp.py`; `mask_for_request` applied on gate-document surfaces (`gate_documents.py:145`). |
| Driver scoping | ✅ | `driver_scope_violation()` (`auth.py:280`) prevents a DRIVER token from enumerating the fleet or addressing another device. |

**Recommended (P2):** add `("/api/cargo", CONTROL_ROOM | {CUSTOMS, DRIVER})` to `_POLICY` so cargo reads are not visible to every authenticated role by default.

---

## 10. Testing Audit

### Existing

| Type | Where | Count |
|---|---|---|
| Unit — state machine | `test_cargo.py:869` `test_state_machine_forward_and_mandatory_gates` | pure-function coverage of `can_transition` |
| API — CRUD | `test_cargo.py:361-511` | ~20 (201/409/404/400 paths, filters, pagination, role scope) |
| Workflow — lifecycle | `test_cargo.py:843-1073` | ~15 (`test_full_lifecycle_create_to_handover`, `test_release_before_verify_409`, `test_duplicate_release_409`, `test_double_discharge_409`, parametrised `test_release_gate_holds_for_every_pre_verify_state`) |
| Integration — real DB | `test_cargo.py:1076` / `:1117` | 2 (`CARGO_TEST_DSN`, real raw-SQL repository round-trip + full lifecycle) |
| Architecture invariants | `test_cargo.py:1216-1259` | 2 (single lifecycle writer; no write path forces RELEASE from an arbitrary state) — **unusually good practice** |
| Adjacent | `test_container_job.py` (30), `test_export_lifecycle.py` (15), `test_ecy_cfs_chain.py` (8), `test_import_chain.py` (10), `test_rail_services.py` (14), `test_performance.py` (19), `test_auth_rbac.py` (28), `test_scenarios.py` (7) | 131 |

**Total cargo-domain: 69 tests in `test_cargo.py` alone.** This is a well-tested transactional core.

### Missing tests required for JNPA scenarios

| Priority | Test | Why |
|---|---|---|
| **P0** | `test_create_rejects_is_released_true` | Would have caught **W1**. Add alongside the fix. |
| **P0** | `test_modal_shift_simulation` — 20% rail→road produces baseline + shifted hourly profiles and names a first constraint | II-A has no test because it has no code |
| **P0** | `test_crane_productivity_baseline_and_reduction` — moves/hour, −25%, new turnaround | II-B |
| **P0** | `test_berth_cascade_48h` — +6 h overrun displaces N calls by M hours | I-B |
| **P1** | `test_gate_hourly_profile_date_range` — bucketing over 1–3 Aug from `core.eir` | II-A + III-A |
| **P1** | `test_release_is_atomic` — inject failure between `_advance` and `update`, assert no divergence | **W2** |
| **P1** | `test_concurrent_put_release_races` — two concurrent PUTs, assert exactly one release | **W3** |
| **P1** | `test_yard_capacity_from_real_data` — congestion score uses `core.yard_block`, not the constant | Yard congestion defensibility |
| **P2** | `test_workflow_rejects_approve_after_reject` | **W4** |
| **P2** | `test_cargo_list_10k_under_slo` — load test at JNPA scale | Performance claim is currently untested |
| **P2** | `test_reefer_allocators_reconcile` — `cargo_reefer_plan` vs `core.reefer_slot` | Reefer double-allocation |

---

## Completed Features

- **Cargo lifecycle state machine** — 9 import + 7 export states, forward-only, mandatory gates. `services/cargo/service.py:74-120`, `repository.py:534-589`, CHECK-enforced in `0115`.
- **Atomic, audited transitions** — `FOR UPDATE` + status update + audit insert in one transaction; a single audit writer, enforced by test. `repository.py:166-192`, `tests/test_cargo.py:1216`.
- **24 cargo endpoints** with correct static-before-dynamic route ordering. `gateway/routers/cargo.py`.
- **Event + notification pipeline** — 18 typed events → `core.cargo_event` → Kafka `jnpa.uc3.lifecycle` + WebSocket + WebPush/FCM. `services/lifecycle_bus.py`, `gateway/firebase.py`.
- **UC-III job spine** — assignment with a 7-check pre-condition chain, 8-state machine, gate/yard/scan events. `services/container_job/`, migration `0113`.
- **Export leg** — booking → Form-13 → gate-in → VGM → LEO → COPRAR → loaded. `services/export_lifecycle/`, migration `0115`.
- **Reefer** — real slot inventory, SQL-computed availability, atomic allocation. `gateway/routers/reefer.py`, `core.reefer_slot`.
- **Real JNPA data ingestion** — daily reports, LDB, EIR/PIN/Form-13, CFS-ECY CODECO, customs IGM/OOC/RMS/LEO, shipping lines IAL/EAL/EDO, rail FOIS/Form-11/CTO, EDI COPARN. 27 migrations, 10 importer scripts.
- **Security posture** — fail-fast prod auth validation, path RBAC + write overlay, ISO-6346 validation, DPDP masking, multi-table audit.
- **Test discipline** — 69 cargo tests including real-DB round-trips and two architecture-invariant tests.

---

## Missing Features (by priority)

| Priority | Missing | Impact |
|---|---|---|
| **P0** | **Simulation/calculation engine** (`services/cargo/simulation/`) | Neither II-A nor II-B can be answered |
| **P0** | **Vessel move counts** (`core.vessel_call_moves`) | II-B has no numerator — productivity is not computable |
| **P0** | **Per-container evacuation mode** (`core.cargo.evacuation_mode`) | II-A cannot identify which containers move by rail |
| **P0** | **Date-ranged hourly gate profile** | II-A + III-A; `mart.v_gate_throughput` is pinned to 24 h |
| **P0** | **W1 — `POST /api/cargo` accepts `is_released:true`** | Recreates the exact corruption `0115` cleaned up |
| **P1** | Berth-queue cascade calculator | I-B, and the II-B knock-on effect |
| **P1** | Real yard-block capacity (`core.yard_block`) | Congestion score is currently indefensible |
| **P1** | W2/W3 — atomic release, locked PUT gate | Silent state divergence under load |
| **P1** | EIR date-range + hourly aggregation params | Notice §1.d requires traceable API queries |
| **P1** | Rake-planning N+1 | ~270 round-trips for a 90-box rake |
| **P2** | `PENDENCY` lifecycle state | Your stated lifecycle has 11 steps; 10 exist |
| **P2** | Cargo reads in `_POLICY` | Any authenticated role can list all containers |
| **P2** | W4 — workflow transition validation | APPROVE accepted after REJECT |
| **P2** | Reefer allocator reconciliation | Two independent allocators can double-book |
| **P2** | Event-table retention/partitioning | Unbounded growth |
| **P2** | `core.transporter` migration | Migration-only deploy fails at `0113` |

---

## JNPA Scenario Coverage Matrix

| Scenario | Supported | Partial | Missing | Required Fix |
|---|---|---|---|---|
| **I-A** Vessel Bunching (6 Aug) | Vessel calls, berths, terminals (`core.berthing_record`, `perf_daily_vessel`) | Berth queue sequence via `services/berthing/lifecycle.py` | Objective function; alternative-order costing; **no move counts to optimise against** | `berth_cascade.py` + `core.vessel_call_moves`; declare the objective explicitly |
| **I-B** Extended Berth Window (2 Aug) | ETA/ATA/berthing/departure/operation timestamps | Displacement is inferable by hand | 48 h cascade calculator; cumulative-delay aggregation | `simulation/berth_cascade.py` + `GET /api/berthing/cascade?from=&hours=48` |
| **II-A** Rail→Road Modal Shift (1–3 Aug) | EIR gate timestamps; daily rail TEUs; TAS slot capacity | Rail volume at day/terminal grain only | Per-container mode; hourly profile over a date range; capacity-vs-load; before/after | `0128` + `mart.v_gate_hourly_profile` + `simulation/modal_shift.py` |
| **II-B** Equipment Availability (6 Aug) | Hours worked (`cargo_operation_start/end`) | Vessel-call identity + berth sequence | **Move counts, crane registry, productivity fn, turnaround model, queue impact** | `0129` + `simulation/crane_productivity.py` |
| **III-A** Gate Approach Congestion | `core.eir` truck in/out (real); `core.gate_event`; `core.tas_appointment`; `mart.v_gate_queue_wait` | Live 24 h view only | Arrival-pattern characterisation; sustained-rate derivation; slotting proposal; quantified peak flattening | `simulation/gate_slotting.py` + date-ranged profile |
| **III-B** Driver Shortage (1–3 Aug → state 4 Aug) | `core.tt_trip` (trips/vehicle), `core.transporter_vehicle`, `core.eir.company`, `core.container_job_assignment` | Transporter attribution possible via EIR `company` | ⅓-trip-reduction model; throughput delta; exposure ranking by transporter and cargo flow; evacuation-strategy optimiser | `simulation/driver_shortage.py` |

**Score: 0 of 6 scenarios currently answerable end-to-end from the API.** Substrate exists for 4 of 6 (I-A, I-B, III-A, III-B); II-B is blocked on missing source data, II-A on a missing per-container attribute.

---

## Production Blockers

### P0 — must fix before evaluation

1. **Build `services/cargo/simulation/`.** Without a calculation engine, no What-If answer is producible from the backend. Start with **II-A and II-B** (they are explicitly named in the Notice), then `berth_cascade` (serves I-B *and* II-B's knock-on).
2. **Add `core.vessel_call_moves`** (migration `0129`). Without it, `Gross Moves / Working Hours` is arithmetic without a numerator. If JNPA source files carry no move count, **derive** from `core.edi_vessel_container` (migration `0125`) and stamp `data_origin='DERIVED'` — then declare that derivation as an assumption.
3. **Add `core.cargo.evacuation_mode`** (migration `0128`) + backfill. II-A's premise ("20% of containers currently evacuated by rail") is unaddressable until a container can be labelled RAIL or ROAD.
4. **Un-pin the gate hourly profile.** `mart.v_gate_throughput`'s hardcoded `now() - '24:00:00'` (`0103_mart_views.sql:62`) makes 1–3 August unqueryable. Add a date-ranged view over `core.eir.truck_in_time`.
5. **Fix W1.** Reject `is_released=true` on `POST /api/cargo` and add the regression test. This one is a ~10-line fix and it closes a data-integrity hole that a prior migration already had to clean up once.
6. **Verify data volume.** I could not reach the database. Before the demo, confirm actual row counts in `core.eir`, `core.gate_event`, `core.berthing_record`, `core.perf_daily_traffic`, and `core.cargo` for **1–6 August 2026**. A perfect engine over an empty table demos as a failure. (Note the prior finding in this repo's history where `core.perf_*` tables existed but were empty — check counts, not just existence.)

### P1 — fix before go-live

7. W2 — make release atomic (single transaction, or set `is_released` inside `transition_lifecycle`).
8. W3 — lock the row in `update_cargo`'s release check, or push the gate into the locked repository path.
9. `core.yard_block` capacity master; remove `_YARD_BLOCK_CAPACITY = 10`, or surface it as a declared assumption in the response.
10. `from`/`to`/`group_by` params on `GET /api/gate-docs/eir` — needed to satisfy Notice §1.d (traceable API queries).
11. Batch the rake-planning N+1.
12. `LIMIT` on `list_yarded_containers()`, or move the grouping into SQL.

### P2 — post-demo

13. `PENDENCY` lifecycle state; 14. Cargo reads in `_POLICY`; 15. W4 workflow ordering; 16. Reefer allocator reconciliation; 17. Event retention/partitioning; 18. `core.transporter` migration; 19. Cargo load test at 10k.

---

## Final Recommendation

### Can this UC-3 backend successfully demonstrate the JNPA Cargo What-If scenarios?

**Not today. With roughly 5–8 focused engineering days on the P0 list, yes — for II-A, I-B and III-A. II-B depends on whether move counts can be sourced or credibly derived.**

**The evidence for that judgement:**

**What is genuinely strong.** The cargo transactional core is better than most systems I audit at this stage. `can_transition()` (`service.py:107-115`) is a real state machine, not a status string with `if` statements. `transition_lifecycle()` (`repository.py:534-589`) takes a row lock and writes the state change and its audit row in one transaction — the property most implementations get wrong. There is a test that reads the repository's own source to assert only one function writes the audit table (`tests/test_cargo.py:1216`). The ISO-6346 check digit is validated on the primary key. The security posture refuses to boot an unauthenticated gateway outside local dev. Data ingestion from real JNPA sources is broad and real — EIR, CODECO, IGM, FOIS, EDI, daily reports. **None of this is demo scaffolding.**

**What is genuinely absent.** The Notice asks six questions, and every one has the same shape: *derive a baseline from the data, perturb one variable, quantify the difference, and show your assumptions*. The backend has **no code of that shape anywhere**. This is not a matter of a missing endpoint — it is a missing architectural layer. The nearest thing, `scenarios/`, does the opposite of what is needed: `scenarios/tfc1.py` closes a gate and injects 80 trucks into the live simulator. It is a good reactive demo and it is not an answer to "what would it cost".

**The sharpest single risk** is II-B. II-A, I-B, III-A and III-B are blocked on *code* — the substrate is in the database (`core.eir` has real truck in/out times; `berthing_record` has operation start/end; `tt_trip` has per-vehicle trips). II-B is blocked on **data**: `Gross Moves / Working Hours` needs a move count, and no table, column, or parser in this repository produces one. You have the denominator and not the numerator. Decide early whether to source move counts from JNPA or derive them from `core.edi_vessel_container` — and if you derive, say so loudly. The Notice (§1.c) explicitly states a declared assumption is treated more favourably than an undeclared figure. That clause is your friend here; use it deliberately rather than as damage control.

**The most efficient path.** Build `simulation/base.py` first, with the `Assumption` dataclass baked into the response envelope, then `berth_cascade.py` — it serves I-B directly and II-B's knock-on effect, so it is the highest-leverage single module. Then `modal_shift.py` (II-A) on top of a date-ranged EIR profile, which also unlocks III-A. Fix W1 in the same sprint; it is small and it removes a data-integrity hole the codebase has already had to repair once.

**One caution on scope.** Do not let the simulation layer write to `core.cargo`. `scenarios/` mutates live state and needs `reset()`; a what-if answer must be reproducible on demand and must never leave the demo database in a different state than it found it. Read-only, always.

---

### Verification limits of this audit

- **The database was not reachable** from the audit host. All schema claims come from migration SQL and runtime-DDL modules; **no row count was verified.** Confirm data presence for 1–6 August 2026 before relying on any scenario answer.
- Runtime DDL in `gateway/*_ext.py` means the deployed schema may differ from the migration set. `core.transporter` is the proven case (created only at gateway boot). Reconcile `information_schema` against `infra/postgres/v3/` before the evaluation.
- Tests were **not executed** — counts and assertions come from reading the files. Note the known environment issue where the full suite aborts natively at `test_performance` on this machine; run file subsets.
