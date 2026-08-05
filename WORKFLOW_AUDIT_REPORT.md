# JNPA Digital Twin — Workflow Validation Report

**Date:** 2026-08-05
**Scope:** Audit + end-to-end validation of the five existing business workflows.
**Role:** Senior QA + Backend Engineer (read-only audit — no business logic was modified).
**Database:** AWS RDS PostgreSQL `jnpa_schema_v3` (the live application DB — every
DB assertion below is against real data, not fixtures).
**Companion document:** [WORKFLOW_TEST_RESULTS.md](WORKFLOW_TEST_RESULTS.md) — commands, raw output, failed scenarios.

---

## 1. Executive summary

| # | Workflow | Status | Steps passed |
|---|----------|--------|--------------|
| 1 | Cargo Lifecycle | **PASS** | 33/33 |
| 2 | Transport | **PARTIAL** | 20/26 (4 env, 2 defects) |
| 3 | CFS-ECY | **PASS** (1 reporting defect) | 23/24 |
| 4 | Gate Document | **PARTIAL** | 34/36 |
| 5 | Evidence / Demo Readiness | **PASS** | 45/45 |

**Existing automated suite: 1,056 passed / 114 skipped / 0 failed.**

Bottom line: the *business logic* is in good shape. The cargo lifecycle state
machine in particular is correct and well guarded — every illegal transition I
attempted was rejected with a precise 409. The defects found are concentrated in
**response reporting** (APIs that report success for work that did not happen)
and in **resilience of one dependency-coupled write path**. The single largest
*process* risk is that **no automated test currently runs against the real
database** (§6.1).

---

## 2. Architecture as-built

| Layer | Location | Notes |
|---|---|---|
| API gateway | [gateway/main.py](gateway/main.py), 68 routers in [gateway/routers/](gateway/routers/) | FastAPI, **421 routes**; auth is flag-gated (`AUTH_ENABLED`) |
| Services | 23 packages in [services/](services/) | Orchestration + typed errors; no SQL |
| Repositories | `services/*/repository.py` | Raw SQL over the cached async SQLAlchemy engine |
| Migrations | [infra/postgres/v3/](infra/postgres/v3/) `0100`–`0203` | Ledger in `core.schema_migrations`; runner `scripts/migrate.py` |
| Schemas | `core` (197 tables), `mart` (20 views), `jnpa` (2 legacy), `staging` | `core` is the live schema; `jnpa` is vestigial |
| Tests | 89 files in [tests/](tests/) + [tests/e2e/](tests/e2e/) | TestClient + in-memory fake repos via `dependency_overrides` |

The layering (router → service → repository) is consistently applied and was easy
to audit. Error mapping is centralised and correct: `/api/cargo` and `/api/fastag`
convert validation failures to 400 via a shared handler in `gateway/main.py:596`.

---

## 3. Workflow 1 — Cargo Lifecycle · **PASS** (33/33)

`Create → Movement → Discharge → Yard assign → Yard position → Planning → Scan pending → Verification → Release`

**APIs tested (17):** `POST/GET/PUT/DELETE /api/cargo`, `/api/cargo/{cn}/discharge`,
`/yard-assignment`, `/yard-position`, `/verify`, `/release`, `/lifecycle`,
`/workflow`, `/workflow/history`, `/api/cargo/events`, `/scan-queue`,
`/yard-planning`, `/reefer-planning`, `/rake-planning`, `/yard-optimization`,
`/notifications`.

**DB tables verified:** `core.cargo`, `core.cargo_lifecycle_event`,
`core.cargo_event`, `core.cargo_scan_verification`, `core.cargo_workflow_event`,
`core.cargo_yard_plan`, `core.cargo_reefer_plan`, `core.cargo_rake_plan`,
`core.cargo_notification`.

**Verified working:**
- Full 8-state walk persisted end-to-end; final DB state `lifecycle_status=RELEASED, is_released=true`.
- **8 lifecycle rows** written in exact order: `CREATED → VESSEL_DISCHARGED → YARD_ASSIGNED → YARD_POSITION_ALLOCATED → REEFER_PLANNED → RAKE_ASSIGNED → VERIFIED → RELEASED`.
- **19 event rows** across 11 topics on `core.cargo_event`, readable via `GET /api/cargo/events`.
- **Error handling is genuinely strong.** Every negative case returned the right code with a structured body:
  - release-before-verify → 409 `illegal_transition` (`current_status: CREATED`)
  - verify-before-yard-assign → 409
  - duplicate discharge / duplicate release → 409
  - duplicate create → 409, bad ISO-6346 check digit → 400, malformed yard block → 400
  - unknown container on GET and on discharge → 404
- Pagination header `X-Total-Count: 11942` present.

### Issues found

**C-1 · `cargo.verified` is emitted for a FAILED scan · Medium**
[services/cargo/service.py:484](services/cargo/service.py#L484) emits `EVENT_VERIFIED`
unconditionally, including when `verified=false`. `EVENT_VERIFIED` is in `_BUS_EVENTS`
([service.py:59](services/cargo/service.py#L59)), so it is distributed over Kafka **and**
WebSocket. A rejected customs scan therefore broadcasts on the same topic as a passed
one; only the `payload.verified` boolean distinguishes them. Any consumer subscribing by
topic will count a failed scan as a verification.
*Fix:* emit a distinct topic (e.g. `cargo.scan_rejected`) on the `verified=false` branch,
or exclude the rejected case from `_BUS_EVENTS`.

**C-2 · `POST /verify {verified:false}` returns a lifecycle it never set · Low (contract)**
The router's fallback declares `SCAN_PENDING`
([gateway/routers/cargo.py:1130](gateway/routers/cargo.py#L1130)) and the OpenAPI example
agrees, but the service does not transition on this branch — it re-reads the row
([service.py:479](services/cargo/service.py#L479)). Observed: the API returned
`lifecycle_status: "RAKE_ASSIGNED"`. `SCAN_PENDING` is *by design* a derived queue label
(documented at [service.py:69](services/cargo/service.py#L69)) and is never persisted to
`core.cargo`, so the code is right and the **declared contract is wrong**.
*Fix:* correct the response model/summary so the field is not advertised as `SCAN_PENDING`.

**C-3 · Yard planning does not reconcile with yard assignment · Low**
`POST /api/cargo/yard-planning` returned `assigned_block: "B-10"` for a container whose
actual `core.cargo.yard_block` was `B-07`; the plan never reaches the cargo row and
`POST /release` later reported `yard_location: "B-07"`. The two surfaces disagree with no
error. Acceptable if plans are advisory, but it will read as a data bug on screen.

---

## 4. Workflow 2 — Transport · **PARTIAL** (20/26)

`Truck creation → Device mapping → Telemetry → Route/trip → Gate movement → Traffic/congestion`

**APIs tested (17):** `/api/vehicles` (POST, GET, `/stats`, `/available`),
`/api/trucks` (list, `/{id}`, `/{id}/route`, `/route/latest`, `/route/ack`),
`/checkin`, `/api/gate/events` (POST, GET), `/api/traffic/{current,health,metrics,congestion/metrics,snapshots,predict}`,
`/api/traffic/congestion-scan`, `/api/alerts`.

**DB tables verified:** `core.vehicle`, `core.reroute_advisory`, `core.gate_event`,
`core.traffic_snapshot`, `core.alert`, `core.decision_audit`.

**Verified working:**
- Vehicle registry create + dedup: duplicate plate → 409 `vehicle_number_exists` with the existing `vehicle_id`. Row confirmed in `core.vehicle` (`TRK-000031`). Stats `{total:41, active:39, assigned:3, available:36}`.
- **The documented PRIMARY→SECONDARY→TERTIARY fallback works.** With the truck-sim down, `GET /api/trucks/{id}` correctly 404'd `no_position`; after a `/checkin` submission the same call returned `decision_path: TERTIARY` with `gate_boom_delay_s: 5`, and an `ELEVATED_SCRUTINY` alert was raised in `core.alert`. This is exactly the behaviour the module documents.
- Gate movement persisted: `POST /api/gate/events` → 201, row id `213337` in `core.gate_event`, queryable by plate. The table holds **213k+ real rows**.
- Traffic is genuinely LIVE: `/api/traffic/current` → `{status: LIVE, source: TOMTOM}`.

### Issues found

**T-1 · Re-route advisory is lost entirely when the truck-sim is unreachable · High**
[gateway/routers/trucks.py:209-217](gateway/routers/trucks.py#L209-L217) calls the
truck-sim **first** and raises 502 on `httpx.HTTPError`. Everything downstream is
therefore skipped: the decision audit record, the `LAST_REROUTE` hot cache, the durable
write to `core.reroute_advisory` (migration 0115), and the WebSocket + WebPush + FCM
fan-out. Observed: `POST /api/trucks/TRUCK-0001/route` → 502 `truck_sim_unreachable`,
`core.reroute_advisory` rows = **0**.
The driver-advisory push is a headline UC-3 flow and it has a hard, undegradable
dependency on a simulator — in a router that implements a three-rung fallback for
*reads* on the very next endpoint.
*Fix:* persist the advisory and dispatch to the driver before/independently of the sim
call; downgrade the sim failure to a partial-success field in the response.

**T-2 · ACK reports success for an advisory that does not exist · Medium**
[services/advisory/repository.py:82](services/advisory/repository.py#L82) returns
`bool(res.rowcount)`, but the router discards it and always answers
`{"acked": true}` ([trucks.py:305](gateway/routers/trucks.py#L305)). It also writes a
`REROUTE_ACK` decision-audit record regardless.
Observed on a device with zero advisories:
`POST /api/trucks/QA-GHOST-DEVICE-0001/route/ack` → `200 {"acked":true,"state":"ACK"}`,
`core.reroute_advisory` rows = 0.
This fabricates a push→driver→ACK round-trip in the evidence trail that never occurred.
*Fix:* return 404 (or `acked:false`) when `rowcount == 0`, and only write the audit record on a real update.

**T-3 · `trip_id` derived inconsistently on gate events · Low**
The event created via the API got `trip_id = "MH04QA9911"` (the bare plate) whereas
ingested rows use `"TRK-002458:2"` (device:sequence). Trip-level joins/TAT
calculations will not group API-created events with ingested ones.

### Environment-limited (not defects)
Docker was not running on the audit host, so container-hosted dependencies were down.
These are **infrastructure gaps in the test environment**, and each degraded exactly as documented:
- `truck-sim` unreachable → `/api/trucks` returned `{count:0, degraded:true}`; no `device_id` resolvable (steps 2b, 4).
- `ai/congestion` unreachable → `/api/traffic/metrics` and `/api/traffic/congestion/metrics` → 503 `congestion_metrics_unavailable` (documented degradation).

---

## 5. Workflow 3 — CFS-ECY · **PASS** (23/24, 1 reporting defect)

`Container → CFS entry → ECY movement → Container status update → Exit/release`

**APIs tested (12):** `/api/cfs-ecy/movements` (+ facility/mode filters), `/stats`,
`/dwell`, `/containers/{cn}`, `/chains`, `/chains/stats`, `/chains/{cn}`,
`/chains/rebuild`, `/templates/{facility}`, `/validate`, `/upload`, `/uploads`.

**DB tables verified:** `core.cfs_ecy_movement` (CFS 967 / ECY 961 = **1,928 rows**),
`core.ecy_cfs_chain` (**1,202 chains**), `core.cfs_ecy_import_file`, `core.cfs_ecy_import_error`.

**Verified working:**
- All movement filters return data; unknown container → 404 `container_not_found`; invalid facility → 400 `invalid_facility`.
- KPIs computed over real data: `total_in 915 / total_out 1013 / containers 1202 / avg dwell 71.04h / median 68.17h`.
- Chain spine intact: 1,202 chains, 242 complete, 528 partial, 529 anomalies with codes (`NO_CFS_IN` ×287). `POST /chains/rebuild` recomputed all 1,202 in 309 ms.
- Upload pipeline correct: template → validate (dry-run) → import → ledger. Malformed rows produce `status: REJECTED` with per-row `row_number`/`column_name`/`error_code`.
- **Idempotency is genuinely correct at the DB level** (verified separately, [WORKFLOW_TEST_RESULTS.md §4](WORKFLOW_TEST_RESULTS.md)): re-uploading identical content — even under a different filename — inserted **0** additional rows (content-hash based, `status: SKIPPED_DUPLICATE`).

### Issues found

**E-1 · Duplicate upload reports `imported: N` while importing nothing · Medium**
*(same defect class in Workflow 4 — see G-1)*
On the duplicate re-upload the response was
`{"status":"SKIPPED_DUPLICATE", "duplicate_file":true, "imported":2}` while the DB row
count was unchanged. `imported` echoes the *parsed* count, not the *persisted* count. An
operator or UI reading `imported` will believe two more rows landed.
*Fix:* set `imported: 0` when `status == SKIPPED_DUPLICATE`.

**E-2 · Module emits no workflow events · Low (by design, but a gap vs. the brief)**
There is no event emission anywhere in [services/cfs_ecy/](services/cfs_ecy/) — no
`lifecycle_bus`, no `digital_twin_event` write. Confirmed: 0 CFS-related rows in
`core.digital_twin_event`. The module is documented as read-only + import, so this is
consistent with its design, but the "workflow events" this audit was asked to verify
**do not exist** for CFS-ECY. Cross-module correlation (e.g. a CFS gate-out triggering a
cargo lifecycle step) is not wired.

---

## 6. Workflow 4 — Gate Document · **PARTIAL** (34/36)

`Gate document upload → OCR/EIR processing → Document validation → Evidence generation`

**APIs tested (16):** `/api/gate-docs/{summary,templates/{type},validate,upload,uploads,eir,pin,form13,tat,container/{cn},truck/{tn}}`,
`/api/ocr/{health,document,documents,documents/{id},documents/{id}/verify}`, `/api/reports/police`.

**DB tables verified:** `core.gate_document` (13), `core.eir` (5), `core.pin_ticket` (2),
`core.form11` (3), `core.gate_doc_import_file`, `core.document_ocr`, `core.gate_capture` (808).

**Verified working:**
- Templates for all three doc types (EIR / PIN / FORM13) return correct headers; invalid type → 400 with the allowed list.
- Validate correctly accepts the template's own example row (`valid:true`) and rejects a malformed one (`valid:false`, `error_code: empty_required`).
- Import + **DB-level idempotency confirmed** (`13 → 13` rows on re-import, `duplicate_file: true`).
- Reads all correct: EIR 5, PIN 2, Form13 202, TAT aggregates (`avg 137 min, median 165 min`, by-terminal breakdown), per-container and per-truck joins.
- OCR round-trip works: upload → `EXTRACTED` (id 2) → `POST /verify` with corrections → `VERIFIED`; the operator's fields were merged into the `fields` jsonb, confirmed in `core.document_ocr`. Unknown doc id → 404 on both read and verify.
- `/api/ocr/health` honestly reports the degraded rung: `upstream.reachable: false`, `active_rung: OCR`.

### Issues found

**G-1 · Duplicate upload reports `imported: 1` while importing nothing · Medium**
Identical to E-1, on `/api/gate-docs/upload`. Observed:
`{"status":"SKIPPED_DUPLICATE","imported":1}` with `core.gate_document` unchanged at 13.
Both upload services share this reporting flaw — fix once, in both
[services/gate_documents/service.py](services/gate_documents/service.py) and
[services/cfs_ecy/upload_service.py](services/cfs_ecy/upload_service.py).

**G-2 · Evidence generation is not linked to gate captures · Medium**
`core.gate_capture` holds 808 rows, but **no row carries an object-storage reference**.
Sample payload:
`{"form13_no":"F13758233896","cargo_desc":"READYMADE GARMENTS","gross_wt_kg":20621,"container_no":"MSCU0160690","shipping_bill_no":"9686631"}`
— document metadata only, no `object_path` / `object_name` / `evidence_uri`. The
`/api/evidence/{object_path}` surface therefore has nothing in the DB to point it at, so
the upload → OCR → **evidence artefact** leg of this workflow cannot be demonstrated
from stored data. Separately, `POST /api/ocr/document` returned `storage_url: null`
(MinIO down in this environment), so newly uploaded documents also produce no evidence object.

**G-3 · MOCK OCR reports 0.75 confidence with zero extracted fields · Low**
With `eir-ocr` unreachable the endpoint returned
`{"source":"MOCK","confidence":0.75,"fields":{},"status":"EXTRACTED"}`. A hard-coded
0.75 on an extraction that produced **no fields at all**, under status `EXTRACTED`, will
mislead any downstream confidence threshold or reviewer queue.
*Fix:* report `confidence: 0` (or null) when `source == MOCK` / `fields` is empty.

---

## 7. Workflow 5 — Evidence / Demo Readiness · **PASS** (45/45)

**44 dashboard/report endpoints swept + Follow-the-Box journey. All returned 200.**

Confirmed data-backed (not placeholder):
`core.gate_event` 213k+ · `core.cargo` 11,942 · IGM containers 12,235 · advance containers 8,878 ·
CFS-ECY 1,928 movements / 1,202 chains · Form13 202 · `core.gate_capture` 808 ·
marine calls, parking facilities (`decision_path: RDS_DIRECT`), transporters, export bookings,
scan machines, workflow rules + executions, scenarios — all populated.

Live integrations confirmed up: **TomTom traffic** (`status: LIVE`), Open-Meteo weather,
OpenAQ air quality, ULIP logistics.

**Demo verdict: ready.** Screenshots are possible for every major screen. Two panels
will render empty:
- `/api/kpi/sources` → `{"sources":[],"count":0}`
- `/api/reefer/availability` → all zeros, `facilities: []`

Note the KPI/ANPR camera strip reports `decision_path: SYNTHETIC` for all cameras — correct
given no live camera feed, but worth stating aloud in a demo rather than letting it be read as live.

**Route-shadowing caution:** `GET /api/berthing/summary` returns **422** (not 404) because
`/api/berthing/{report_id}` captures the literal string. Harmless today, but the same
static-before-parameter ordering discipline that [gateway/routers/cargo.py:33](gateway/routers/cargo.py#L33)
documents should be applied in [gateway/routers/berthing.py](gateway/routers/berthing.py).

---

## 8. Test-coverage findings

### 8.1 · No automated test runs against the real database · **High (process)**
All 10 DB-integration test files gate on **`localhost:5433`** — the local sandbox
Postgres, which is now an opt-in `localdb` compose profile after the RDS-only migration.
The result: those tests **silently skip** rather than fail. 114 of 1,170 tests skipped,
7 of them explicitly `"Postgres not reachable on 5433"`.

Affected: `test_cargo.py`, `test_cfs_ecy.py`, `test_customs_adapter.py`,
`test_customs_repository.py`, `test_gateway.py`, `test_performance.py`,
`test_performance_upload.py`, `test_rfid_ingest.py`, `test_trucking_app.py`, `test_vahan_sim.py`.

Every real defect in this report (T-1, T-2, E-1, G-1, G-2) was found by running against
RDS — none is reachable through the fake-repo tests that make up the green suite.
*Fix:* gate the integration tests on the configured `POSTGRES_DSN` instead of a hardcoded
host:port, so they run against whichever DB is configured and skip only when none is set.

### 8.2 · Missing workflow tests
No end-to-end test exists for:
- the transport chain (vehicle → device → telemetry → route → ack → gate event);
- the re-route advisory persistence/ACK contract (would have caught T-1 and T-2);
- upload **response** correctness on the duplicate path (would have caught E-1/G-1);
- gate-document → OCR → evidence artefact continuity (G-2).

### 8.3 · Whole-suite run aborts natively
`pytest shared tests` dies at `test_performance` on this host (pre-existing, environment-specific).
Run in file batches — see [WORKFLOW_TEST_RESULTS.md §1](WORKFLOW_TEST_RESULTS.md).

---

## 9. Required fixes — prioritised

| ID | Severity | Fix | Location |
|----|----------|-----|----------|
| **T-1** | High | Persist + dispatch the re-route advisory independently of the truck-sim call; degrade the sim failure to a response field | [gateway/routers/trucks.py:209](gateway/routers/trucks.py#L209) |
| **8.1** | High | Gate DB-integration tests on `POSTGRES_DSN`, not `localhost:5433` | 10 files in [tests/](tests/) |
| **T-2** | Medium | Honour `AdvisoryRepository.ack()`'s return value: 404/`acked:false` when nothing was updated; don't audit a phantom ACK | [gateway/routers/trucks.py:305](gateway/routers/trucks.py#L305) |
| **E-1 / G-1** | Medium | Report `imported: 0` on `SKIPPED_DUPLICATE` | [services/cfs_ecy/upload_service.py](services/cfs_ecy/upload_service.py), [services/gate_documents/service.py](services/gate_documents/service.py) |
| **C-1** | Medium | Distinct topic for a rejected scan; don't publish it as `cargo.verified` | [services/cargo/service.py:484](services/cargo/service.py#L484) |
| **G-2** | Medium | Store an object reference on `core.gate_capture` so evidence retrieval has a target | gate-capture writer + [gateway/routers/evidence.py](gateway/routers/evidence.py) |
| **C-2** | Low | Response model must not advertise `SCAN_PENDING` on the `verified:false` branch | [gateway/routers/cargo.py:1130](gateway/routers/cargo.py#L1130) |
| **G-3** | Low | `confidence: 0`/null when OCR source is MOCK or fields are empty | [gateway/routers/document_ocr.py](gateway/routers/document_ocr.py) |
| **T-3** | Low | Consistent `trip_id` derivation between API-created and ingested gate events | [gateway/routers/container_job.py:248](gateway/routers/container_job.py#L248) |
| **C-3** | Low | Reconcile yard plan with actual `yard_block`, or label plans clearly as advisory | [services/cargo/service.py](services/cargo/service.py) |
| **E-2** | Low | Decide whether CFS-ECY should emit lifecycle events for cross-module correlation | [services/cfs_ecy/](services/cfs_ecy/) |
| **§7** | Low | Declare static `/summary`-style routes before `/{param}` in the berthing router | [gateway/routers/berthing.py](gateway/routers/berthing.py) |

---

## 10. Audit method & caveats

- The **real gateway FastAPI app** was booted in-process (Starlette `TestClient`, lifespan on) against the **real RDS**, reusing the existing test framework. No mocks or fake repositories were used for the workflow runs.
- Kafka / MQTT / Redis were disabled for the harness so the app took its documented degraded paths instead of blocking on connect; the Docker daemon was not running on the audit host, so `truck-sim`, `ai/congestion`, `eir-ocr` and MinIO were genuinely unreachable. Findings arising from that are labelled *environment-limited* and separated from defects.
- Writes were confined to purpose-made QA records (containers `QATU…`, plate `MH04QA9911`, device `QA-GHOST-DEVICE-0001`); the idempotency probe rows were deleted afterwards. **No business logic, migration or existing record was modified.**
- Steps that failed on my own malformed payloads were corrected and re-run; only genuine findings are reported above.
