# UC-3 Lifecycle — Deployment & Validation Report

**Date:** 2026-08-01 · **Target:** AWS RDS PostgreSQL 18.3 · `__RDS_HOST__:5432/jnpa_schema_v3`
**Branch:** `migrate-schema-v3` · **Baseline:** `docs/UC3_LIFECYCLE_IMPLEMENTATION.md`

**Overall: DEPLOYED — no BLOCKERS.** 4 migrations applied, 8 tables verified, all pre-existing data preserved exactly. **4 production-only defects were found by this validation and fixed**; every fix is re-verified against the live database.

Nothing below is marked PASS unless it was executed against the live RDS and its result read back from that database.

---

## 1. Pre-flight

| Step | Result | Evidence |
|---|---|---|
| DNS + TCP reachability | **PASS** | resolves → 13.234.197.96, TCP 5432 connect in 0.20 s |
| Authenticated connection | **PASS** | `PostgreSQL 18.3 on aarch64-unknown-linux-gnu`, db `jnpa_schema_v3`, user `postgres` |
| Target tables absent (no clobber risk) | **PASS** | all 10 new tables reported `ABSENT` before migrating |
| `core.scanner_machine` absent on PRODUCTION | **PASS** | confirms the client-clarification finding against the real DB, not just the repo |
| Destructive-statement scan of 0111-0114 | **PASS** | zero `DROP` / `TRUNCATE` / `DELETE` / `ALTER COLUMN TYPE` / `RENAME` |
| Lock-risk assessment | **PASS** | `core.pdp` 72 MB / 367 k rows (largest touched); `gate_event` 6.8 MB; `gate_capture` 360 kB |

**Baseline row counts captured for the preservation check:** pdp 367,078 · driver 31,846 · transporter 2,194 · cargo 19 · gate_event 47,374 · gate_capture 808 · cfs_ecy_movement 1,928 · rms_scan_container 98 · vehicle 40.

Safety guards used on every migration session: `lock_timeout = 15s`, `statement_timeout = 300s` — so a migration could never wedge production behind a lock.

---

## 2. Migration execution (sequential, stop-on-failure)

| Migration | Result | Duration | Verified |
|---|---|---|---|
| `0111_uc3_foundation.sql` | **PASS** | 3.15 s | 3 indexes on `core.pdp` created; 367,078 rows preserved; planner confirms `Index Scan using idx_pdp_number_accepted` |
| `0112_gate_documents.sql` | **PASS** | 0.08 s | `core.eir` (27 cols), `core.pin_ticket` (19), `gate_doc_import_file` (16), `gate_doc_import_error` (6); `tat_minutes` confirmed `is_generated=ALWAYS`; 2 Form-13 payload indexes on `core.gate_capture` |
| `0113_container_job.sql` | **PASS** | 0.16 s | 7 nullable columns added to `core.gate_event` with **all 47,374 rows preserved**; `container_job_assignment` + `container_job_event` + `cargo_movement_event` + `scanner_machine` + `scan_event` created |
| `0114_ecy_cfs_chain.sql` | **PASS** | 0.05 s | `core.ecy_cfs_chain` + 6 indexes + `chain_status` CHECK |

No migration failed; the stop-on-failure path was never triggered.

**Lock impact — WARNING (accepted, informational):** 0111 held a write lock on `core.pdp` for ~3 s while building indexes. Reads were unaffected and `core.pdp` is written only by the offline importer, so no live traffic was blocked. The other three migrations were sub-second. Adding columns to `core.gate_event` was metadata-only (PG 11+ semantics for nullable, no-default columns) — no table rewrite of the 47 k-row table.

---

## 3. Object verification (all 8 requested tables)

| Table | Exists | Indexes | Notes |
|---|---|---|---|
| `core.eir` | **PASS** | 5 | incl. partial `uq_eir_row_sha` |
| `core.pin_ticket` | **PASS** | 5 | incl. partial `uq_pin_row_sha` |
| `core.container_job_assignment` | **PASS** | 8 | incl. both double-assignment guards |
| `core.gate_event` | **PASS** | 3 | extended, not recreated |
| `core.cargo_movement_event` | **PASS** | 5 | |
| `core.scan_event` | **PASS** | 4 | |
| `core.scanner_machine` | **PASS** | 4 | |
| `core.ecy_cfs_chain` | **PASS** | 6 | |

**Constraints verified live (PASS):** `uq_job_open_vehicle` and `uq_job_open_container` (both partial unique, terminal states excluded); CHECKs on `move_type`, `status`, `document_type`, `movement_type`, `scan_event.result`, `machine_class`, `chain_status`; FKs `container_job_assignment.transporter_id → core.transporter(id)`, `cargo_movement_event.job_id`, `scan_event.job_id`, `scan_event.machine_code → core.scanner_machine`.

**Seed data (PASS):** all 4 client RMS machines present and active — `D-INNSA1RSDT01`, `D-INNSA1RSDT02` (DRIVE_THROUGH), `M-INNSA1SDMB01`, `M-INNSA1SDMB02` (MOBILE), customs house `INNSA1`.

**Existing data preserved (PASS):** re-checked after all migrations *and* after every validation write — all 9 baseline counts identical to the pre-flight numbers.

---

## 4. Defects found during validation, and fixed

All four were invisible to the unit suite because it runs against fake repositories; each appeared the first time the code met the real RDS schema.

| # | Defect | Impact | Fix | Re-verified |
|---|---|---|---|---|
| D1 | `GET /api/scan/status/{cn}` → **HTTP 500**: `column rc.igm_no does not exist` | Scanner routing endpoint completely broken in production | Read `igm_no` from the parent `core.rms_scan_report` via `report_id`; order by `(report_id, sl_no)` instead of the non-existent `rc.id` | **PASS** — returns `machine_code: D-INNSA1RSDT02`, `igm_no: 1191409` |
| D2 | `GET /api/customs/rms/{igm}/containers` → **HTTP 500**: same drift + `str` bound to a `bigint` parameter | Phase-1 endpoint broken | Same parent-join; bind `igm_no` as a real `int`; router 400s on non-numeric | **PASS** — 200 with rows; `?machine_type=D` filters; `abc` → 400 `invalid_igm_no` |
| D3 | **Gate-document upload silently imported 0 rows** while reporting the file as valid: `ON CONFLICT (row_sha256)` cannot infer a **partial** unique index | EIR/PIN ingestion non-functional | Repeat the index predicate: `ON CONFLICT (row_sha256) WHERE row_sha256 IS NOT NULL` | **PASS** — 3/3 EIR rows imported |
| D4 | A failed upload **poisoned its own sha256**, so retrying the corrected file returned `SKIPPED_DUPLICATE` forever; and DB row-errors were counted but their detail never persisted | File could never be loaded after one failure; operator saw a count with no reason | Reuse/reset a ledger row that imported nothing; persist `row_errors` | **PASS** — retry imported 3 rows; a further retry correctly returned `SKIPPED_DUPLICATE` |

**Root cause common to D1/D2 — schema drift (WARNING, ongoing):** the deployed `core.rms_scan_container` carries only the base 7 columns (`report_id, sl_no, container_no, machine_type, scan_location, cfs_name, goods_desc`). Migration **0102's extension columns (`id`, `igm_no`, `iso_valid`, `created_at`) were never applied to this RDS** — consistent with the known partial 0102 application (earlier hotfixes covered only the transporter/vehicle and driver/pdp sections). The code now works against **both** schema variants, but other modules may still assume the un-applied columns. Recommend a full 0102-vs-RDS column audit before go-live.

Two regression tests were added (`tests/test_gate_documents.py`) that assert the ON CONFLICT predicate and the drift-safe RMS query, so these specific defects cannot silently return.

---

## 5. API validation (live gateway → live RDS)

Gateway run against the RDS DSN with `AUTH_ENABLED=false`, `JNPA_RUNTIME_DDL=0`. Every endpoint called and its response body read.

| Endpoint | Result |
|---|---|
| `GET /api/gate-docs/summary` · `/eir` · `/pin` · `/form13` · `/tat` · `/container/{cn}` · `/truck/{no}` · `/uploads` | **PASS** (8/8 HTTP 200) |
| `POST /api/gate-docs/validate` · `/upload` | **PASS** — validate previewed TAT 165; upload persisted 3 rows; re-upload `SKIPPED_DUPLICATE` |
| `GET /api/jobs` · `GET /api/jobs/{id}` · `GET /api/cargo-jobs/container/{cn}` | **PASS** (incl. correct 404s) |
| `POST /api/jobs` · `/validate` · `/{id}/accept` · `/complete` · `/cancel` | **PASS** |
| `POST|GET /api/gate/events` | **PASS** — writes into the extended `core.gate_event` |
| `POST|GET /api/yard/movements` | **PASS** |
| `GET /api/scan/machines` · `/status/{cn}` · `POST|GET /api/scan/events` | **PASS** (after D1 fix) |
| `GET /api/customs/rms/{igm}/containers` | **PASS** (after D2 fix) |
| `POST /api/cfs-ecy/chains/rebuild` · `GET /chains` · `/chains/stats` · `/chains/{cn}` · `?anomaly_only` · `?anomaly_code=` | **PASS** |
| `GET /api/driver/jobs` · `/{id}` (+ 404 for a foreign id) | **PASS** |

---

## 6. Business-rule validation against real production data

Fixtures were selected **from the live database**, not invented.

| Rule | Input (live data) | Result |
|---|---|---|
| Cancelled PDP permit refused | driver `UP58 20030002331`, permit `PDP2026/341311/1` (`active=false`) | **PASS** — 400 `pdp_inactive`, names the permit |
| Unknown vehicle refused | `TRK-999999` | **PASS** — 400 `vehicle_not_found` |
| Invalid ISO-6346 refused | `ABCU1234567` | **PASS** — 400 `invalid_container_number` |
| Valid assignment accepted | container `MAEU6123458`, vehicle `TRK-000002` (ACTIVE), driver `UP51 20140005551` (permit `PDP2023/2154/13` valid to 2026-08-23) | **PASS** — job #1 created, 5 checks returned |
| Double-assignment blocked | same truck, second container | **PASS** — 400 `vehicle_already_assigned … open job #1` |

---

## 7. End-to-end lifecycle → database validation chain

One full trip driven through the API; every link verified by reading the database.

| Chain link | Verified in DB |
|---|---|
| **Row created** | `core.container_job_assignment` id=1 → `COMPLETED`, accepted + completed timestamps set |
| **Audit created** | `core.container_job_event` — 7 append-only rows |
| **History created** | full transition chain: `job.assigned → job.accepted → job.gate_in → job.in_yard → job.yard_pickup → job.scan_recorded → job.gate_out`, each with old→new status and JSON detail |
| **Lifecycle updated** | job status advanced `ASSIGNED → ACCEPTED → AT_GATE → IN_YARD → PICKED_UP → COMPLETED`; `core.cargo.customs_status` for `MAEU6123458` updated by the clean scan |
| **Gate rows** | `core.gate_event` 47375 (`GATE_IN`, **BAT lane `D391`**, doc `PIN/DEPLOY-VALIDATION-1`, `source=API`, `trip_id=JOB-1`) and 47376 (`GATE_OUT`) |
| **Yard row** | `core.cargo_movement_event` — `YARD_PICKUP`, yard `2P08D.1` (**the real PIN format survived verbatim**, 7 chars) |
| **Scan row** | `core.scan_event` — `D-INNSA1RSDT02` → joins `core.scanner_machine` as `DRIVE_THROUGH`, result `SCANNED_CLEAN` |
| **Event generated** | **PASS with WARNING** — lifecycle events fire and are logged; Kafka fan-out was **not** exercised because no broker is reachable from this host. The bus degrades to WebSocket-only by design. See §10. |

---

## 8. ECY→CFS chain against the real 1,928 CODECO rows

| Check | Result |
|---|---|
| Rebuild executed | **PASS** — 1,202 chains in 1.64 s |
| **242 COMPLETE chains** | **PASS** — exactly the "242 containers ✓" the client document states |
| Hero `ONEU2122848` | **PASS** — ECY-out 01/07 10:00 IST → CFS-in 14:00 IST → CFS-out 07/07 08:16 IST; **transit exactly 4.00 h**, dwell 138.27 h, cycle 142.27 h — matches the document |
| **Planted anomaly `COSU4663595` detected** | **PASS** — flagged `MULTI_OUT` with `cfs_out_count: 2`; the audit's silent-absorption gap is closed |
| Cycle KPIs | **PASS** — avg transit 3.40 h, avg dwell 71.04 h, avg cycle 94.78 h, median 94.14 h |

**Anomaly totals need context (WARNING, not a defect):** 529 chains carry a flag — 287 `NO_CFS_IN` and 241 `ORPHAN_CFS_IN`. This is the shape of the source data, not a fault: the ECY file holds 961 containers appearing once each while the CFS file holds 484 in / 484 out, so only 242 boxes appear in both. Present it that way in the demo.

**`DUPLICATE_IN` is undetectable post-ingest (WARNING):** the client document records COSU4663595 as having a duplicated CFS-In *and* two CFS-Outs. Only `MULTI_OUT` can be detected, because the duplicate IN was already collapsed at ingest by `uq_cfs_ecy_movement` before the chain layer ever sees it. Detecting it would require flagging at import time.

---

## 9. Web & Driver PWA

| Item | Result |
|---|---|
| Web `tsc --noEmit` | **PASS** (exit 0) |
| PWA `tsc --noEmit` | **PASS** (exit 0) |
| `prettier --check` (CI command) | **PASS** — all files compliant |
| Backend APIs behind every UC-3 Lifecycle tab (Lifecycle / Documents / Chains / Upload) | **PASS** — validated individually in §5 |
| Driver PWA job endpoints (list, detail, accept, gate-arrival, pickup, drop, complete) | **PASS at API level** |
| **Browser-rendered UI walkthrough** | **NOT VERIFIED** — see §10 |
| **PWA driver login (device-token) end-to-end** | **NOT VERIFIED** — see §10 |

---

## 10. Not verified — must be closed before the client demo

These are stated plainly rather than assumed. None is a code defect; each is an environment limit of this validation run.

1. **NOT VERIFIED — browser UI.** The web app was typechecked and its APIs proven, but no page was rendered in a browser and no click-path was exercised. Run `make up` (or the Vite dev server) against the RDS DSN and walk: search → assign → gate-in → yard → scan → gate-out, plus the Chains tab.
2. **NOT VERIFIED — driver PWA on a device.** Login is Vehicle-ID/device-token based; with `AUTH_ENABLED=false` the job list correctly reported `scope: "support"` rather than driver scope. The per-driver ownership rule (404 for a foreign job id) is unit-tested but **was not exercised with a real DRIVER token**. Verify with auth enabled before demo.
3. **NOT VERIFIED — Kafka fan-out.** No broker is reachable from this host, so `jnpa.uc3.lifecycle` publication was never observed on the wire. The DB writes and WS path are unaffected (the bus is best-effort by design).
4. **WARNING — schema drift beyond RMS.** 0102's extension columns are partially applied on this RDS. Audit every module's SQL against the real column list before go-live.
5. **WARNING — redundant index.** `idx_pdp_appl` duplicates the pre-existing `pdp_appl_number_idx` (which was **not** visible in `infra/postgres/v3/`, so the earlier audit's "core.pdp has no indexes" claim was too strong — the live DB already had `pdp_pkey`, `pdp_appl_number_idx`, `pdp_pdp_number_idx`). `idx_pdp_appl` is safe but costs write overhead; consider dropping it in a maintenance window. **No index was dropped during this deployment.**
6. **WARNING — Form-13 has no live rows yet.** `core.gate_capture` holds 808 pre-existing `sim` rows; the module reads only `source_mode='live'`, so `/api/gate-docs/form13` correctly returns empty until a Form-13 file is uploaded.

---

## 11. Data written during validation

No existing row was modified, deleted, renamed or retyped. The following **new** rows now exist and should be removed or kept deliberately before the demo:

| Table | Rows | Nature |
|---|---|---|
| `core.scanner_machine` | 4 | **Keep** — reference master seeded by migration 0113 |
| `core.ecy_cfs_chain` | 1,202 | **Keep** — derived from real CODECO data; regenerate any time via `POST /api/cfs-ecy/chains/rebuild` |
| `core.eir` | 3 | Test EIRs (`E-GTI-1`, `E-GTI-2`, `E-NOCNTR`) — delete if unwanted |
| `core.gate_doc_import_file` | 1 | Ledger row for `eir.csv` |
| `core.container_job_assignment` | 1 | Job #1 (`MAEU6123458` / `TRK-000002`), COMPLETED |
| `core.container_job_event` | 7 | Its audit history |
| `core.cargo_movement_event` | 1 | Its yard pickup |
| `core.scan_event` | 1 | Its scan |
| `core.gate_event` | 2 | ids 47375/47376, `source='API'` (baseline 47,374 untouched) |
| `core.cargo` | 0 new | `MAEU6123458.customs_status` was set to `PENDING` by the clean scan — a **field update on an existing row**, expected lifecycle behaviour |

I did not delete any of these, since removing rows from a production-like database was outside the authorisation given.

---

## 12. Code changes made during deployment

| File | Change |
|---|---|
| `services/container_job/repository.py` | D1 — drift-safe RMS selection query |
| `services/customs/repository.py` | D2 — parent-join + int binding for RMS containers |
| `gateway/routers/customs.py` | D2 — 400 on non-numeric `igm_no` |
| `services/gate_documents/repository.py` | D3 + D4 — partial-index ON CONFLICT predicate; retry of a zero-import ledger row |
| `services/gate_documents/service.py` | D4 — persist DB-level row errors |
| `tests/test_gate_documents.py` | 2 regression tests for D1 and D3 |

**Regression suite after all fixes: 115 passed, 1 skipped** (plus the 2 new tests → 19 in the gate-documents file).

---

## 13. Verdict

| Area | Status |
|---|---|
| Migrations 0111–0114 | **PASS** |
| 8 requested tables + indexes + constraints + seed | **PASS** |
| Existing production data preserved | **PASS** |
| Backward compatibility (additive only) | **PASS** |
| API layer | **PASS** (after 4 fixes) |
| Business rules vs real data | **PASS** |
| Lifecycle → DB → audit → history → event chain | **PASS** (event bus: DB/WS verified, Kafka not observed) |
| ECY→CFS chain vs client document | **PASS** |
| Web / PWA build + format | **PASS** |
| Browser UI + PWA device walkthrough | **NOT VERIFIED** |
| **BLOCKERS** | **NONE** |
