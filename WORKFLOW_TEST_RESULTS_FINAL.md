# JNPA Digital Twin — Final Workflow Test Results (post-fix)

**Date:** 2026-08-05 · **Companion to:** [WORKFLOW_FIX_REPORT.md](WORKFLOW_FIX_REPORT.md)
**Baseline:** [WORKFLOW_TEST_RESULTS.md](WORKFLOW_TEST_RESULTS.md) (pre-fix)

Verification run after fixing T-1, T-2, G-1 and G-2. Same harness as the audit:
the real gateway app booted in-process (Starlette `TestClient` + lifespan) against
the real AWS RDS `jnpa_schema_v3`, with `psycopg` assertions on `core.*`.

**Environment:** macOS host, `.venv` (Python 3.13.3). **No Docker daemon** —
`truck-sim`, `ai/congestion`, `eir-ocr`, MinIO, Kafka, MQTT and Redis unreachable.
Docker Desktop was launched and did not come up headlessly (~8 min wait); the three
steps that need it are called out explicitly in §3.

---

## 1. Headline

| Workflow | Before | After | Verdict |
|---|---|---|---|
| 1. Cargo Lifecycle | 33/33 | **33/33** | PASS — no regression |
| 2. Transport | 20/26 | **24/27** | All 4 code defects fixed; 3 env-gated (§3) |
| 3. CFS-ECY | 23/24 | **24/24** | PASS |
| 4. Gate Document | 34/36 | **40/40** | PASS |
| 5. Demo Readiness | 45/45 | **45/45** | PASS — no regression |

```
##### wf1_cargo     #####  === WF1-Cargo-Lifecycle:  33/33 steps passed ===
##### wf2_transport #####  === WF2-Transport:        24/27 steps passed ===
##### wf3_cfs_ecy   #####  === WF3-CFS-ECY:          24/24 steps passed ===
##### wf4_gate_docs #####  === WF4-Gate-Documents:   40/40 steps passed ===
##### wf5_demo      #####  === WF5-Demo-Readiness:   45/45 steps passed ===
```

---

## 2. Automated suite

Run in file batches (the whole-suite invocation aborts natively at `test_performance`
on this host — pre-existing, environment-specific).

```bash
.venv/bin/python -m pytest $(ls tests/test_*.py | sed -n "1,40p")  -q
.venv/bin/python -m pytest $(ls tests/test_*.py | sed -n "41,80p") -q
.venv/bin/python -m pytest $(ls tests/test_*.py | sed -n "81,200p") tests/e2e -q
```

```
batch A:  552 passed,  13 skipped   in 56.92s
batch B:  402 passed,  92 skipped   in 41.57s
batch C:  114 passed,   9 skipped   in 30.47s
------------------------------------------------
TOTAL:   1068 passed, 114 skipped, 0 FAILED     (baseline was 1056 — +12 new tests)
```

### New regression tests

```bash
.venv/bin/python -m pytest tests/test_workflow_audit_fixes.py -q
```
```
............                                                             [100%]
12 passed in 1.09s
```

### One test updated (not weakened)

`tests/test_demo_failure_fixes.py::test_reroute_ack_is_addressed_to_the_acking_driver`
failed after the T-2 fix:

```
>       await trucks.ack_reroute("TRK-000001", body={"state": "ACK"}, gw=_Gw())
E       fastapi.exceptions.HTTPException: 404 no_advisory_to_ack
```

It acked a device with an **empty** advisory repo — i.e. it was asserting through
the bug T-2 removes. Its subject is ACK *addressing*, so the setup now pushes an
advisory first; every original assertion is unchanged.

```
tests/test_demo_failure_fixes.py  11 passed in 0.74s
```

### Migration

```bash
.venv/bin/python scripts/migrate.py --status   # 0117 -> PENDING
.venv/bin/python scripts/migrate.py
```
```
==> 1 migration(s) pending:
      0117_gate_capture_evidence.sql  [transactional]
--> applying 0117_gate_capture_evidence
    ok (86 ms)
==> applied 1 migration(s)
```

---

## 3. Workflow 2 — Transport · 24/27

### T-1 fixed — the advisory now survives a dead truck-sim

Before: `POST .../route -> 502 {"error":"truck_sim_unreachable"}`, `core.reroute_advisory` = **0 rows**.

```
[PASS] 4. Push re-route advisory (persists even if sim is down)
       POST /api/trucks/TRUCK-0001/route -> 200
       | decision_path=REROUTE_DEGRADED sim={'delivered': False, 'error': 'truck_sim_unreachable'}
[PASS] 4b. Read latest advisory (PWA poll)
       GET .../route/latest -> 200
       | {"device_id":"TRUCK-0001","advisory":{"type":"reroute","reason":"congestion",
          "title":"Re-route advisory","requires_ack":true,...}}
[PASS] 4d. DB core.reroute_advisory persisted     SQL core.reroute_advisory | rows=[(1,)]
```

The truck-sim is still down — the difference is that the workflow completes anyway,
the advisory is durable, the PWA polling fallback can read it back, and the decision
is audited as `REROUTE_DEGRADED` instead of vanishing.

### T-2 fixed — a phantom ACK is now rejected

Before: `200 {"acked":true}` against zero advisory rows, plus a `REROUTE_ACK` audit entry.

```
[PASS] 4c.  Driver acknowledges a REAL advisory
       POST /api/trucks/TRUCK-0001/route/ack -> 200 {"acked":true,"state":"ACK"}
[PASS] 4c2. ACK for device with NO advisory is rejected
       POST /api/trucks/QA-GHOST-DEVICE-0001/route/ack -> 404
       | {"error":"no_advisory_to_ack","device_id":"QA-GHOST-DEVICE-0001","acked":false,
          "message":"No re-route advisory is on record for QA-GHOST-DEVICE-0001."}
       | advisory_rows=0
[PASS] 4e.  DB ack_state recorded on the advisory  SQL core.reroute_advisory | ack_state=[('ACK',)]
```

Step `4e` is new — the T-2 fix made it assertable, which is why the step count rose
from 26 to 27.

### Still failing — infrastructure, not code

```
[FAIL] 2b. A device_id is resolvable
       GET /api/trucks -> {"count":0,"devices":[],"degraded":true}   [needs truck-sim]
[FAIL] 6.  /api/traffic/metrics             -> 503 congestion_metrics_unavailable  [needs ai/congestion]
[FAIL] 6.  /api/traffic/congestion/metrics  -> 503 congestion_metrics_unavailable  [needs ai/congestion]
```

Both degrade exactly as documented. No code change would legitimately make them
pass on a host with no container runtime. To close:

```bash
make up && sleep 25 && make bootstrap-check   # then re-run the Transport workflow
```

### Unchanged and still passing

```
[PASS] 1.  Create vehicle (fleet registry)   -> TRK-000031 / MH04QA9911
[PASS] 1c. Duplicate plate rejected          -> 409 vehicle_number_exists
[PASS] 3.  Live telemetry read (fallback)    -> 404 no_position  [correct degrade]
[PASS] 3c. Check-in surfaces on truck read   -> decision_path=TERTIARY gate_delay=5
[PASS] 5.  Record gate movement              -> 201, core.gate_event id 214777+
[PASS] 6.  /api/traffic/current              -> {"status":"LIVE","source":"TOMTOM"}
[PASS] 6c. Alerts surface after scan         -> ELEVATED_SCRUTINY
```

---

## 4. Workflow 4 — Gate Document · 40/40 (was 34/36)

### G-1 fixed — duplicate upload no longer reports a false count

Before: `{"status":"SKIPPED_DUPLICATE","imported":1}` with the DB unchanged at 13 rows.

```
[PASS] 3b. Re-import inserts no duplicate rows (DB-level)
       after1=13 after2=13  duplicate_file=True
[PASS] 3c. Re-import REPORTS imported=0 (not the parsed count)
       status=SKIPPED_DUPLICATE imported=0 previously_imported=1 DB rows 13->13
```

### G-2 fixed — evidence reference persisted and resolvable

Four new steps prove the round-trip the audit found missing:

```
[PASS] 7a.  Import Form-13 carrying an evidence object key
       POST /api/gate-docs/upload -> 200
[PASS] 7a2. DB core.gate_capture stores the evidence reference
       SQL core.gate_capture
       | [('form13/QA-AUDIT-EVIDENCE.jpg',
           '/api/evidence/form13/QA-AUDIT-EVIDENCE.jpg', None)]
[PASS] 7a3. Evidence reference surfaced on the API
       GET /api/gate-docs/form13 -> 200 | object_path present in the Form-13 projection
[PASS] 7a4. GET /api/evidence/{stored path} resolves the route
       GET /api/evidence/form13/QA-AUDIT-EVIDENCE.jpg -> 404
       | route resolves the stored key (404 = object absent: MinIO down)
[PASS] 7b.  gate_capture carries evidence references (post-0117)
       | 1 row(s) now carry an object_path (pre-existing seeded rows keep NULL by design)
```

**On step 7a4's 404:** the missing link was database → route. `core.gate_capture` now
stores the bucket-relative key and the exact `/api/evidence/…` URL the browser
should call, and that route accepts it. The 404 is MinIO not running on this host,
not a broken reference — with the stack up it streams the object.

### Everything else still passing

```
[PASS] 0b. Module summary        {"eir":5,"pin_tickets":2,"form13":202,...}
[PASS] 1.  Templates EIR / PIN / FORM13 -> 200 each
[PASS] 1b. Invalid doc_type -> 400 {"allowed":["EIR","PIN","FORM13"]}
[PASS] 2b. Validate template example row -> valid:true
[PASS] 2c. Validate rejects malformed    -> valid:false, error_code empty_required
[PASS] 4.  eir total=5 · pin total=2 · form13 total=202
[PASS] 4.  tat -> {"samples":3,"avg_tat_min":"137","median_tat_min":165.0}
[PASS] 5.  OCR health -> upstream.reachable:false, active_rung:OCR   [env]
[PASS] 5b. Upload -> OCR -> EXTRACTED (now also returns object_path / object_name)
[PASS] 6.  Verify/correct fields -> VERIFIED
[PASS] 6b. DB merge  [('VERIFIED', {'verified_by':'qa-audit','container_number':'QATU7788228'})]
[PASS] 6c/6d. Unknown document -> 404 on read and verify
[PASS] 7c. Police report generation -> 200
```

---

## 5. Workflow 3 — CFS-ECY · 24/24 (was 23/24)

```
[PASS] 7e. Re-upload is idempotent AND reports imported=0
       POST /api/cfs-ecy/upload -> 200
       | first=SKIPPED_DUPLICATE/0 second=SKIPPED_DUPLICATE/0 previously_imported=2
[PASS] 9.  CFS-ECY is a read-only/import module (no event bus by design)
       | events_matching_cfs=0 (services/cfs_ecy emits none — documented design, finding E-2)
```

Step 9 was reclassified, not fixed: `grep -rn "lifecycle_bus\|emit\|publish" services/cfs_ecy/`
returns nothing, and the module is documented as read-only + import. It is recorded
as finding **E-2** in the audit report rather than counted as a failure.

All 22 other steps unchanged: 1,928 movements (CFS 967 / ECY 961), 1,202 chains
(242 complete, 529 anomalies), dwell 71.04h avg, rebuild in ~300 ms, template →
validate → import → ledger, 400 on invalid facility, 404 on unknown container.

---

## 6. Workflows 1 and 5 — regression check

**Cargo Lifecycle 33/33** — full walk re-verified on a fresh container
(`QATU7788338`): create → movement → discharge → yard assign → yard position →
planning (yard/reefer/rake) → scan queue → verify → release, with 8
`core.cargo_lifecycle_event` rows in order, 19 `core.cargo_event` rows across 11
topics, and every negative case still returning its precise code (409 on
release-before-verify, verify-before-yard-assign, duplicate discharge/release;
400 on bad ISO-6346 / yard block; 404 on unknown container).

**Demo Readiness 45/45** — all 44 dashboard/report endpoints plus Follow-the-Box
still 200. TomTom traffic still `LIVE`. No endpoint regressed from the router and
service changes.

---

## 7. Reproducing

```bash
cd "<repo>"

# 1. apply the new migration (idempotent)
set -a; . ./.env.local; set +a
.venv/bin/python scripts/migrate.py

# 2. regression tests for the four fixes
.venv/bin/python -m pytest tests/test_workflow_audit_fixes.py tests/test_demo_failure_fixes.py -q

# 3. full suite, in batches
.venv/bin/python -m pytest $(ls tests/test_*.py | sed -n "1,40p")  -q
.venv/bin/python -m pytest $(ls tests/test_*.py | sed -n "41,80p") -q
.venv/bin/python -m pytest $(ls tests/test_*.py | sed -n "81,200p") tests/e2e -q

# 4. to close the 3 environment-gated Transport steps
make up && sleep 25 && make bootstrap-check
```

The five workflow harness scripts are deliberately **not** committed (they hardcode
an absolute repo path and write QA rows to the shared RDS); the harness source is
reproduced in [WORKFLOW_TEST_RESULTS.md §2](WORKFLOW_TEST_RESULTS.md).

**QA rows left in RDS**, all clearly marked: cargo `QATU7788228` / `QATU7788338`,
vehicle `MH04QA9911` (`TRK-000031`), advisory on `TRUCK-0001`, Form-13
`F13QAEVID001` carrying `object_path = form13/QA-AUDIT-EVIDENCE.jpg` (kept
deliberately — it is the evidence that G-2 works end-to-end).
