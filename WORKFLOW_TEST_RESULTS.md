# JNPA Digital Twin — Workflow Test Results

**Date:** 2026-08-05 · **Companion to:** [WORKFLOW_AUDIT_REPORT.md](WORKFLOW_AUDIT_REPORT.md)

Raw commands and output from the workflow audit. Findings and fixes are in the audit report;
this file is the evidence trail.

**Environment:** macOS host, `.venv` (Python 3.13.3), DB = AWS RDS `jnpa_schema_v3`.
Docker daemon **not running**, so `truck-sim`, `ai/congestion`, `eir-ocr`, MinIO, Kafka,
MQTT and Redis were unreachable. Every degradation caused by that is labelled below.

---

## 1. Existing automated suite

The whole-suite invocation aborts natively at `test_performance` on this host
(pre-existing, environment-specific), so it was run in file batches.

```bash
.venv/bin/python -m pytest $(ls tests/test_*.py | head -40) -q          # batch A
.venv/bin/python -m pytest $(ls tests/test_*.py | sed -n '41,80p') -q   # batch B
.venv/bin/python -m pytest $(ls tests/test_*.py | sed -n '81,200p') tests/e2e -q  # batch C
```

```
batch A:  552 passed,  13 skipped   in 54.32s
batch B:  402 passed,  92 skipped   in 40.95s
batch C:  102 passed,   9 skipped   in 28.22s
------------------------------------------------
TOTAL:   1056 passed, 114 skipped, 0 FAILED
```

Workflow-specific subset:

```bash
.venv/bin/python -m pytest tests/test_cargo.py tests/test_cfs_ecy.py \
  tests/test_gate_documents.py tests/test_container_job.py tests/test_ecy_cfs_chain.py \
  tests/test_export_lifecycle.py tests/test_import_chain.py tests/test_chain_verify.py -q
```
```
176 passed, 3 skipped, 1 warning in 4.42s
```

### Skip reasons (the coverage gap — audit report §8.1)

```bash
.venv/bin/python -m pytest $(ls tests/test_*.py | head -40) -q -rs | grep '^SKIPPED'
```
```
   7  Postgres not reachable on 5433
   1  Kafka not reachable at localhost:29092; run `make up` first.
   1  congestion service not running
   1  anomaly service not running
   1  tesseract_binary_missing
   1  ai/anpr/eval/metrics.json absent
```

Files hardcoding `localhost:5433` (these silently skip instead of running against the
configured RDS DSN):

```bash
grep -rln "5433" tests/*.py
```
```
tests/test_cargo.py           tests/test_customs_repository.py   tests/test_rfid_ingest.py
tests/test_cfs_ecy.py         tests/test_gateway.py              tests/test_trucking_app.py
tests/test_customs_adapter.py tests/test_performance.py          tests/test_vahan_sim.py
                              tests/test_performance_upload.py
```

---

## 2. Smoke-test harness

The real gateway app was booted in-process against the real RDS, reusing the existing
test framework (Starlette `TestClient` + lifespan). Harness (`smoke_common.py`):

```python
def load_env() -> None:
    # parse .env.local into os.environ
    for line in (REPO / ".env.local").read_text().splitlines():
        ...
    # local infra is down -> disable pumps that would block on connect
    for k in ("KAFKA_BROKERS", "MQTT_HOST", "REDIS_URL", "AUDIT_BASE_URL"):
        os.environ.pop(k, None)
    os.environ["AUTH_ENABLED"] = "false"
    os.environ["JNPA_ENV"] = "dev"

def client():
    from starlette.testclient import TestClient
    from gateway.main import app
    return TestClient(app, raise_server_exceptions=False)

def q(sql, params=()):          # direct DB assertions via psycopg on RFID_POSTGRES_DSN
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, params); return cur.fetchall()
```

Connectivity confirmed before any run:

```
current_database = jnpa_schema_v3   current_user = postgres
schemas: core (197 tables), jnpa (2), mart (20 views), staging (2), synth, public
gateway route table: 421 routes
```

Test data was confined to `QATU…` containers, plate `MH04QA9911`, device
`QA-GHOST-DEVICE-0001`; the idempotency probe rows were deleted afterwards.

---

## 3. Workflow 1 — Cargo Lifecycle · 33/33 PASS

Container `QATU7788228`.

```
[PASS] 1. Create cargo                        POST /api/cargo -> 201  | CREATED
[PASS] 1b. DB core.cargo row created          SQL  core.cargo    | [('CREATED','PENDING',False)]
[PASS] 1c. Duplicate create -> 409            POST /api/cargo -> 409
[PASS] 1d. Bad ISO-6346 -> 400                POST /api/cargo -> 400
[PASS] 2n. Release before verify -> 409       POST .../release -> 409
       {"detail":{"error":"illegal_transition","current_status":"CREATED","attempted_status":"RELEASED"}}
[PASS] 2n2. Verify before yard-assign -> 409  POST .../verify  -> 409
       {"detail":{"error":"illegal_transition","current_status":"CREATED","attempted_status":"VERIFIED"}}
[PASS] 2. Cargo movement (gate patch)         PUT  /api/cargo/{cn} -> 200 | GATE-1
[PASS] 3. Vessel discharge                    POST .../discharge -> 200 | VESSEL_DISCHARGED
[PASS] 3b. Duplicate discharge -> 409         POST .../discharge -> 409
[PASS] 4. Yard assignment                     PUT  .../yard-assignment -> 200 | B-07 ASSIGNED
[PASS] 4b. DB yard_block + lifecycle          SQL  core.cargo | [('YARD_ASSIGNED','B-07')]
[PASS] 4c. Invalid yard_block -> 400          PUT  .../yard-assignment -> 400
[PASS] 5. Yard position allocation            POST .../yard-position -> 201 | YARD_POSITION_ALLOCATED
[PASS] 6a. Yard planning                      POST /api/cargo/yard-planning   -> 201 | assigned_block B-10
[PASS] 6b. Reefer planning                    POST /api/cargo/reefer-planning -> 201 | REEFER-A02
[PASS] 6c. Rake planning                      POST /api/cargo/rake-planning   -> 201 | RAKE-QA-01
[PASS] 6d. Yard optimization read             GET  /api/cargo/yard-optimization -> 200 | congestion 0.53
[PASS] 7. Scan pending (verify=false)         POST .../verify -> 200 | returned lifecycle=RAKE_ASSIGNED
[PASS] 7b. Container present in scan-queue    GET  /api/cargo/scan-queue -> 200 | queue_len=15
[PASS] 8. Verification                        POST .../verify  -> 200 | VERIFIED
[PASS] 9. Release                             POST .../release -> 200 | RELEASED, yard B-07, veh MH04QA0001
[PASS] 9b. Duplicate release -> 409           POST .../release -> 409
[PASS] 9c. DB final state                     SQL  core.cargo | [('RELEASED', True)]
[PASS] 10. Lifecycle history complete         GET  .../lifecycle -> 200
[PASS] 10b. DB core.cargo_lifecycle_event     8 rows: CREATED, VESSEL_DISCHARGED, YARD_ASSIGNED,
             YARD_POSITION_ALLOCATED, REEFER_PLANNED, RAKE_ASSIGNED, VERIFIED, RELEASED
[PASS] 10c. DB core.cargo_event topics        19 events / 11 topics: cargo.created, cargo.gate_movement,
             cargo.lifecycle_changed, cargo.queue_updated, cargo.rake_assigned, cargo.reefer_planned,
             cargo.released, cargo.verified, cargo.vessel_discharged, cargo.yard_assigned,
             cargo.yard_position_allocated
[PASS] 10d. GET /api/cargo/events -> 200 | n=19
[PASS] 11. Unknown container -> 404           GET  /api/cargo/QATU9999997 -> 404
[PASS] 11b. Discharge unknown -> 404          POST .../discharge -> 404
[PASS] 12. Workflow TRIGGER                   POST .../workflow -> 200 | TRIGGERED
[PASS] 12b. Workflow history -> 200 | n=1
[PASS] 12c. Stakeholder notification -> 201
[PASS] 13. List cargo + X-Total-Count -> 200  | total=11942

=== WF1-Cargo-Lifecycle: 33/33 steps passed ===
```

### Failed scenario (first run, superseded)

Step 7 was originally asserted as `lifecycle_status == "SCAN_PENDING"` and returned
`RAKE_ASSIGNED`. Investigation showed `SCAN_PENDING` is a **derived queue label** never
persisted to `core.cargo` ([services/cargo/service.py:69](services/cargo/service.py#L69));
`verify_cargo(verified=False)` deliberately does not transition
([service.py:479](services/cargo/service.py#L479)). The assertion was wrong — but the
*declared* response contract at
[gateway/routers/cargo.py:1130](gateway/routers/cargo.py#L1130) is also wrong → finding **C-2**.

Steps 5 originally failed 400 on my own malformed payload (`position` must be a string,
`priority` enum is `LOW|MEDIUM|HIGH|CRITICAL`); corrected and re-run → 201.

---

## 4. Workflow 3 — CFS-ECY · 23/24 PASS

```
[PASS] 0. DB core.cfs_ecy_movement populated   [('CFS', 967), ('ECY', 961)]
[PASS] 1. List movements                       GET /api/cfs-ecy/movements -> 200
[PASS] 1b. Filter facility=CFS / =ECY          -> 200, items>0 each
[PASS] 1c. Filter mode=IN / =OUT               -> 200, items>0 each
[PASS] 2. Container movement timeline          GET .../containers/ONEU2122848 -> 200
[PASS] 2b. Unknown container handled           -> 404 {"error":"container_not_found"}
[PASS] 3. CFS dwell report (OUT-IN)            -> 200 | e.g. CCLU2547920 dwell 140.5h
[PASS] 4. CFS-ECY stats/KPIs                   -> 200 | in 915 / out 1013 / containers 1202
                                                      | avg dwell 71.04h, median 68.17h
[PASS] 5. List ECY->CFS chains                 -> 200 | total=1202
[PASS] 5b. Chain KPIs                          -> 200 | complete 242, partial 528, anomalies 529
                                                      | NO_CFS_IN x287, avg transit 3.40h, cycle 94.78h
[PASS] 5c. One container's chain               -> 200
[PASS] 5d. DB core.ecy_cfs_chain populated     rows=[(1202,)]
[PASS] 6. Rebuild chains from CODECO           -> 200 {"chains":1202,"complete":242,"anomalies":529,"ms":309}
[PASS] 7. Download upload template             -> 200 | Container Number,Timestamp,Mode,Facility
[PASS] 7b. Validate well-formed upload         -> 200 {"status":"VALIDATED","valid":true,...}
[PASS] 7c. Validate rejects malformed rows     -> 200 {"status":"REJECTED","valid":false,
                                                       "errors":[{"row_number":1,"column_name":"Timestamp",...}]}
[PASS] 7d. Import upload                       -> 200 {"file_id":5,"status":"SUCCESS","imported":2}
[FAIL] 7e. Re-upload is idempotent             -> first_inserted=2 second_inserted=2   <-- see below
[PASS] 7f. Upload ledger (audit trail)         -> 200
[PASS] 7g. DB core.cfs_ecy_import_file ledger  rows=[(3,)]
[PASS] 8. Invalid facility handled             -> 400 {"error":"invalid_facility","facility":"NOPE"}
[FAIL] 9. Workflow events for CFS-ECY          core.digital_twin_event | events_matching_cfs=0

=== WF3-CFS-ECY: 22/24 (23/24 after 7e was re-tested at DB level) ===
```

### Failed scenario 7e — isolated and resolved

The response field was ambiguous, so idempotency was re-tested by counting actual DB rows:

```
rows before      : 0    import_files: 3
upload #1 -> 200 {'file_id': 6, 'status': 'SUCCESS',           'imported': 2, 'duplicate_file': False}
rows after #1    : 2    import_files: 4
upload #2 -> 200 {'file_id': 6, 'status': 'SKIPPED_DUPLICATE', 'imported': 2, 'duplicate_file': True}
rows after #2    : 2    import_files: 4
upload #3 (renamed file, identical content) -> 200 {'status': 'SKIPPED_DUPLICATE', 'imported': 2}
rows after #3    : 2

VERDICT movement rows: after1=2 after2=2 after3=2 -> IDEMPOTENT
cleanup: removed 2 probe rows
```

**Persistence is correctly idempotent** (content-hash based, survives a filename change).
The failure is a **reporting** defect: `imported: 2` on a `SKIPPED_DUPLICATE` where zero
rows were written → finding **E-1**.

### Failed scenario 9 — confirmed by code inspection

```bash
grep -rn "lifecycle_bus\|emit\|publish\|digital_twin_event" services/cfs_ecy/
# (no matches)
```
The module emits no events at all → finding **E-2** (consistent with its read-only design).

---

## 5. Workflow 2 — Transport · 20/26

```
[PASS] 1. Create vehicle (fleet registry)      POST /api/vehicles -> 200/409
                                               {"vehicle_id":"TRK-000031","vehicle_number":"MH04QA9911",...}
[PASS] 1b. DB core.vehicle row                 [('TRK-000031','MH04QA9911','ACTIVE')]
[PASS] 1c. Duplicate plate rejected            -> 409 {"error":"vehicle_number_exists"}
[PASS] 1d. Vehicle searchable                  GET /api/vehicles?q= -> 200
[PASS] 1e. Vehicle stats                       -> 200 {"total":41,"active":39,"assigned":3,"available":36}
[PASS] 1f. Available vehicles                  -> 200
[PASS] 2. Device list (truck-sim control plane) -> 200 {"count":0,"devices":[],"degraded":true}   [env: sim down]
[FAIL] 2b. A device_id is resolvable           device_id=None                                     [env: sim down]
[PASS] 3. Live telemetry read (fallback chain) -> 404 {"error":"no_position",
              "hint":"no live GPS, no ULIP relay, and no /checkin on record"}                     [correct degrade]
[PASS] 3b. Web check-in (TERTIARY telemetry)   POST /checkin -> 200 {"accepted":true,...}
[PASS] 3c. Check-in surfaces on truck read     -> 200 | decision_path=TERTIARY gate_delay=5       [fallback works]
[FAIL] 4. Push re-route advisory               POST .../route -> 502 {"error":"truck_sim_unreachable"}
[PASS] 4b. Read latest advisory (PWA poll)     -> 200 {"advisory":null,"source":null}
[PASS] 4c. Driver acknowledges advisory        -> 200 {"acked":true,"state":"ACK"}
[FAIL] 4c2. ACK for device with NO advisory    POST /api/trucks/QA-GHOST-DEVICE-0001/route/ack -> 200
              resp={"acked":true,"device_id":"QA-GHOST-DEVICE-0001","state":"ACK"}  advisory_rows=0
[FAIL] 4d. DB core.reroute_advisory persisted  rows=[(0,)]
[PASS] 5. Record gate movement                 POST /api/gate/events -> 201
              {"id":213337,"plate":"MH04QA9911","gate_id":"GATE-3","trip_id":"MH04QA9911",
               "event_type":"GATE_IN","container_number":"QATU5566772"}
[PASS] 5b. Query gate movements                GET /api/gate/events?plate= -> 200
[PASS] 6. /api/traffic/current                 -> 200 {"status":"LIVE","source":"TOMTOM","current_speed":81.0}
[PASS] 6. /api/traffic/health                  -> 200 {"provider":"TOMTOM","configured":true}
[FAIL] 6. /api/traffic/metrics                 -> 503 {"error":"congestion_metrics_unavailable"}  [env: ai/congestion down]
[FAIL] 6. /api/traffic/congestion/metrics      -> 503 (same)                                      [env]
[PASS] 6. /api/traffic/snapshots               -> 200 | SEG-00 speed 8.0 jam 7.5
[PASS] 6. /api/traffic/predict                 -> 200 {"decision_path":"SYNTHETIC","horizon_min":15}
[PASS] 6b. Congestion scan                     -> 200 {"threshold":0.8,"count":0,"created":[]}
[PASS] 6c. Alerts surface after scan           -> 200 | kind ELEVATED_SCRUTINY (raised by the check-in above)

=== WF2-Transport: 20/26 steps passed ===
```

### Failed scenarios — classification

| Step | Cause | Verdict |
|------|-------|---------|
| 2b, 4, 4d | `truck-sim` container unreachable (Docker not running) | **environment**, but exposes **T-1** |
| 4c2 | ACK succeeds against zero stored advisories | **defect T-2** |
| 6 (metrics ×2) | `ai/congestion` unreachable; degrades exactly as documented | **environment** |

**T-1 evidence.** `POST /api/trucks/{id}/route` raises 502 *before* persisting or
dispatching — `core.reroute_advisory` stayed at 0 rows, and no WS/WebPush/FCM fan-out
occurred. Code path: [gateway/routers/trucks.py:209-217](gateway/routers/trucks.py#L209-L217)
performs the sim call first; lines 235-241 (persist + fan out) are unreachable on failure.

**T-2 evidence.** [services/advisory/repository.py:82](services/advisory/repository.py#L82)
returns `bool(res.rowcount)` from `UPDATE core.reroute_advisory ... WHERE device_id = :d`,
but [trucks.py:305](gateway/routers/trucks.py#L305) discards it and returns
`{"acked": True}` unconditionally, having already written a `REROUTE_ACK` decision-audit row.

Earlier failures on `/checkin` (422 missing `plate`) and `/api/gate/events`
(422 missing `plate`) were my own payload errors; corrected and re-run → 200/201.

---

## 6. Workflow 4 — Gate Document · 34/36

```
[PASS] 0. DB tables   gate_document=13  eir=4  pin_ticket=2  form11=3
                      gate_doc_import_file=2  document_ocr=1  gate_capture=808
[PASS] 0b. Module summary       GET /api/gate-docs/summary -> 200
           {"eir":4,"pin_tickets":2,"pin_legs":2,"dual_move_tickets":0,"form13":202,
            "form13_live":0,"form13_sim":202,"containerless_docs":2,"eir_with_tat":2,"files":2}
[PASS] 1. Template EIR / PIN / FORM13   -> 200 each (correct headers)
[PASS] 1b. Invalid doc_type rejected    -> 400 {"error":"invalid_doc_type","allowed":["EIR","PIN","FORM13"]}
[PASS] 2b. Validate template example row -> 200 {"status":"VALIDATED","valid":true}
[PASS] 2c. Validate rejects malformed    -> 200 {"status":"REJECTED","valid":false,
                                                 "errors":[{"column_name":"Truck Number",
                                                            "error_code":"empty_required"}]}
[PASS] 3. Import EIR upload              -> 200 {"file_id":5,"status":"SUCCESS","imported":1}
[PASS] 3b. Re-import: no duplicate rows  after1=13 after2=13  duplicate_file=True
[FAIL] 3c. Re-import REPORTS imported=0  status=SKIPPED_DUPLICATE imported=1, DB unchanged 13->13
[PASS] 3d. Upload ledger / audit trail   -> 200
[PASS] 4. /api/gate-docs/eir     -> 200 total=5
[PASS] 4. /api/gate-docs/pin     -> 200 total=2
[PASS] 4. /api/gate-docs/form13  -> 200 total=202
[PASS] 4. /api/gate-docs/tat     -> 200 {"samples":3,"avg_tat_min":"137","median_tat_min":165.0,
                                          "min":"82","max":"165","by_terminal":[...]}
[PASS] 4b. All docs for one container    -> 200
[PASS] 4c. All docs for one truck        -> 200 (EIR joined by truck_no)
[PASS] 5. OCR health                     -> 200 {"engine":"tesseract","configured":true,
             "upstream":{"url":"http://eir-ocr:8210","reachable":false},"active_rung":"OCR"}   [env]
[PASS] 5b. Upload document -> OCR        -> 200 {"id":2,"fields":{},"confidence":0.75,
                                                 "source":"MOCK","storage_url":null,"status":"EXTRACTED"}
[PASS] 5c. List OCR documents            -> 200 count=2
[PASS] 5d. Read one OCR document         -> 200 raw_text="[MOCK OCR] EIR document"
[PASS] 6. Verify/correct extracted fields -> 200 status=VERIFIED
[PASS] 6b. DB reflects VERIFIED + merge   [('VERIFIED', {'verified_by':'qa-audit',
                                                         'container_number':'QATU7788228'})]
[PASS] 6c. Unknown document -> 404        {"detail":"document_not_found"}
[PASS] 6d. Verify unknown document -> 404 {"detail":"document_not_found"}
[PASS] 7. DB core.gate_capture            rows=808
[FAIL] 7b. Evidence object reference      no object path in payload; sample=
           {'form13_no':'F13758233896','cargo_desc':'READYMADE GARMENTS','gross_wt_kg':20621,
            'container_no':'MSCU0160690','shipping_bill_no':'9686631'}
[PASS] 7c. Police report generation       GET /api/reports/police -> 200 (ILLEGAL_PARKING incidents)

=== WF4-Gate-Documents: 34/36 steps passed ===
```

### Failed scenarios

- **3c** → finding **G-1**: `imported: 1` reported on a `SKIPPED_DUPLICATE` where the DB row count was unchanged (13 → 13). Same defect class as E-1.
- **7b** → finding **G-2**: all 808 `core.gate_capture` rows carry document metadata only — no `object_path` / `object_name` / `evidence_uri` — so `/api/evidence/{object_path}` has no stored target. Compounded by `storage_url: null` on new OCR uploads (MinIO down).

Also observed → finding **G-3**: MOCK OCR returns `confidence: 0.75` with `fields: {}` under status `EXTRACTED`.

An initial run reported 404 on every `/api/gate-documents/*` path; the correct router
prefix is **`/api/gate-docs`** ([gateway/routers/gate_documents.py:40](gateway/routers/gate_documents.py#L40)).
Docs and web client already use the correct prefix — my paths were wrong, not the code.

---

## 7. Workflow 5 — Evidence / Demo Readiness · 45/45 PASS

44 dashboard/report endpoints + the Follow-the-Box journey, all 200.

```
/healthz                      /api/kpi/strip                /api/kpi/throughput
/api/kpi/sources              /api/kpi/cameras              /api/alerts
/api/anpr/cameras             /api/reports/police           /api/violations/catalog
/api/cargo                    /api/cargo/scan-queue         /api/cargo/events
/api/gate-docs/summary        /api/cfs-ecy/stats            /api/cfs-ecy/chains/stats
/api/jobs                     /api/gate/events              /api/yard/movements
/api/scan/machines            /api/scan/events              /api/traffic/current
/api/traffic/snapshots        /api/bottlenecks              /api/weather/current
/api/air-quality/current      /api/logistics/current        /api/performance/kpi
/api/customs/summary          /api/shipping-lines/summary   /api/berthing/stats
/api/marine/calls             /api/parking/facilities       /api/carbon/rollup
/api/empty/kpi                /api/trt/summary              /api/reefer/availability
/api/vehicles/stats           /api/drivers/master           /api/transporters
/api/export/bookings          /api/workflows/catalog        /api/workflows/rules
/api/workflows/executions     /api/scenarios                /api/journey/container/{cn}
```

Sample payloads confirming real data:

```
/api/customs/summary   {"igm_vessels":16,"igm_containers":12235,"ooc":8,"smtp":6,
                        "smtp_lines":209,"rms_scanlists":4,"rms_containers":98,"leo":100}
/api/shipping-lines/summary  {"advance_containers":8878,"distinct_containers":8854,
                              "delivery_orders":9,"shipping_lines":160,"with_bl":302}
/api/cfs-ecy/stats     {"total_events":1930,"container_count":1203,"average_dwell_hours":71.04}
/api/gate/events       {"id":213576,"plate":"MH06KD3383","gate_id":"G-BMCT","trip_id":"TRK-002292:6"}
/api/parking/facilities {"decision_path":"RDS_DIRECT","source":"rds",...}
/api/traffic/current   {"status":"LIVE","source":"TOMTOM","decision_path":"LIVE"}
/api/bottlenecks       {"count":3,"bottlenecks":[{"rank":1,"segment_id":"SEG-00","jam_factor":7.5}]}
/api/scan/machines     {"machine_code":"D-INNSA1RSDT01","machine_class":"DRIVE_THROUGH"}
/api/workflows/executions  [{"results":[{"name":"Over-speed → violation","matched":...}]}]
```

### Panels that will render empty

```
/api/kpi/sources            {"sources":[],"count":0}
/api/reefer/availability    {"facilities":[],"totals":{"total":0,"available":0,...,"free_pct":0.0}}
```

### Other observations

- `/api/kpi/cameras` and `/api/anpr/cameras` report `decision_path: SYNTHETIC` for every camera — correct with no live feed, but state it explicitly in a demo.
- `GET /api/berthing/summary` → **422** (not 404): `/api/berthing/{report_id}` shadows the literal path segment. Route-ordering hygiene issue, harmless today.
- `/api/journey/container/MSMU1908508` → 200 with `found: false`, `gate_record_source: "none"` — the container has gate documents but no journey/gate spine record, so Follow-the-Box returns an empty-but-well-formed envelope.

---

## 8. Reproducing

```bash
cd "<repo>"
.venv/bin/python -m pytest $(ls tests/test_*.py | head -40) -q          # suite, batch A
.venv/bin/python -m pytest $(ls tests/test_*.py | sed -n '41,80p') -q   # batch B
.venv/bin/python -m pytest $(ls tests/test_*.py | sed -n '81,200p') tests/e2e -q
```

The five workflow harness scripts (`wf1_cargo.py` … `wf5_demo.py` + `smoke_common.py`)
were run from a scratch directory and deliberately **not** committed — they hardcode an
absolute repo path and write QA rows to the shared RDS. §2 above contains the full harness
so they can be recreated; each script is a linear sequence of `TestClient` calls plus
`psycopg` assertions, recorded through a small `Recorder` helper that prints the
`[PASS]/[FAIL]` lines reproduced in this document.

Start the full stack first (`make up`) to close the environment-limited gaps in §5 and §6
— `truck-sim`, `ai/congestion`, `eir-ocr` and MinIO were down for this audit.
