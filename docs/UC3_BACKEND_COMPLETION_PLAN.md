# UC-3 Backend — End-to-End Audit & Completion Plan

**Date:** 2026-08-01 · **Status:** AUDIT + PLAN ONLY — no code changed
**Verified against:** the live RDS (`jnpa_schema_v3`) and a running gateway, not from memory
**Demo container:** `MSCU1234566` — ISO-6346 **valid** (check digit 6 confirmed), already present in `core.cargo` as `CREATED`

---

## 0. Three findings that block the demo today

**B1 — The demo container's documents are invisible to the API.**
`core.gate_capture` holds a real FORM13 row for `MSCU1234566` (`F13758233896`, plus ESEAL / WEIGHBRIDGE / ICEGATE), but every row is `source_mode='sim'` — written by the deterministic seed generator. `services/gate_documents/repository.py` scopes Form-13 reads to `source_mode = 'live'`, so:

```
GET /api/gate-docs/container/MSCU1234566
→ {"eir":[],"pin":[],"form13":[],"total":0}     ← while a Form-13 demonstrably exists
```

That filter was my decision (keep real uploads separate from seed). It is wrong as written: it silently reports "no documents" instead of "documents exist, provenance = SIM". **Fix by returning both with provenance, not by inserting data.**

**B2 — The cargo state machine has never executed on this database.**
All **19/19** `core.cargo` rows are `CREATED`; `core.cargo_lifecycle_event` has **0 rows**. Discharge → yard → verify → release has never run in production. (By contrast the job spine has run: 7 `container_job_event` rows.)

**B3 — `POST /api/cargo` writes no lifecycle-history row.**
`create_cargo` emits `cargo.created` into `core.cargo_event` only. `core.cargo_lifecycle_event` is written exclusively by `transition_lifecycle()`, so a new container has status `CREATED` (a DB default) with **no audit row explaining how it got there** — breaking the "every operation creates an event" rule in Phase 7.

---

## 1. Current architecture flow

```
Router (gateway/routers/*.py)      FastAPI, DTOs, HTTP codes, RBAC
        ↓
Service (services/<module>/service.py)   business rules, state machine, events
        ↓
Repository (services/<module>/repository.py)   raw SQL over the shared async engine
        ↓
PostgreSQL core.* / mart.*        (infra/postgres/v3/*.sql migrations)
```

Cross-cutting: `services/lifecycle_bus.py` (Kafka + WS fan-out, best-effort), `gateway/auth.py` (prefix RBAC), `jnpa_shared/iso6346.py` (validation), upload-ledger pattern (sha256 file dedup + row dedup + per-row SAVEPOINT).

**This is a sound architecture and the plan below adds nothing new to it.**

---

## 2. Step-by-step trace (Phase 1 deliverable)

| # | Step | Endpoint | Router | Service | Repository | Table | Status |
|---|---|---|---|---|---|---|---|
| 1 | Cargo creation | `POST /api/cargo` | `cargo.py` | `cargo/service.create_cargo` | `repository.create` | `core.cargo` | ✅ works · ❌ **no lifecycle-history row (B3)** |
| 2 | Form-13 | `POST /api/gate-docs/upload` (`doc_type=FORM13`) | `gate_documents.py` | `gate_documents/service.import_file` | `repository.persist` | `core.gate_capture` (`capture_type='FORM13'`) | ✅ upload works · ❌ **read hidden by `source_mode` filter (B1)** |
| 3 | EIR | same, `doc_type=EIR` | ” | ” | ” | `core.eir` | ✅ works (TAT generated column verified 165/82 min) |
| 4 | PIN | same, `doc_type=PIN` | ” | ” | ” | `core.pin_ticket` | ✅ schema + parser ready · ⚠️ **0 rows live** |
| 5 | Doc lookup by container | `GET /api/gate-docs/container/{cn}` | ” | `service.docs_for_container` | `repository.docs_for_container` | 3 tables, keyed on `container_number` | ✅ mapping correct · ❌ Form-13 arm filtered out |
| 6 | Truck selection | `GET /api/vehicles/available` | `vehicles.py` | `gateway/fleet.py` | inline SQL | `core.vehicle` | ✅ works — 40 ACTIVE |
| 7 | Driver selection | `GET /api/drivers/master` | `drivers_master.py` | `driver_master/service` | `driver_master/repository` | `core.driver` + `core.pdp` | ✅ works — 31,846 drivers · ⚠️ **no plain `/api/drivers`** |
| 8 | Job creation | `POST /api/jobs` | `container_job.py` | `container_job/service.assign` | `repository.create_job` | `core.container_job_assignment` | ✅ 10-check validation · ❌ **no document check** |
| 9 | Job events | job actions | ” | `service._advance` | `repository.transition` | `core.container_job_event` | ✅ works · ⚠️ **event names differ from your spec** |
| 10 | Gate in/out | `POST /api/gate/events` | ” | `service.record_gate_event` | `repository.record_gate_event` | `core.gate_event` | ✅ verified live |
| 11 | Yard pickup/drop | `POST /api/yard/movements` | ” | `service.record_movement` | `repository.record_movement` | `core.cargo_movement_event` | ✅ verified live |
| 12 | Scanner | `POST /api/scan/events` | ” | `service.record_scan` | `repository.record_scan` | `core.scan_event` | ✅ verified live |
| 13 | Vessel discharge | `POST /api/cargo/{cn}/discharge` | `cargo.py` | `service.discharge_cargo` | `transition_lifecycle` | `core.cargo` + `cargo_lifecycle_event` | ⚠️ **never executed (B2)** |
| 14 | Yard assignment | `PUT /api/cargo/{cn}/yard-assignment` | ” | `service.assign_yard` | ” | ” | ⚠️ never executed |
| 15 | Yard position | `POST /api/cargo/{cn}/yard-position` | ” | `service.allocate_yard_position` | ” | ” | ⚠️ never executed |
| 16 | Scan pending | — | — | — | — | — | ❌ **derived label only, never written** |
| 17 | Verification | `POST /api/cargo/{cn}/verify` | ” | `service.verify_cargo` | ” | ” | ⚠️ never executed |
| 18 | Release | `POST /api/cargo/{cn}/release` | ” | `service.release_cargo` | ” | ” | ⚠️ never executed · ❌ **`PUT is_released=true` bypass still open** |

---

## 3. Database mapping (as deployed)

| Concern | Table | Key | Notes |
|---|---|---|---|
| Cargo | `core.cargo` | `container_number` (PK) | `lifecycle_status` CHECK = 9 states; **zero FKs** |
| Cargo audit | `core.cargo_event` | `container_number` | 2 rows |
| Cargo history | `core.cargo_lifecycle_event` | `container_number` | **0 rows** |
| EIR | `core.eir` | `row_sha256` (partial unique) | `tat_minutes` GENERATED |
| PIN | `core.pin_ticket` | `row_sha256` | one row per move leg (`leg_seq`) |
| Form-13 | `core.gate_capture` | `(container_no, capture_type, captured_at)` | **reused, not duplicated** |
| Job | `core.container_job_assignment` | partial-unique on open vehicle + open container | duplicate-prevention verified live |
| Job history | `core.container_job_event` | `job_id` | 7 rows |
| Gate | `core.gate_event` | — | 47,376 rows (+7 additive columns) |
| Yard | `core.cargo_movement_event` | `job_id` | |
| Scan | `core.scan_event` | FK → `core.scanner_machine` | |
| Chain | `core.ecy_cfs_chain` | `container_number` unique | 1,202 rows |

---

## 4. Missing implementation list

| ID | Gap | Phase | Severity |
|---|---|---|---|
| **M1** | Form-13 reads exclude seeded rows → documents invisible | 5 | **P0 blocker** |
| **M2** | `POST /api/cargo` writes no lifecycle-history row | 2, 7 | **P0** |
| **M3** | `POST /api/jobs` does not verify required documents exist | 2, 4 | **P0** |
| **M4** | `SCAN_PENDING` never written — a hole in the documented state machine | 6 | P1 |
| **M5** | `PUT /api/cargo/{cn}` with `is_released=true` jumps any state → `RELEASED`, bypassing verify | 6 | P1 |
| **M6** | Job event names differ from your spec (`job.assigned` vs `JOB_CREATED`, `job.yard_pickup` vs `job.pickup`, `job.completed` vs `job.complete`) | 4 | P1 (contract) |
| **M7** | No plain `GET /api/drivers` (only `/api/drivers/master`) | 3 | P2 |
| **M8** | Job creation does not link back to cargo — `core.cargo.vehicle_number` is still free text (`MH05CD4567` on the demo container) alongside the new job spine: **two sources of truth** | 2 | P1 |
| **M9** | Document events: no `document.*` event stream (Phase 7 asks for one) | 7 | P2 |
| **M10** | Schema drift: `advance_list_container.id` NULL for 8,878 rows; `rms_scan_container` missing 0102 columns | 8 | P1 |
| **M11** | Zero FKs between cargo and its child tables | 8 | P2 |

---

## 5. Required code changes (no demo hacks, no manual inserts)

### P0 — unblocks the demo

**C1 · Form-13 provenance instead of exclusion** — `services/gate_documents/repository.py`
Drop the hard `source_mode='live'` filter; always select `source_mode` and return it per row. Add an optional `?source=live|sim|all` query param (default `all`) on `GET /api/gate-docs/container/{cn}`. The UI already has a `ModeChip` (`GateCustoms.tsx`) to badge SIM vs LIVE.
*Why not seed a "live" row: that would be inserting demo data to satisfy a query bug.*

**C2 · Lifecycle history on create** — `services/cargo/service.py`
`create_cargo` writes one `core.cargo_lifecycle_event` row (`old_status=NULL`, `new_status='CREATED'`, `action='CREATE'`, actor from the principal). Reuse the existing repository insert — no new table.

**C3 · Document validation at assignment** — `services/container_job/service.py`
Add check #11 to the existing chain: for `IMPORT_PICK`/`EXPORT_DROP`, require at least one gate document for the container (EIR ∪ PIN ∪ FORM13) unless `allow_missing_documents=true` is passed explicitly. New error code `documents_missing`, HTTP 400, consistent with the other ten checks. Reuse `GateDocumentRepository.docs_for_container` — no new SQL.

### P1 — correctness

**C4 · Close the release bypass** — `services/cargo/service.py`: route the legacy `PUT is_released=true` through `_advance(...LC_RELEASED)` so it obeys the state machine, or reject it with 409 and a pointer to `POST /release`.

**C5 · Write `SCAN_PENDING`** — set it when `customs_status` becomes `UNDER_INSPECTION` (customs reconcile) or when a scan is recorded as pending, so the documented state exists in data rather than only as a queue label.

**C6 · Event-name aliases** — emit your spec's names *in addition to* the current ones (`JOB_CREATED`, `job.pickup`, `job.complete`) so no existing consumer breaks. Contract-level change only.

**C7 · Link job → cargo** — on job assignment, set `core.cargo.vehicle_number` from the job (single write path) so the legacy field stops diverging from the job spine.

**C8 · `GET /api/drivers`** — thin alias to the existing driver-master list; no new service.

### P2 — hygiene

**C9** document event stream (`core.gate_doc_event`, mirroring `cargo_event`) · **C10** FKs + drift backfill (see §6).

---

## 6. Migration changes (proper migrations — no manual ALTER)

| Migration | Contents | Risk |
|---|---|---|
| `0115_uc3_integrity.sql` | Backfill `core.advance_list_container.id` from a sequence (fixes the 8,878 NULLs); add the 0102 columns `rms_scan_container.{id,igm_no,iso_valid,created_at}` that were never applied here; index `core.cargo_lifecycle_event (container_number, id DESC)` | low — additive + one backfill UPDATE |
| `0116_uc3_events.sql` | `core.gate_doc_event` (document event stream, C9) | low — new table |
| `0117_uc3_fk.sql` | FKs `cargo_event / cargo_lifecycle_event / cargo_movement_event → core.cargo(container_number)`, `NOT VALID` first then `VALIDATE CONSTRAINT` | **medium** — run in a window; `NOT VALID` avoids a long lock |

All additive and backward compatible, matching the 0111–0114 style already applied.

---

## 7. Testing commands

```bash
# unit / contract (fakes — no DB)
.venv/bin/python -m pytest tests/test_cargo.py tests/test_container_job.py \
  tests/test_gate_documents.py tests/test_ecy_cfs_chain.py -q

# integration against the live RDS
export POSTGRES_DSN='postgresql+asyncpg://postgres:***@database-1...:5432/jnpa_schema_v3?ssl=require'
.venv/bin/python -m uvicorn gateway.main:app --port 8099

# the full demo scenario, API-only (no manual SQL)
curl -s localhost:8099/api/gate-docs/container/MSCU1234566          # C1: docs visible
curl -s -XPOST localhost:8099/api/jobs -H 'content-type: application/json' \
  -d '{"container_number":"MSCU1234566","vehicle_id":"TRK-000020",
       "driver_licence":"MH23 20170012229","move_type":"IMPORT_PICK"}'   # C3
curl -s -XPOST localhost:8099/api/cargo/MSCU1234566/discharge -d '{}'
curl -s -XPUT  localhost:8099/api/cargo/MSCU1234566/yard-assignment -d '{"yard_block":"C-09"}'
curl -s -XPOST localhost:8099/api/cargo/MSCU1234566/verify -d '{"verified":true}'
curl -s -XPOST localhost:8099/api/cargo/MSCU1234566/release -d '{}'
curl -s localhost:8099/api/cargo/MSCU1234566/lifecycle                # audit trail
```

**Negative tests that must stay red:** release before verify → 409 · second job on the same truck → 400 · cancelled-PDP driver → 400 · job without documents → 400 (after C3).

---

## 8. Final UI demo flow

UC-3 Lifecycle → search `MSCU1234566` → **Documents** tab shows FORM13 (badged SIM until a real upload) → assign truck `TRK-000020` + driver → Gate In (BAT lane) → Yard Pickup → Scanner → Release → Gate Out → audit history → ECY→CFS Chains → Driver PWA.

---

## 9. Recommended order

| Step | Work | Effort |
|---|---|---|
| **1** | C1 + C2 + C3 (P0) — makes the documented flow run end-to-end for `MSCU1234566` with **no manual inserts** | ~0.5 d |
| **2** | C4–C8 (P1) — state-machine correctness + contract aliases | ~1 d |
| **3** | `0115` migration + C9/C10 | ~0.5 d |
| **4** | `0117` FK migration in a maintenance window | ~0.5 d |

**Nothing here inserts demo data.** The demo container becomes fully demonstrable because the read path stops hiding rows that already exist (C1) and the write paths (job + cargo lifecycle) are driven purely through the APIs.

**Awaiting approval before any code change.**
