# UC-III Lifecycle Backend Audit Report

**Date:** 2026-07-31 · **Branch:** `migrate-schema-v3` · **Auditor role:** Principal Solution Architect / Senior Backend Engineer / Domain Expert / Software Auditor
**Source of truth:** client documents `06_Truck_Gate_Lifecycle_UC3.md` (F-U3) and `07_Empty_ECY_CFS_Lifecycle.md` (F-Y1)
**Method:** six parallel deep-read audits over the full repo (gateway, services, migrations legacy + v3, shared DTOs, scenarios, ingest, tests, web, mobile-pwa). Every claim below is code-cited. Items that could not be verified are marked **NOT FOUND** — nothing is guessed.

---

## 1. Executive Summary

The backend is a **broad but siloed** implementation. Master data (transporters, drivers, PDP ledger, fleet), document ingestion (customs IGM/OOC/SMTP/RMS/LEO/SB, shipping-lines IAL/EAL/EDO-CODECO, CFS-ECY CODECO), and a real container lifecycle state machine (migration 0023) all exist and are largely production-shaped (upload ledgers, sha256 dedup, idempotent imports, RBAC on most prefixes).

What is **missing is precisely the connective tissue the two client lifecycles describe**:

1. **EIR does not exist anywhere in the repo** — `grep -rniw eir` returns zero hits. No table, parser, endpoint, or DTO. The corpus's flagship artifacts (eir1–eir4, TruckIn/TruckOut TAT, BAT lanes, scanner stamps, To/From CFS) have no home.
2. **PIN tickets, BAT lanes, dual-move trips, and corridor-exit are unmodelled** (zero code references).
3. **There is no truck↔container job assignment.** The only link is the free-text `core.cargo.vehicle_number` column — no driver, no validation, no job entity, no double-assignment guard.
4. **Gate events are simulator-only.** `core.gate_event` has exactly one writer (`ingest/trucking_app/.../sinks.py:302`). The `GateTransaction` DTO and `gate.transactions` Kafka topic exist but are never produced or consumed. No HTTP path records a real gate-in/gate-out.
5. **The ECY→CFS chain is never materialized.** The 1,928 CODECO rows are stored flat; dwell is computed CFS-only per facility; no ECY-Out→CFS-In linkage, no road leg, no cycle time, no terminal-export handover, and the planted COSU4663595 anomaly is silently absorbed (one duplicate IN dropped, the second OUT retained and *biasing dwell by +2 h with no flag*).
6. **RMS scanner routing stops at a container flag.** Selection lists parse correctly, `rms_selected` can drive `customs_status='UNDER_INSPECTION'` — but reconcile is manual-only, there is no scanner-machine master, no scan-done event/status, no truck→scanner link, and gate-exit logic never branches on inspection state.
7. **Cross-domain joins are absent by construction:** GatePass (EDO) never joins gate events/TRT/trips/cargo; transporter names on gate docs (TRANSTAR/TRANSTA) have no bridge to the transporter master; FASTag lanes and geofence events never join a gate journey; `core.cfs_ecy_movement` and `core.empty_container_*` are two disconnected islands.

Overall verdict: **~55–60 % of the UC-III lifecycle surface exists as isolated modules; ~0 % of it is connected end-to-end.** The remaining work is dominated by (a) three new document entities (EIR, PIN, real Form-13), (b) one new job-assignment entity, (c) join/bridge logic, and (d) event distribution — not by rework of what exists.

---

## 2. Lifecycle Step Mapping — Truck & Gate (F-U3)

Canonical order: `Transporter registered → driver PDP permit → assignment to container job → gate document (Form 13 / PIN ticket) → terminal gate-in (BAT lane) → yard pickup/drop → EIR issued → gate-out (CODECO/GatePass) → [scanner if RMS-selected] → corridor exit`

### 2.1 Transporter Registration — **PARTIAL**

| Layer | Finding |
|---|---|
| Router/API | `gateway/routers/transporters.py` — POST/GET `/api/transporters`, `/blacklist`, `/{id}`, `/{id}/vehicles`, `/{id}/blacklist`, `/{id}/lift`, `/validate/vehicle/{plate}`, `/validate/driver/{driver_id}`. Upload: `/api/td-upload/*` (`transporters_drivers_upload.py`, entity TRANSPORTER). |
| Service/Repo | inline SQL in router + `services/transporters_drivers/{repository,upload_parsers,upload_service}.py` |
| DB | `core.transporter` (arch base + 0102: `id`, `code`, `gstin`, `status`, partial-unique code) · `core.transporter_vehicle` · `core.transporter_blacklist` (ACTIVE/LIFTED history) · `core.td_import_file`/`_error` ledger |
| Status values | `ACTIVE|SUSPENDED|BLACKLISTED` (CHECK exists only in legacy 0024; **v3 `status` has no CHECK**) |
| Events | blacklist → WS `alert` + `persist_alert_event` (both `except: pass`); `dispatch_alert` imported but **never called**; everything else emits nothing |
| Ingestion | `scripts/import_transporter_master.py` maps TransporterDetails.xlsx columns exactly — **but is broken vs v3** (writes legacy column names `source_company_id/name/mobile/doc_type/doc_file`, conflicts on a constraint that doesn't exist in v3) |
| Frontend | `TransporterBlacklist.tsx` (tab under `/vehicles`, `/alerts`, `/reports`) + `td/UploadPanel.tsx` |
| **Missing** | transporter update/delete API; approval/KYC workflow; `city` column (client master has city — only free-text `address` exists); **name bridge for gate-doc codes** (`TRANSTAR`, `TRANSTA`, `[AAGFT1724J]` appear nowhere; only lowercased-name match in importers, no PAN/GSTIN/alias matcher); **`/api/transporters` absent from `gateway/auth.py::_POLICY`** → open to any authenticated role incl. DRIVER |

### 2.2 Driver PDP Permit — **PARTIAL**

| Layer | Finding |
|---|---|
| Router/API | `gateway/routers/drivers_master.py` — `GET /api/drivers/master` (+`/stats`, `/validate/{licence}`, `/{licence}`, `/{licence}/pdp-history`). RBAC CUSTOMS+DTCCC_ADMIN. Note: `gateway/routers/pdp.py` is a *different thing* (Port Data Platform mock adapter, `/api/pdp/*`). |
| Service/Repo | `services/driver_master/{repository,service}.py` |
| DB | `core.driver` (31,846 rows; `licence_no_norm` generated column, `transporter_id` FK, `licence_valid_to`) · `core.pdp` (367,078 rows: `pdp_id` PK, `appl_number`, `pdp_number`, `accepted_at`, `active`, `valid_until`, `cancelled_by`, `cancellation_date/time`) |
| Version history | Derived at query time by grouping on `appl_number` (11× renewal fan-out is implicit). No version table, no sequence, no parent link, no renewal counter. |
| Cancellations | Columns present and exposed in `pdp-history`; no cancel API, no reason field, no cancellation event table. |
| **Critical semantic defect** | `pdp_status` (ACTIVE/EXPIRING/EXPIRED), the `/stats` KPIs, and `validate()`'s ALLOW/REVIEW decision are all computed from **`core.driver.licence_valid_to` (driving-licence date), not from `pdp.active`/`pdp.valid_until`**. The permit itself is never evaluated anywhere. |
| Enforcement | **PDP is never checked at assignment, booking, or gate time.** `core.pdp` is referenced by zero gateway modules. `/api/drivers/master/validate/{licence}` is called only from the frontend. |
| Performance | **`core.pdp` has no indexes in v3** (legacy 0026 indexes were not ported) — 367k-row sequential scans on every pdp-history call. No FK pdp→driver (string join via `latest_pdp_number`; historical permits attach only via shared `appl_number`). |
| Ingestion | `scripts/import_driver_master.py` reads both xlsx sheets — **broken vs v3** (legacy column names + wrong conflict target vs the partial index `uq_driver_licence_norm`). `/api/td-upload` has **no PDP entity** (drivers only), so there is currently **no working PDP ingestion path**. |
| Frontend | `DriverMaster.tsx` tab. **`pdp-history` endpoint has no UI caller.** |

### 2.3 Vehicle Assignment (vehicle↔transporter↔driver) — **PARTIAL, fragmented**

Three disconnected mechanisms, no shared key, no reconciliation job:

1. **`core.transporter_vehicle`** (transporter→vehicle+driver): written only by manual `POST /api/transporters/{id}/vehicles` and a demo seed. `driver_id` is unvalidated free text (no FK). Consumed only by blacklist validation.
2. **`core.driver.transporter_id`**: resolved by lowercased company-name string match in importers/upload; unmatched → NULL.
3. **`core.driver_identity.vehicle_no_norm`** (PWA login binding): partial-unique per ACTIVE driver; `fleet.sync_from_assignments()` backfills `core.vehicle`.

Vehicle master itself is **YES**: `core.vehicle` + `gateway/fleet.py` + `/api/vehicles` (list/stats/available/create/patch/sync-fleet), backend-generated `TRK-%06d` ids, dedup on plate, deactivation guard when a driver holds the vehicle. VAHAN RC/DL/FASTag lookup chain (LIVE→SIM→CACHED→PROVISIONAL) is implemented with full lookup-history tables, but **VAHAN owner data is never used to derive the vehicle→transporter join** the client doc flags as NOT-IN-CORPUS.

**Missing:** PDP/licence validity check at assignment; assignment history (no end-date/unassign); any join between the three mechanisms; drift defect in `gateway/fleet.py` `_DDL` (`vehicle_number` vs queried `vehicle_no`, masked only because `JNPA_RUNTIME_DDL` defaults off).

### 2.4 Container (Job) Assignment — **MISSING**

- The only container→vehicle link is `core.cargo.vehicle_number` — nullable free-text plate set via generic `PUT /api/cargo/{cn}`; trim/upper only, **no plate-format validation, no existence check against `core.vehicle`, no driver field at all, no uniqueness** (one truck can be on N containers).
- No job/trip-order entity binding {transporter, driver, vehicle, container, move-type (import pick / export drop / empty pick / empty-by-group), gate, document}.
- `core.tas_booking` (vehicle_id, driver_id) has **no container_no**; `core.tt_trip` has no container; `core.trt_record` has no container.
- The one table with the right shape — `core.empty_container_allocation` (`container_id, truck_id, trailer_id, driver_id, cfs, ecd, status ALLOCATED→…→COMPLETED`) — belongs to the synthetic `empty-container/` optimiser: only `ALLOCATED`/`ALLOCATION` are ever written, `container_id` is not ISO-validated, and it has zero linkage to `core.cargo`.

### 2.5 Gate Document: Form-13 — **PARTIAL (synthetic only)**

- Exists only as deterministic seed data: `gate-data/seed.py` `Form13Record(form13_no, container_no, shipping_bill_no, cargo_desc, gross_wt_kg)` → `core.gate_capture` (`capture_type='FORM13'`, jsonb payload). Live-mode seam (`GATE_FORM13_MODE=live` + URL) exists but unconfigured.
- **No parser for the real `form13_parsed/*` corpus.** Client fields with no home: VisitID (`4418958`), in/out gate codes (`IGTK01`/`OGTK05`), transporter name, export/import flag, BAT.
- `document_ocr.py` coerces doc type `FORM13` to `UNKNOWN` (`_DOC_TYPES = {LR, INVOICE, EWAYBILL, PERMIT, UNKNOWN}`), and even successful OCR returns `_mock_fields(...)` ("field parsing TODO").

### 2.6 Gate Document: PIN Ticket — **MISSING**

`grep -riE "pin_?no|pin_?ticket|dual.?move|MTYHLI|TRANSTAR"` → **0 hits**. No PIN entity, no yard location in real format (`2P08D.1`), no Gate-10, no "Read SMS" yard, no empty-by-group pick, no dual-move ticket.

### 2.7 Terminal Gate-In (BAT lane) — **PARTIAL (event), MISSING (BAT / API / document link)**

- `core.gate_event` (`GATE_ARRIVAL|GATE_TXN_START|GATE_IN|GATE_OUT`, device_id, plate, gate_id, trip_id) — **sole writer is the truck simulator**. No `POST /api/gate/*` ingest endpoint; ANPR reads are never bridged to gate events.
- `GateTransaction` DTO (`shared/jnpa_shared/schemas.py:326` — gate_id, direction, vehicle_no, container_no, outcome, duration_s) and topic `gate.transactions` are **dead: never produced, never consumed**.
- **BAT lane (`D391`, `B723`): zero code references.** `gate_event` has `gate_id` only; the cosmetic `"lane": "IN-2"` in the PDP mock is never stored.
- No container number, no document reference (Form13/PIN/GatePass) on any gate event.
- `core.gate_capture` + `core.leo_reconciliation` (Auto-LEO: e-Seal/Form13/Weighbridge/ICEGATE checks, flags → alerts) are real but **never join `core.gate_event`**.

### 2.8 Yard Pickup / Yard Drop — **MISSING (as events)**

- Yard exists only as cargo *state*: `yard_block` (regex `^[A-Z]{1,2}-\d{1,3}$` — **cannot hold the real PIN format `2P08D.1`**), plan rows (`cargo_yard_plan.yard_row/slot/position` free text, write-once), `YARD_ASSIGNED`/`YARD_POSITION_ALLOCATED` transitions.
- No `picked_up_at`/`dropped_at`, no pickup/drop events (`grep -i pickup` → only a KPI name), no slot inventory/occupancy (except reefer slots, which `plan_reefer` doesn't even write), no slot-conflict prevention (`next_yard_slot()` is a racy count).

### 2.9 EIR Issuance — **MISSING (entirely)**

The token `EIR` appears **nowhere** in the repo (all file types). No entity, parser, endpoint, status. Consequences: the corpus's measurable TAT ground truth (82/165 min TruckIn→TruckOut), seal numbers, vessel/VIA on gate docs, To/From CFS binding (`CLP CFS`), and scanner stamps are all unrepresentable.

### 2.10 Gate-Out / CODECO / GatePass — **PARTIAL (two silos)**

- **Silo 1 — gate event:** `core.gate_event.GATE_OUT` (simulator only). Read by `journey.py` and TAT views.
- **Silo 2 — EDO/CODECO:** `services/shipping_lines/parsers/edo_codeco.py` genuinely parses CODECO XML incl. **`gate_pass_no`, `gate_pass_ts`, `vehicle_no`, `gate_number`, `con_seal_status`** → `core.edo_delivery_order` (unique on ref+container+gatepass, indexed by vehicle_no). Queryable via `GET /api/shipping-lines/delivery-orders?vehicle=`; rendered in `ShippingLines.tsx`.
- **Missing:** `gate_pass_no` is never joined to gate events, TRT, trips, or cargo; `gate_number` free text, not FK to `core.gate`; no issuance workflow; "same truck two jobs one night" is not derived (double-trip module can't see gate passes); containerless CODECO blocks are **silently skipped** (`edo_codeco.py:63 continue`) and flat-EDO rows without gate_pass_no are rejected — i.e. the corpus's `MH46AF4375` no-container document cannot be ingested by any path (no vehicle-keyed fallback).

### 2.11 RMS Scanner — **PARTIAL (selection), MISSING (routing/scan-done)**

- **Implemented:** `rms_txt.py` parses selection lists (extracts `machine_type` letter D/M + `scan_location` e.g. `INNSA1RSDT02`, CFS name, goods) → `core.rms_scan_report`/`core.rms_scan_container`; `mart.v_customs_container_status.rms_selected`; `reconcile_cargo()` → `customs_status='UNDER_INSPECTION'` + `CUSTOMS_SCAN_REQUIRED` notification; ICEGATE gate-capture gets `assessment=ASSESSED|FACILITATED`.
- **Missing:** reconcile is **manual-only** (no post-import hook/scheduler — in a running system `rms_selected` may never reach cargo); no scanner-machine master (D-/M- codes never reconstituted; the mobile/fixed/drive-through decode exists only as an SQL comment); **no scan-done event or status** ("SCANNED CLEAN" unrepresentable; `customs_status` CHECK has no post-scan value — a scanned box stays UNDER_INSPECTION until OOC); **no truck/trailer→scanner link** (`ingest/dtcs/` required by checklist INT-11 does not exist; `trailer_reads` is camera-OCR only); no `GET /api/customs/rms/{igm}/containers` (headers only); amendment lists silently dropped (`ON CONFLICT (report_id, sl_no) DO NOTHING`); journey/gate-exit logic never branches on `UNDER_INSPECTION`; no Kafka/WS distribution of `customs.rms_selected`.
- **Defect:** `gateway/customs_ext.py` boot DDL contradicts runtime SQL (`scanlist_id/scan_machine` vs `report_id/machine_type/agent_pan/processing_end`) — with `JNPA_RUNTIME_DDL=1` on a fresh DB every RMS insert fails; the schema lock-step test checks names only.

### 2.12 Corridor Exit — **MISSING (as a step)**

Corridor geometry (`jnpa_shared/corridor.py`, `GET /api/corridor`), geofence zones/events, and FASTag transactions (`lane_direction`, `toll_plaza_name`) all exist — **none are linked to a gate journey**. The only "exit" signal is the simulator's state change. No corridor-exit event, no toll/ANPR checkpoint tie-in.

### 2.13 Adjacent capabilities (implemented, reusable)

- **TAT/TRT:** `mart.v_tat_inside_port` etc. (event-derived, sim-fed) + `core.trt_record` phase machine (`POST /api/trt/phase`, GATE_IN→PARKED→LOADING→COMPLETED, minutes computed, WS frame). Two parallel unreconciled implementations; neither ingests document-derived TAT.
- **Double-trip:** `core.tt_trip` cycle model + `/api/double-trip/*` + WS on 2nd completion. Capped at 2 legs, no container/move-type → dual-move not representable.
- **TAS:** persisted `/api/rms-tas/*` (slots/book/status with FOR UPDATE + deferred-window guard) **and** in-memory `/api/tas/*` (`tas_mock`) — the cross-twin deferred pump writes to the **in-memory** one. Bookings carry no container.
- **Cargo lifecycle (0023):** 9-state machine with rank-based forward-only transitions, mandatory-state gating, 409s, transactional audit rows (`cargo_lifecycle_event`), scan-queue, verify, release. Gaps: `SCAN_PENDING` never written (derived label only); **`PUT is_released=true` bypasses the whole chain from any state**; several transitions `strict=False` silently no-op; `discharge_time` not persisted; zero FKs; events are DB rows (`core.cargo_event`) polled via HTTP — nothing consumes `cargo.released`.
- **Customs docs (0031):** IGM/OOC/SMTP/SB/LEO fully ingested with ledgers. LEO is SB-keyed and never joined to a container (drawer shows "N/A" for imports). Separate synthetic Auto-LEO engine is unconnected to imported LEO.
- **Journey:** `GET /api/journey/container/{cn}` — 11 stages, but stage timestamps are **synthetic offsets from hard-coded anchor 2026-06-13T06:00Z**; only `facts` are live. Container-keyed only; no truck/plate journey endpoint.

---

## 3. Lifecycle Step Mapping — Empty ECY→CFS (F-Y1)

| Step | Status | Evidence |
|---|---|---|
| ECY gate-out | **PARTIAL** | Generic row `core.cfs_ecy_movement(facility_type='ECY', mode='OUT')`. No destination, no departure semantics. |
| Road movement | **MISSING** | Zero code. No vehicle/driver/transporter column or join anywhere in the CFS-ECY path. |
| CFS gate-in | **PARTIAL** | Generic row; no same-day rule, no link to preceding ECY-Out. |
| CFS dwell | **YES (CFS-local)** | `mart.v_cfs_ecy_dwell` (`max(OUT)−min(IN)` grouped by container+facility, CFS only) + `/api/cfs-ecy/dwell` + avg/median in `/stats`. ECY dwell intentionally NULL. |
| CFS gate-out | **PARTIAL** | Generic row; terminates dwell; no destination/terminal/export intent. |
| Terminal export flow | **MISSING** | No table/endpoint/join. Only a soft, by-value `core.cargo.lifecycle_status` lookup that returns NULL for all CODECO containers in practice. |

**Chain materialization: MISSING.** No table/view/CTE joins ECY→CFS rows (the dwell view partitions *by facility*, so they can never combine). The 242 verified chains are visible only by a human reading `GET /api/cfs-ecy/containers/{cn}`'s flat event list. No transit time, no cycle time, no orphan/broken-chain reporting. `active_containers` conflates ECY states (every single-event ECY container miscounts).

**Anomaly surfacing: MISSING.** COSU4663595's duplicate IN collapses silently at the unique constraint (script discards the count; upload records it as a warning only); both OUTs are retained (different timestamps), biasing that container's dwell +2 h with no flag. No anomaly table/column/endpoint/UI; the string COSU4663595 appears nowhere. OUT-before-IN, multi-OUT, unbalanced pairs — none detected.

**Facility attribution: KEY-ONLY (matches corpus limitation, but not surfaced).** filename→`CFS|ECY` constant; upload selector + optional per-row column. No facility master — CFS codes CLP/BLC/DRT/JCF/SBW exist only as free text in shipping-lines data (`nominated_cfs`, `group_code`), never mapped; all CFS dwell pools physically distinct facilities.

**Module quality is otherwise good:** 3-layer validation, ISO-6346 check-digit, IST stamping, sha256 file dedup + row-level idempotency, whitelisted sort/filter, RBAC, 2 solid test files, upload UI. But: `EVENT_UPLOAD` constant is dead; no Kafka/notification/audit emission; `/dwell` and `/uploads/{id}` have typed clients but no screen; `cfs_ecy_ext._DDL` creates schema `core` but not `mart` (view creation would fail on a bare DB); router docstring contradicts actual RBAC; source xlsx paths are hardcoded to a local machine directory.

**Two disconnected islands:** `core.cfs_ecy_movement` (real CODECO) vs `core.empty_container_*` (synthetic optimiser with exactly the columns F-Y1 needs — cfs, ecd, truck_id, driver_id, IN_TRANSIT status). Nothing joins them; `empty.container.moves` topic is write-only; only `ALLOCATED`/`ALLOCATION` states are ever written.

---

## 4. Existing Features (implemented, keep, reuse)

| # | Feature | Where | Reuse note |
|---|---|---|---|
| 1 | Transporter master + blacklist w/ history + validate endpoints | `transporters.py`, `core.transporter*` | Base for gate-doc name bridge |
| 2 | Driver master (31,846) + PDP ledger (367,078) + derived version history | `services/driver_master`, `core.driver`, `core.pdp` | Fix status semantics + indexes; do not rebuild |
| 3 | Fleet/vehicle master + TRK-id allocation + driver-hold guard | `fleet.py`, `vehicles.py`, `core.vehicle` | Assignment validation target |
| 4 | VAHAN RC/DL/FASTag 4-rung chain + lookup history + provisional flow | `vahan.py`, `vehicle_intel.py` | — |
| 5 | Cargo 9-state lifecycle machine + transactional audit + 60-test suite | `services/cargo`, 0023 | Extend, don't replace |
| 6 | Customs document layer: IGM/OOC/SMTP/RMS/LEO/SB parsers + ledgers + reconcile | `services/customs` | RMS routing builds on `rms_scan_container` |
| 7 | Shipping-lines IAL/EAL/EDO incl. CODECO XML parser with GatePass/vehicle/gate fields | `services/shipping_lines` | **The** GatePass source for gate-out joins |
| 8 | CFS-ECY CODECO store + CFS dwell view + upload module | `services/cfs_ecy`, 0027/0034 | Chain layer goes on top |
| 9 | Upload-ledger pattern (sha256 dedup, validate/upload/uploads, per-row errors) — uniform across td/cfs-ecy/sl/customs/perf/berthing | multiple | Template for EIR/Form13/PIN ingestion |
| 10 | TRT phase machine + double-trip cycles + TAS booking (persisted) | `trt.py`, `double_trip.py`, `rms_tas.py` | Extend with container/move-type |
| 11 | Auto-LEO reconciliation (e-Seal/Form13/WB/ICEGATE flags → alerts) + live ICEGATE adapter | `gate-data/` | Gate-side flag engine |
| 12 | ISO-6346 check-digit validation shared lib | `jnpa_shared/iso6346.py` | Use in every new entity |
| 13 | Kafka/CloudEvents plumbing + resilient KafkaPump (backoff) + WS broadcast | `kafka_io.py`, `pumps.py` | Ready for gate/scan topics |
| 14 | Enforcement hash-chained case audit | `enforcement.py` | Pattern for document audit if needed |

**Do NOT change:** the 0023 rank-based transition core, the upload-ledger pattern, ISO-6346 lib, KafkaPump, the customs/shipping-lines parser architecture, RBAC scheme, v3 `core.*`/`mart.*` naming.

---

## 5. Partial Features (need extension)

1. **PDP semantics** — permit validity/active never used in any decision; fix `_STATUS_EXPR`, `/stats`, `validate()` to evaluate `core.pdp` (keep licence check as a second signal).
2. **Form-13** — synthetic seed only; needs real entity/parser (VisitID, gates, transporter, export flag) feeding `gate_capture` or a dedicated table.
3. **Gate events** — need an HTTP/Kafka ingest path + container/document/lane columns + production of `GateTransaction` on `gate.transactions`.
4. **RMS** — needs machine master, containers endpoint, amendment handling, automated reconcile, scan-done state/event, truck link.
5. **GatePass** — parsed but siloed; needs joins (gate_number→core.gate, vehicle_no→fleet, gate_pass↔trips/TRT/cargo).
6. **CFS-ECY** — needs chain/leg materialization, transit+cycle metrics, anomaly flags, facility master.
7. **Yard model** — widen location format, add pickup/drop events + occupancy.
8. **Double-trip** — extend `tt_trip` with container/move-type to express dual-move; derive cycles from gate passes.
9. **TAS** — unify in-memory vs persisted surfaces; add container to bookings.
10. **Cargo notifications** — add acknowledge/resolve endpoints (CHECK values currently unreachable).
11. **Journey** — replace synthetic anchor timestamps with real event times; add truck-keyed journey.
12. **Workflow engine** — actions are strings, never executed, never triggered by pumps; either wire or descope.

---

## 6. Missing Features (build new)

| # | Feature | Sized as |
|---|---|---|
| M1 | **EIR entity + parser + ingestion + API** (container, seal, vessel+VIA, BAT, TruckIn/TruckOut→TAT, To/From CFS, company, stamps) | New table + parser + upload module (reuse ledger pattern) |
| M2 | **PIN ticket entity + parser** (PIN no, yard loc free-format, gate, company, dual-move legs, empty-by-group) | New table + parser |
| M3 | **Container job assignment** entity ({transporter, vehicle, driver, container/group, move-type, document ref, status}) + assignment API + validations (vehicle ACTIVE, driver PDP/blacklist, no double-assignment) | New table + endpoints + guards |
| M4 | **Gate-event ingest API** + BAT-lane column + document linkage + `gate.transactions` production | Router + ALTERs + pump |
| M5 | **Yard pickup/drop events** + slot occupancy | Columns + events + endpoints |
| M6 | **Scanner routing**: machine master, truck→scanner decision endpoint, scan-done event + post-scan status value | New ref table + endpoint + CHECK extension |
| M7 | **Corridor-exit event** (FASTag/ANPR/geofence tie-in to journey) | Join logic + event |
| M8 | **ECY→CFS chain materialization** (legs, transit time, cycle dwell, orphans) + anomaly flags + facility master (CLP/BLC/DRT/JCF/SBW) | View/table + endpoints |
| M9 | **Terminal-export handover linkage** for empties (soft-join to EAL/Form13/CODECO; explicitly report "not in corpus" when absent) | Query layer |
| M10 | **Transporter name bridge** (alias table + normalizer for TRANSTAR/TRANSTA/PAN codes) | Ref table + matcher |
| M11 | **Event distribution** for document milestones (customs.rms_selected, cargo.released, cfs_ecy movements) to Kafka/WS | Producers + pump wiring |
| M12 | **PDP upload entity** in `/api/td-upload` (or fixed importer) — currently no working PDP ingestion | Parser entity |

---

## 7. API Gap Analysis

**Existing & adequate:** `/api/transporters/*` (needs RBAC), `/api/drivers/master/*`, `/api/vehicles/*`, `/api/vahan/*`, `/api/cargo/*` (23 endpoints), `/api/customs/*` (14), `/api/shipping-lines/*` (16), `/api/cfs-ecy/*` (9), `/api/rms-tas/*`, `/api/trt/*`, `/api/double-trip/*`, `/api/gate-data/*`, `/api/journey/container/{cn}`, `/api/td-upload/*`.

**Missing endpoints:**
- `POST /api/gate/events` (or equivalent ingest) — no way to record a real gate crossing
- `GET /api/customs/rms/{igm_no}/containers` (+ machine/location filters)
- `POST /api/customs/reconcile` automation (post-import hook)
- Job assignment: `POST/GET /api/jobs` (or `/api/cargo/{cn}/assign` with driver+vehicle+validation)
- EIR/PIN/Form-13 CRUD + upload (validate/upload/uploads triad)
- `GET /api/cfs-ecy/chains` (+ `/anomalies`)
- Truck-keyed journey: `GET /api/journey/vehicle/{plate}`
- Cargo notification ack/resolve: `POST /api/cargo/notifications/{id}/ack|resolve`
- Transporter PATCH/DELETE; PDP cancel/renew (if lifecycle writes are in scope)
- Scanner: `GET /api/scanners`, `GET /api/scan-status/{vehicle|container}`

**Needs extension:** `PUT /api/cargo/{cn}` (validate vehicle, or deprecate vehicle_number in favor of assignment); `POST /api/rms-tas/book` (+container_no); `POST /api/double-trip/start` (+container/move-type); `/api/cargo/scan-queue` (should consider `rms_selected`, not just yard state).

**Response gaps:** `/api/drivers/master/*` should expose permit-derived status; `/api/cfs-ecy/stats.active_containers` semantics; journey stages need real timestamps; `CustomsDetailsDrawer` data already includes machine_type/scan_location (render them).

**Dead/orphan surfaces:** `/api/tas/*` in-memory vs `/api/rms-tas/*` duplication; `web` calls `/api/congestion/metrics` (404s silently; real path `/api/traffic/congestion/metrics`); customs module endpoints (12 of 14) have no UI; `/api/vehicles` list/stats/create/patch unused by UI.

---

## 8. Database Gap Analysis

**Missing objects:**

| Object | Purpose |
|---|---|
| `core.eir` (+ import ledger) | EIR documents; TAT source of truth |
| `core.pin_ticket` (+ legs child table for dual-move) | PIN tickets |
| `core.form13` (real fields: visit_id, in_gate, out_gate, transporter, direction) — or structured columns/promotion from `gate_capture.payload` | Form-13 |
| `core.container_job` / `core.cargo_assignment` (transporter_id FK, vehicle_id FK, driver ref, container_number, move_type, document refs, status + history) | Job assignment |
| `core.scanner_machine` (code, type mobile/fixed/drive-through, location, terminal) | RMS routing |
| `core.scan_event` (container, vehicle?, machine, result, ts) | Scan-done |
| `core.transporter_alias` (alias→transporter_id, source) | TRANSTAR/TRANSTA bridge |
| `core.cfs_facility` (code CLP/BLC/DRT/JCF/SBW, name, type) | Facility identity |
| `core.cfs_ecy_chain` or `mart.v_cfs_ecy_chain` (container, ecy_out_ts, cfs_in_ts, cfs_out_ts, transit_h, dwell_h, cycle_h, complete bool, anomaly flags) | F-Y1 chain |
| Yard occupancy (slot inventory beyond reefer) | Pickup/drop |

**Missing columns:** `gate_event`: container_no, lane/bat, document_ref, source; `cargo`: driver ref, discharge_time, picked_up_at/dropped_at (or via events); `tas_booking`: container_no; `tt_trip`: container_no, move_type; `cfs_ecy_movement`: facility_code, vehicle_no (nullable), anomaly flag; `cargo.customs_status`: post-scan value (e.g. `SCANNED_CLEAN`).

**Missing constraints/indexes:** **`core.pdp` — recreate `(pdp_number)`, `(appl_number)`, `(active)` indexes (0026 not ported)**; FKs are absent across all cargo tables and pdp→driver (add soft or real FKs where data quality permits); CHECKs missing in v3 for `transporter.status` and `vehicle.status` (legacy had them).

**Defects to fix:** `import_transporter_master.py` + `import_driver_master.py` legacy column names vs v3; `customs_ext.py` and `fleet.py` and `cfs_ecy_ext.py` boot-DDL drift vs runtime SQL (and lock-step tests compare names only — extend to columns); stale test `test_customs_repository.py` legacy table name; duplicate migration number 0038; `core.ulip_api_audit` has no writer.

---

## 9. Workflow Gap Analysis

**Backend workflow as actually implemented (container view):**
`CREATED → VESSEL_DISCHARGED → YARD_ASSIGNED → [YARD_POSITION_ALLOCATED | REEFER_PLANNED | RAKE_ASSIGNED] → (SCAN_PENDING: label only) → VERIFIED → RELEASED` — then nothing (release event has no consumer).

**Client workflow (truck view):** registration → PDP → job → document → gate-in(BAT) → yard pickup/drop → EIR → gate-out(GatePass) → [scan] → corridor exit.

| Issue | Detail |
|---|---|
| Missing statuses | `SCAN_PENDING` never written; no post-scan status; no gate-in/out, pickup/drop, in-transit, corridor-exit states anywhere on the truck side |
| Invalid transition | `PUT is_released=true` jumps any state → RELEASED (documented bypass — never-verified boxes can appear in the handover query) |
| Silent no-ops | yard-assign / yard-position / reefer / rake transitions run `strict=False` |
| Missing validation | assignment (vehicle/driver/PDP/blacklist), booking (container), gate-in (PDP/blacklist/document) — none enforced |
| Missing events | no Kafka for any lifecycle milestone (all DB-row events, HTTP-polled); `gate.transactions`, `vehicle.tracks`, `geofence.violations` topics dead; `fastag.txns`, `parking.state`, `weighbridge.reads`, `empty.container.moves`, `carbon.records`, `face.verifications` write-only |
| Duplicate logic | two TAT implementations (mart views vs TRT); two TAS surfaces (mock vs persisted, cross-twin pump feeds the mock); two LEO systems (imported `core.leo` vs synthetic Auto-LEO); Auto-LEO vs cargo scan-verification |
| Dead code | generic `/api/workflows` engine authors rules but **never executes actions and is never triggered**; `EVENT_UPLOAD` (cfs_ecy) dead; `dispatch_alert` import unused (transporters); `GeofenceEnforcement.tsx` unrouted; `TOPIC_RFID`/`MQTT_RFID_PREFIX` shared constants unused (duplicated literals) |
| Unreachable states | `cargo_notification` ACK/RESOLVED; `empty_container_allocation` PICKED_UP…COMPLETED; `movement_history` PICKUP/DELIVERY/COMPLETION; `import_file.source='DIRECTORY'` for cfs-ecy |

---

## 10. UI Impact Analysis

| Missing backend feature | UI change required |
|---|---|
| EIR / Form-13 / PIN entities | New "Gate Documents" tab (natural home: `/gate-customs` or `/reports`); FollowTheBox timeline gains document artifacts; upload panel (clone `cfs/UploadPanel.tsx`) |
| Job assignment | `VehicleManagement.tsx` gains "Assign to job" action; new job list/detail; FollowTheBox `truck_assignment` stage becomes real; PWA driver job screen (`mobile-pwa` currently has **no** cargo/job/gate surface) |
| Real gate events + BAT | GateCustoms captures table gains lane; a truck-keyed timeline screen (currently none — `/api/trucks/{id}`, `/api/trt/vehicle/{id}` unused by any screen) |
| TAT from EIR | KPI strip `TAT inside port` gains a document-derived series; EcyTrt tab comparison |
| Scanner routing / scan-done | CustomsDetailsDrawer: render machine_type/scan_location (already fetched, never rendered); RMS list screen (customs module has **no dedicated screen** — 12 of 14 endpoints UI-less); GateCustoms scan status chip |
| CFS-ECY chains + anomalies | CfsEcyMovements: chain view + anomaly badges; wire the already-typed-but-unused `cfsEcyDwell` and `cfsEcyUploadDetail` clients; KPI tiles for transit/cycle time |
| Facility master | CfsEcyMovements facility filter beyond CFS/ECY binary |
| PDP fixes | DriverMaster: surface permit-based status + wire `pdp-history` endpoint (currently uncalled); validate panel shows permit verdict |
| Notifications ack | AlertsCenter cargo-notification actions; `web` currently calls `/api/notifications` **nowhere** |
| Event distribution | WhatIfConsole/CommandCenter live frames for gate/scan/chain events (WS channels exist) |
| Dual-move | DoubleTrip screen gains per-leg container/move-type |

---

## 11. Risks

1. **Schema-drift time bombs:** three boot-DDL modules (`customs_ext`, `fleet`, `cfs_ecy_ext`) diverge from runtime SQL; masked only by `JNPA_RUNTIME_DDL=0`. Lock-step tests compare object names, not columns.
2. **Broken offline importers** (transporter + driver/PDP) mean master-data refresh currently has no working path for PDP at all; a re-import attempt will error mid-run.
3. **`core.pdp` unindexed** — pdp-history is a 367k-row seq scan; will degrade RDS under demo load.
4. **RBAC hole** on `/api/transporters` (writes incl. blacklist open to all roles).
5. **Lifecycle bypass** (`PUT is_released=true`) can leak unverified containers into UC-III handover.
6. **RMS amendments silently dropped** — a re-issued selection list changing a machine assignment is discarded.
7. **Anomaly absorption** in CFS-ECY silently biases dwell KPIs (the planted demo anomaly will be invisible in front of the client).
8. **Zero API tests** for `/api/transporters`, `/api/drivers/master`, `/api/vehicles`, `/api/trt`, `/api/double-trip`, `/api/kpi` — the areas Phase work will touch most.
9. **Two TAS surfaces** with the cross-twin pump feeding the in-memory one — restart loses deferred windows and the persisted book never sees them.
10. **Dead client codes:** TRANSTAR/TRANSTA/CLP/COSU4663595/BAT lanes appear nowhere — demo walkthroughs of the client's hero examples will fail unless Phase 2 lands.
11. Journey timeline's synthetic timestamps (anchor 2026-06-13) will read as fabricated data in a document-driven demo.

---

## 12. Recommended Implementation Order

### Phase 1 — Correctness & foundations (no schema additions beyond indexes)
- Fix `scripts/import_transporter_master.py` + `scripts/import_driver_master.py` to v3 columns/conflict targets; add PDP entity to `/api/td-upload` (or bless the fixed script as the path).
- Add `core.pdp` indexes (`pdp_number`, `appl_number`, `active`) — migration `0111` + RDS hotfix per existing precedent.
- Add `/api/transporters` to `_POLICY` (writes → CONTROL_ROOM/admin).
- Repoint PDP semantics: `_STATUS_EXPR`, `/stats`, `validate()` evaluate `core.pdp.active`/`valid_until` (licence date as secondary).
- Automate `reconcile_cargo()` post-import; add `GET /api/customs/rms/{igm}/containers`.
- Fix boot-DDL drift (customs_ext, fleet, cfs_ecy_ext incl. `CREATE SCHEMA mart`); extend lock-step tests to columns; fix stale legacy-name test.
- Files: `scripts/import_*.py`, `services/transporters_drivers/upload_parsers.py`, `gateway/auth.py`, `services/driver_master/repository.py`, `services/customs/{service,repository}.py`, `gateway/routers/customs.py`, `gateway/{customs_ext,fleet,cfs_ecy_ext}.py`, `infra/postgres/v3/0111_*.sql`, tests for each. **Risk:** low — additive/corrective.

### Phase 2 — Gate document layer (EIR / Form-13 / PIN) + TAT
- New tables `core.eir`, `core.pin_ticket`(+legs), `core.form13` (+ import ledgers via the standard triad); parsers under a new `services/gate_documents/` following the customs/sl parser architecture; upload router + panels.
- Document-derived TAT: computed column/view from EIR TruckIn/TruckOut; surface beside `mart.v_tat_inside_port`.
- Transporter alias bridge (`core.transporter_alias` + matcher fed from document company fields).
- Handle containerless documents (nullable container + vehicle-keyed fallback), fixing the EDO silent-skip too.
- Migration `0112`; tests per parser with corpus fixtures. **Risk:** medium — parsing real scanned-doc variance.

### Phase 3 — Assignment, gate events, yard, scanner
- `core.container_job` + assignment API with validations (vehicle ACTIVE, driver PDP+blacklist, uniqueness); deprecate direct `cargo.vehicle_number` writes (keep read-compat).
- Gate-event ingest endpoint + columns (container_no, lane, document_ref, source); produce `GateTransaction` on `gate.transactions`; consume in gateway pump → WS.
- Yard pickup/drop events + widened location format; wire `SCAN_PENDING` to actually be written; close the `PUT is_released` bypass behind a role/flag.
- Scanner: `core.scanner_machine` master, scan-status endpoint, `SCANNED_CLEAN`-style status value + `customs.scan_done` event; journey/gate-exit branch on inspection state.
- `tt_trip` + `tas_booking` container/move-type columns (dual-move representable).
- Migrations `0113`–`0114`; extend `tests/test_cargo.py`, new gate/scan tests. **Risk:** medium-high — touches the live cargo machine; mitigate by keeping 0023 core untouched and additive states/gates only.

### Phase 4 — ECY→CFS chain, events, UI completion
- `mart.v_cfs_ecy_chain` (leg pairing, transit/cycle/dwell, completeness, orphans) + `/chains` + `/anomalies` endpoints + ingest-time anomaly flags (duplicate IN, multi-OUT, OUT-before-IN); `core.cfs_facility` master.
- Soft export-handover join (EAL/Form13/CODECO lookups, honest "not in corpus" labeling); optional bridge from `cfs_ecy_movement` to `empty_container_allocation`.
- Event distribution: produce document/lifecycle milestones to Kafka/WS; consumer for `empty.container.moves`; notification triggers + ack/resolve endpoints.
- UI: gate-documents tab, RMS/customs screen, chain+anomaly views, truck timeline, DriverMaster pdp-history, render machine_type/scan_location, PWA job screen.
- **Risk:** low-medium — mostly additive reads and frontend.

---

## 13. Final Conclusion

The project's module inventory maps well onto the client corpus — every *master* and every *document family* the lifecycle documents cite (except EIR/PIN/real-Form-13) already has a table, an importer or upload path, and an API. The architecture (raw-SQL repositories, upload ledgers, v3 `core.*`/`mart.*`, RBAC prefixes, KafkaPump) is consistent and should be followed, not redesigned.

What the client documents actually describe, however, is a **journey** — one truck touching four documents across three terminals in seven days, and one empty container completing a three-leg repositioning chain — and the backend today cannot tell either story: the documents that anchor the truck journey (EIR, PIN) don't exist, the assignment that starts it doesn't exist, the gate events that mark it are simulated, and the empty-container chain is stored but never connected. The four-phase plan above closes exactly those gaps, reusing the existing parser/ledger/lifecycle machinery at every step, and defers nothing that the corpus can prove — while explicitly labeling the two things the corpus itself cannot prove (vehicle↔transporter join source, terminal export gate-in for empties) as KEY-ONLY/NOT-IN-CORPUS rather than fabricating them.
