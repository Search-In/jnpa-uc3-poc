# UC-3 Lifecycle Implementation — Phase Report

**Date:** 2026-07-31 · **Branch:** `migrate-schema-v3` · **Baseline:** `docs/UC3_LIFECYCLE_BACKEND_AUDIT.md`
**Scope authority:** client lifecycle documents (F-U3 truck & gate, F-Y1 empty ECY→CFS) + the client clarification of 2026-07-31.

**Verification:** `170 passed, 1 skipped` across the UC-3 and adjacent suites; `web` and `mobile-pwa` both `tsc --noEmit` clean.

---

## Client clarification — how each point was applied

| # | Instruction | What was done |
|---|---|---|
| 1 | Remove Follow-The-Box from scope | The screen is no longer routed or navigated. `/follow-the-box` now redirects to the new `/uc3-lifecycle` console; nav leaf and i18n key replaced. `FollowTheBox.tsx` is left on disk untouched (no deletion requested) but is not reachable. |
| 2 | Follow the UC-3 lifecycle + demo-readiness docs | Every entity, field and demo assertion traces to a document line (see the fidelity table below). |
| 3 | Approved DB additions | All seven implemented. `core.gate_event` extended (not recreated). |
| 4 | Verify before creating `core.scanner_machine` | Verified absent — justification below. Created. |
| 5 | Reuse existing architecture | Form-13 reuses `core.gate_capture`; upload modules clone the CFS-ECY ledger pattern; RBAC, router/service/repository layering, ISO-6346 lib, KafkaPump all reused. |
| 6 | Additive + backward compatible | Every migration is `CREATE ... IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`. No column dropped, no type changed, no existing row rewritten. |
| 7 | Explain each phase before moving on | This document; each phase section states DB / API / backend / UI / PWA impact. |

### Point 4 — `core.scanner_machine` verification result

**Verified: the table does not exist.** Evidence:

```
grep -rniE "CREATE TABLE[^;]*(scanner|scan_machine|machine)" infra/postgres/
  → only the new 0113 file
```

The only scanner data in the schema is two **free-text columns on each RMS selection row** (`core.rms_scan_container.machine_type` — a single letter `D`/`M`/`F` — and `scan_location`, e.g. `INNSA1RSDT02`, both from migration 0031). Those are per-selection attributes, not a registry.

**Why a new table is required:** the client lifecycle step is *"[scanner if RMS-selected]"* — routing a truck to a specific machine. From the selection columns alone the backend cannot answer what a machine *is*: there is no decode of `D`/`M`/`F` into drive-through / mobile / fixed (that mapping exists only as an SQL comment in 0031), no terminal or lane, and no in-service flag, so a routing decision cannot be made or displayed and an out-of-service machine cannot be excluded. The alternative — deriving everything from the free-text strings on each selection row — was rejected because it re-parses a code on every read, cannot represent a machine that has no current selection, and has nowhere to record `active`. The new table is a small reference master (4 seeded rows, the exact machines named in the client's RMS lists) that the selection rows join to by reconstituted `machine_code`; the selection columns are unchanged.

---

## Phase 1 — Foundation fixes

**Problem:** the audit found the master-data foundation partly broken.

| Change | File | Effect |
|---|---|---|
| Transporter importer ported to v3 columns | `scripts/import_transporter_master.py` | Was writing legacy `source_company_id/name/mobile/doc_type/doc_file` and conflicting on a constraint absent in v3 → every run errored. Now writes `company_id/company_name/mobile_number/document_type/document_file`, conflict on `company_id`. |
| Driver + PDP importer ported to v3 | `scripts/import_driver_master.py` | Driver upsert now uses `licence_number/driver_name/date_of_birth` and the real partial-unique arbiter (`WHERE id < 100000000`); PDP upsert uses `accepted_at/valid_until/cancelled_by` + derives `cancellation_date`. This restores the **only** PDP ingestion path (the upload module has no PDP entity). |
| **DB:** `core.pdp` indexes | `infra/postgres/v3/0111_uc3_foundation.sql` | 367,078-row table had **no indexes** in v3 (legacy 0026 ones were never ported). Added `(pdp_number, accepted_at DESC)`, `(appl_number)`, partial `(active)`. Every permit lookup was a sequential scan. |
| **PDP semantics corrected** | `services/driver_master/repository.py`, `service.py` | `pdp_status`, the `/stats` KPIs and `validate()` were computed from `core.driver.licence_valid_to` (the *driving licence* date) and ignored the permit entirely. Now the **actual permit** decides (`core.pdp.active` + `valid_until`), with the licence date as fallback only when no permit row exists. `validate()` additionally returns `pdp_valid`, `pdp_active`, `pdp_validity`. |
| **RBAC hole closed** | `gateway/auth.py` | `/api/transporters` was absent from `_POLICY` → blacklist/create/lift were open to **any** authenticated role incl. DRIVER. Now control-room + customs + police for reads, control-room/customs for writes. |
| Customs reconcile automated | `services/customs/service.py` | `reconcile_cargo()` ran only when a human called it, so `rms_selected` could never reach `core.cargo` in a running system. Now runs automatically after a successful import (best-effort; a failure never fails the import). |
| RMS amendments no longer dropped | `services/customs/repository.py` | `ON CONFLICT (report_id, sl_no) DO NOTHING` silently discarded a re-issued selection list that changed a container's machine. Now `DO UPDATE`. |
| New API: RMS containers | `gateway/routers/customs.py` | `GET /api/customs/rms/{igm_no}/containers` (+ `machine_type`, `scan_location`, `container_no` filters). Previously only the scan-list *header* was reachable. |
| Boot-DDL drift fixed | `gateway/customs_ext.py`, `fleet.py`, `cfs_ecy_ext.py` | `customs_ext` created RMS tables with `scanlist_id/scan_machine` while the runtime SQL used `report_id/machine_type/agent_pan/processing_end` — on a fresh DB with `JNPA_RUNTIME_DDL=1` every RMS insert would fail. Also fixed the second view-join divergence, `fleet._DDL`'s `vehicle_number` vs queried `vehicle_no`, and `cfs_ecy_ext` creating `mart.` objects without `CREATE SCHEMA mart`. |
| Stale test ported | `tests/test_customs_repository.py` | Asserted legacy table names under `core.`. |

**API:** 1 added, 0 changed shape. **UI/PWA:** none. **Tests:** 73 passing in the affected suites.

---

## Phase 2 — Gate documents (EIR / PIN ticket / Form-13)

**Problem:** the corpus's flagship artifacts had no home — `grep -rniw eir` returned **zero hits** repo-wide; PIN tickets and BAT lanes likewise.

**DB — `infra/postgres/v3/0112_gate_documents.sql`:**
- `core.eir` — container (nullable), seal, vessel + VIA, **BAT lane**, truck, driver, TruckIn/TruckOut, **`tat_minutes` as a generated column**, company, from/to CFS, scanner stamp.
- `core.pin_ticket` — PIN number, **free-format `yard_location`** (the real `2P08D.1` cannot fit the cargo `yard_block` regex `^[A-Z]{1,2}-\d{1,3}$`), gate, company, `move_type`, **`leg_seq`** so a dual-move ticket is two legs sharing one PIN.
- `core.gate_doc_import_file` / `_error` — the upload ledger, required by the pattern every other upload module follows.
- **Form-13: no new table.** It reuses `core.gate_capture`, which already declares `capture_type='FORM13'`, keeps `container_no` nullable and carries `vehicle_plate` + `payload jsonb`. Real uploads are written with `source_mode='live'`, leaving the existing deterministic `'sim'` seed rows untouched. Two partial payload indexes added.

**Backend:** new `services/gate_documents/` (`upload_parsers` / `repository` / `service`) following the CFS-ECY module shape — alias-driven column mapping, ISO-6346 check-digit, IST timestamps, sha256 file dedup + `row_sha256` row dedup, per-row SAVEPOINT.

**Key fidelity decision:** the **truck**, not the container, is the required key for every document type — so the corpus's containerless EIR (truck `MH46AF4375`) is ingested and stays truck-keyed. Every other module in the repo silently drops such rows.

**API:** `GET /api/gate-docs/{summary,eir,pin,form13,container/{cn},truck/{no},tat}`, `GET /templates/{doc_type}`, `POST /validate|/upload`, `GET /uploads[/{id}]`. `GET /tat` is the **document-derived** TAT, independent of the simulator-fed KPI views.

**Tests (17):** assert the corpus ground truth `165` and `82` minutes from the two GTI EIRs; the containerless case; the BMCT dual-move ticket as two legs with `EXPORT_DROP` + `IMPORT_PICK`; `2P08D.1` surviving verbatim; Form-13 VisitID `4418958` + gates `IGTK01`/`OGTK05`; row-hash idempotency; boot-DDL↔migration lock-step.

---

## Phase 3 — Container job assignment (the backbone)

**Problem:** no truck↔container job entity existed. The only link was `core.cargo.vehicle_number`: free text, no driver, no validation, no uniqueness.

**DB — `infra/postgres/v3/0113_container_job.sql`:**
- `core.container_job_assignment` — container **or** `group_code` (empty-by-group jobs), transporter, vehicle, driver, `move_type`, document reference, terminal/gate, 8-state `status`, timestamps.
- Two **partial unique indexes** enforce the guard the audit found missing, concurrency-safe: one open job per vehicle, one per container (terminal states excluded).
- `core.container_job_event` — append-only audit history (child of the approved entity; required by the "every lifecycle action has audit history" rule).

**Backend — `services/container_job/`:** the validation chain runs **pure input checks before resource lookups**, so a malformed request never reports a resource conflict as its reason:

1. `move_type` valid · 2. container **or** group present · 3. ISO-6346 · 4. container has no open job · 5. vehicle exists · 6. vehicle `ACTIVE` · 7. vehicle has no open job · 8. driver exists + `ACTIVE` · 9. **PDP permit active and in date** · 10. transporter not blacklisted.

State machine: `ASSIGNED → ACCEPTED → AT_GATE → IN_YARD → PICKED_UP|DROPPED → COMPLETED`, `CANCELLED` from any open state; transitions applied under `SELECT ... FOR UPDATE` with the history row in one transaction.

**API:** `POST /api/jobs`, `POST /api/jobs/validate` (dry-run), `GET /api/jobs[/{id}]`, `POST /api/jobs/{id}/{accept,complete,cancel}`, `GET /api/cargo-jobs/container/{cn}`.

**Tests:** unknown/inactive vehicle rejected; **cancelled permit** rejected naming the canceller; **expired permit** rejected; blacklisted transporter rejected; double-assignment of truck *and* container rejected; a job that never started cannot be completed, only cancelled; completing frees the truck for the next job.

---

## Phase 4 — Gate events + yard movements

**Problem:** `core.gate_event` had exactly one writer — the truck simulator. No HTTP path recorded a real crossing; no container, lane or document on the event; `GateTransaction`/`gate.transactions` were dead.

**DB (additive ALTERs on the existing table, per the approved list):** `core.gate_event` gains `container_number`, `bat_lane`, `document_type`, `document_reference`, `job_id`, `source`, `driver_id` + two indexes. The simulator's writes are unaffected (all new columns are nullable). API-recorded crossings satisfy the pre-existing `NOT NULL` `device_id`/`trip_id` by using the plate and `JOB-{id}`.

**DB:** `core.cargo_movement_event` — `YARD_PICKUP` / `YARD_DROP` / `YARD_MOVE` with free-format `yard_location`, vehicle, driver, terminal, actor.

**Behaviour:** a gate event advances the linked job (`GATE_IN → AT_GATE`, `GATE_OUT → COMPLETED`); a yard pickup/drop advances it through `IN_YARD` to `PICKED_UP`/`DROPPED`. **A crossing is a physical fact: it is recorded even when the job cannot advance** (e.g. an out-of-order `GATE_OUT`) — asserted by test.

**API:** `POST|GET /api/gate/events`, `POST|GET /api/yard/movements`.

---

## Phase 5 — Scanner routing

**DB:** `core.scanner_machine` (justified above) seeded with the four machines from the client's RMS lists, plus `core.scan_event` (`SCAN_PENDING|SCANNED_CLEAN|SCAN_HOLD|SCAN_SKIPPED`).

**The routing answer that did not exist:** `GET /api/scan/status/{container}` reconstitutes the full machine code from the stored letter + location (`D` + `INNSA1RSDT02` → `D-INNSA1RSDT02`), joins the machine master for its class, and reports the latest verdict. Recording `SCANNED_CLEAN` clears the customs hold (back to `PENDING`; OOC still decides final clearance); `SCAN_HOLD` sets `UNDER_INSPECTION`.

**API:** `GET /api/scan/machines`, `GET /api/scan/status/{cn}`, `POST|GET /api/scan/events`.

---

## Phase 6 — Lifecycle event distribution

**Problem:** every milestone was a DB row discoverable only by polling; nothing consumed `cargo.released`.

**Backend:** new `services/lifecycle_bus.py` publishing to Kafka topic `jnpa.uc3.lifecycle` **and** the existing WS hub (channel `uc3_lifecycle`), wired into `CargoService._emit` (release / verify / discharge / lifecycle-change / customs-status) and every job, gate, yard and scan milestone. Best-effort by construction — a broker outage can never fail a container release.

**Performance fix found while testing:** the producer was being constructed in every context (unit tests, CLI importers) and then retried an unreachable broker for its lifetime — the affected suite went from ~11 s to **14 minutes**. It now only connects when `KAFKA_BROKERS`/`KAFKA_BOOTSTRAP_SERVERS` is actually set; the suite is back to 11 s. A regression test guards this.

---

## Phase 7 — ECY→CFS chain (F-Y1)

**Problem:** the chain was never materialised — `mart.v_cfs_ecy_dwell` groups **by facility**, so an ECY leg and a CFS leg can never combine; the 242 verified chains existed only in an operator's head; and the planted `COSU4663595` anomaly (duplicate IN + two OUTs) was silently absorbed, its retained second OUT biasing dwell by +2 h with no flag.

**DB — `infra/postgres/v3/0114_ecy_cfs_chain.sql`:** `core.ecy_cfs_chain`, one row per container with the three legs, `transit_hours` / `dwell_hours` / `cycle_hours`, `chain_status`, and `anomaly_codes[]` + `anomaly_detail`.

**Anomaly codes:** `DUPLICATE_IN`, `MULTI_OUT`, `OUT_BEFORE_IN`, `ORPHAN_CFS_IN`, `NO_CFS_IN`, `LONG_TRANSIT`. The rebuild is one idempotent statement set; a lock test asserts the SQL and the documented label table cannot drift apart.

**Honesty:** the sixth leg (terminal export gate-in) is returned as `present: false` with `note: "not in corpus — no source file records this leg"` rather than fabricated — matching the client document, which marks it absent.

**API:** `POST /api/cfs-ecy/chains/rebuild`, `GET /api/cfs-ecy/chains`, `/chains/stats`, `/chains/{container}`.

---

## Web UI

**New screen `web/src/screens/Uc3Lifecycle.tsx` at `/uc3-lifecycle`** — one journey view, no page jumping, four tabs:

- **Lifecycle** — job list on the left; on the right the 5-step timeline (Truck Assignment → Gate In (BAT) → Yard Pickup/Drop → RMS Scanner → Gate Out). **Every step is clickable** and opens its detail (timestamps, gate, BAT lane, yard location, machine code, result). A single context-aware action button advances the job; the audit history is listed underneath.
- **Documents** — every EIR / PIN / Form-13 for the searched container, with TAT / yard / VisitID surfaced.
- **ECY → CFS chains** — KPI tiles, per-chain legs and durations, **amber anomaly badges** with readable labels, and a rebuild button.
- **Data upload** — `web/src/screens/gatedocs/UploadPanel.tsx`: template → validate (preview + errors/warnings) → import → history, identical to the other modules.

**API client:** ~30 helpers + UC-III types appended to `web/src/lib/api.ts`. **Routing/nav/i18n/RBAC:** leaf and `SCREEN_ROLES` entry added; `/follow-the-box` and `/document-ocr` redirect to the new console.

---

## Driver PWA

**New screen `mobile-pwa/src/screens/Jobs.tsx`, route `/jobs`, new bottom-tab "Jobs".** The driver sees only their own jobs — the gateway resolves scope from the **device binding on the token**, never from client input, and returns 404 (not 403) for another driver's job id so ids cannot be probed.

One context-aware button per job: **Accept → Reached gate → Confirm pickup/drop → Complete trip**, with a yard-location field on the pickup/drop step. Every tap calls the same state machine the control room uses, so driver and operator actions produce identical audit history.

**Backend:** `gateway/routers/driver_jobs.py` (`/api/driver/jobs`), ownership-enforced on top of the existing `/api/driver` RBAC rule. **API client:** `myJobs`, `myJob`, `jobAccept`, `jobGateArrival`, `jobPickup`, `jobDrop`, `jobComplete` + types.

---

## Migration inventory (all additive)

| File | Objects |
|---|---|
| `0111_uc3_foundation.sql` | 3 indexes on `core.pdp` |
| `0112_gate_documents.sql` | `core.eir`, `core.pin_ticket`, `core.gate_doc_import_file`, `core.gate_doc_import_error`, 2 indexes on `core.gate_capture` (Form-13 reuse) |
| `0113_container_job.sql` | `core.container_job_assignment`, `core.container_job_event`, 7 `ADD COLUMN IF NOT EXISTS` on `core.gate_event`, `core.cargo_movement_event`, `core.scanner_machine` (+4 seed rows), `core.scan_event` |
| `0114_ecy_cfs_chain.sql` | `core.ecy_cfs_chain` |

Boot-DDL mirrors live in `gateway/gate_docs_ext.py` (lock-step asserted by test), gated by `JNPA_RUNTIME_DDL=1` as with every other module.

---

## Known limitations (stated, not hidden)

1. **The migrations have not been executed** — no database was reachable in this environment. All SQL is written and the boot-DDL mirror is lock-step tested, but the DDL itself is unexecuted; run 0111–0114 against a dev DB before the demo.
2. Tests are contract/unit level against fakes (the repo's established pattern). The DSN-gated integration tests still need a live Postgres.
3. `core.form13` does not exist by design — Form-13 lives in `core.gate_capture` with `source_mode='live'`. Anything querying a `core.form13` table will find nothing; use `/api/gate-docs/form13`.
4. The transporter-name bridge (TRANSTAR/TRANSTA → transporter master) is **not** implemented — it was in the audit's Phase-2 plan but is not in the approved table list. Gate documents store the company name as free text.
5. Corridor-exit as a distinct lifecycle step remains unimplemented (no approved entity for it).
6. The EIR/PIN parsers accept the tabular upload format; parsing the original scanned PDFs is out of scope.
