# JNPA Digital Twin — Workflow Fix Report

**Date:** 2026-08-05
**Scope:** Remediation of the open findings in [WORKFLOW_AUDIT_REPORT.md](WORKFLOW_AUDIT_REPORT.md) — T-1, T-2, G-1, G-2.
**Companion:** [WORKFLOW_TEST_RESULTS_FINAL.md](WORKFLOW_TEST_RESULTS_FINAL.md) — commands, output, failed scenarios.
**Not committed** — working tree only.

---

## 1. Result

| Workflow | Before | After | Status |
|---|---|---|---|
| 1. Cargo Lifecycle | 33/33 | **33/33** | PASS (no regression) |
| 2. Transport | 20/26 | **24/27** | **All 4 code defects fixed.** 3 remaining steps are environment-gated (Docker down) — see §6 |
| 3. CFS-ECY | 23/24 | **24/24** | PASS |
| 4. Gate Document | 34/36 | **40/40** | PASS |
| 5. Demo Readiness | 45/45 | **45/45** | PASS (no regression) |

**Automated suite: 1,068 passed / 114 skipped / 0 failed** (was 1,056 — +12 new regression tests).

Every defect the audit raised against these four findings is fixed and pinned by a
test. The three Transport steps still failing need `truck-sim` and `ai/congestion`
running; they are not code defects and cannot be closed on a host with no Docker
daemon. I attempted to start Docker Desktop to close them and it would not come up
headlessly — details and the exact steps in §6.

---

## 2. Files changed

| File | Fix | Change |
|---|---|---|
| [gateway/routers/trucks.py](gateway/routers/trucks.py) | T-1, T-2 | Re-ordered the re-route flow; ACK now validated (+98/−…) |
| [services/gate_documents/service.py](services/gate_documents/service.py) | G-1 | `imported: 0` on `SKIPPED_DUPLICATE` |
| [services/cfs_ecy/upload_service.py](services/cfs_ecy/upload_service.py) | G-1 (E-1) | same contract, same fix |
| [services/gate_documents/repository.py](services/gate_documents/repository.py) | G-2 | Persist + project the evidence object reference |
| [services/gate_documents/upload_parsers.py](services/gate_documents/upload_parsers.py) | G-2 | Optional evidence-key column (template unchanged) |
| [gateway/routers/document_ocr.py](gateway/routers/document_ocr.py) | G-2 | Store a resolvable proxy path, not `s3://…` |
| [gateway/routers/evidence.py](gateway/routers/evidence.py) | G-2 | Resolve objects from both evidence buckets |
| [infra/postgres/v3/0117_gate_capture_evidence.sql](infra/postgres/v3/0117_gate_capture_evidence.sql) | G-2 | **new** — additive columns on `core.gate_capture` |
| [tests/test_workflow_audit_fixes.py](tests/test_workflow_audit_fixes.py) | all | **new** — 12 regression tests |
| [tests/test_demo_failure_fixes.py](tests/test_demo_failure_fixes.py) | T-2 | Test setup updated for the new ACK precondition (§3.2) |

Architecture untouched. Every change follows the existing
router → service → repository → `core.*` layering, and no existing business rule
was altered beyond the four defects.

---

## 3. Fix detail

### 3.1 · T-1 — Re-route advisory lost when the truck-sim is down · **High**

**Root cause.** [gateway/routers/trucks.py](gateway/routers/trucks.py) called the
truck-sim as the *first* statement of `reroute_truck` and raised
`502 truck_sim_unreachable` from the `except httpx.HTTPError` branch. Everything
that actually serves the driver sat *below* that raise and was therefore
unreachable on a sim outage:

```
    truck-sim POST            <- raises 502 here
    record_decision(...)      <- never ran
    LAST_REROUTE[device_id]   <- never ran
    advisory_repo.save(...)   <- never ran  -> core.reroute_advisory stayed empty
    notifications.dispatch()  <- never ran  -> no WS / WebPush / FCM
    send_sms(...)             <- never ran
```

A simulator being down silently deleted the entire driver-advisory workflow — in a
router that implements a documented three-rung fallback for *reads* on the very
next endpoint.

**Fix applied.** The sim is demoted to an optional downstream. Order is now
exactly as specified:

```
    validate / build advisory from the REQUEST   (no dependency on the sim's reply)
        -> LAST_REROUTE  (hot cache)
        -> advisory_repo.save()                  (durable, core.reroute_advisory)
        -> notifications.dispatch()              (WS + WebPush + FCM)
        -> send_sms()                            (env-gated)
        -> truck-sim POST                        (OPTIONAL — failure degrades)
        -> record_decision(REROUTE | REROUTE_DEGRADED)
```

- A reachable sim still enriches the advisory with `dest` / `route_km` and re-saves it; the decision is audited as `REROUTE` / `SourceState.LIVE` exactly as before.
- An unreachable (or 4xx/5xx) sim returns **200** with `sim: {delivered: false, error: "truck_sim_unreachable"}`, `persisted: true` and `decision_path: "REROUTE_DEGRADED"` (`SourceState.DEGRADED`).
- No 502 is raised from this route any more; the advisory always survives.

Response gains `persisted`, `decision_path` and `sim` — additive keys, existing
consumers unaffected.

### 3.2 · T-2 — ACK reported success for an advisory that never existed · **Medium**

**Root cause.** `AdvisoryRepository.ack()` already returned
`bool(res.rowcount)` from its `UPDATE core.reroute_advisory … WHERE device_id = :d`
([services/advisory/repository.py:82](services/advisory/repository.py#L82)), but the
route discarded the value and answered `{"acked": true}` unconditionally — *after*
writing a `REROUTE_ACK` decision-audit record. An ACK for a device that was never
pushed anything therefore fabricated a push → driver → ACK round-trip in the
evidence trail.

**Fix applied.** The ACK is applied **first** and the result is honoured:

- Row updated → `200 {"acked": true, "state": …}`, decision recorded, ACK frame broadcast to that driver only (the pre-existing addressing guarantee is preserved).
- No row **and** no advisory in the hot cache → `404 {"error": "no_advisory_to_ack", "acked": false, "device_id": …}`, **no** decision record, **no** WS frame.

The hot-cache clause is deliberate: an advisory the driver can demonstrably see
must remain ack-able even when the durable write was best-effort (no DSN
configured). It never invents an advisory that does not exist in either place.

**Test updated, not weakened.** `test_reroute_ack_is_addressed_to_the_acking_driver`
in [tests/test_demo_failure_fixes.py](tests/test_demo_failure_fixes.py) acked a
device with an empty repo — it was asserting through the bug. Its subject is ACK
*addressing*, so the setup now pushes an advisory first and every original
assertion is unchanged.

### 3.3 · G-1 / E-1 — Duplicate upload reported a false `imported` count · **Medium**

**Root cause.** Both upload services returned `result["imported_count"]` verbatim.
On a re-upload the repository correctly detects the duplicate by content hash,
writes nothing and returns `import_status = SKIPPED_DUPLICATE` — but
`imported_count` still carried the count from the **original** import. The
response therefore read `{"status": "SKIPPED_DUPLICATE", "imported": 2}` while
zero rows had landed.

**Fix applied**, identically in both services:

```python
imported = 0 if status == "SKIPPED_DUPLICATE" else result["imported_count"]
...
if status == "SKIPPED_DUPLICATE":
    out["previously_imported"] = result["imported_count"]
```

- `imported` now reports what **this request** persisted.
- The original import's count is not lost — it is surfaced as `previously_imported` (additive) and remains in the upload ledger.
- **DB behaviour is untouched.** Persistence was already correctly idempotent (verified in the audit: identical content under a different filename inserted 0 rows). Only the reported number changed.

### 3.4 · G-2 — Gate captures carried no evidence object reference · **Medium**

**Root cause — two independent halves.**

1. `core.gate_capture` had no column for an object reference at all, so
   `GET /api/evidence/{object_path}` had nothing in the database to resolve. All
   808 rows held document metadata only.
2. `_store_document` in [gateway/routers/document_ocr.py](gateway/routers/document_ocr.py)
   persisted `s3://{bucket}/{object_name}` — an **internal** URI a browser cannot
   reach. Even with MinIO up, a stored document could never be retrieved from its
   own persisted reference. (`gateway/routers/violations.py` already had this
   right, returning `/api/evidence/{object_name}`.)

**Fix applied.**

*Migration* [0117_gate_capture_evidence.sql](infra/postgres/v3/0117_gate_capture_evidence.sql) —
fully additive, idempotent, applied cleanly to RDS in 86 ms:

```sql
ALTER TABLE core.gate_capture
    ADD COLUMN IF NOT EXISTS object_path  text,   -- bucket-relative key
    ADD COLUMN IF NOT EXISTS evidence_uri text,   -- /api/evidence/<object_path>
    ADD COLUMN IF NOT EXISTS object_name  text;   -- original filename
CREATE INDEX IF NOT EXISTS idx_gate_capture_object_path
    ON core.gate_capture (object_path) WHERE object_path IS NOT NULL;
```

Three NULLable columns and one partial index. No column dropped, no CHECK changed,
no existing row invalidated — pre-0117 rows keep NULL, meaning "no evidence object
on record".

*Repository* — the Form-13 insert persists the reference and a new
`evidence_uri_for()` helper builds the client-facing URL using the same convention
as `violations.py`; `_FORM13_SELECT` projects all three columns so the API exposes
them.

*Parser* — an **optional** `object_path` field with aliases (`objectpath`,
`imagefile`, `image`, `scanfile`, `evidence`, `evidencepath`, …). Deliberately
**not** added to the template: a document-only Form-13 stays valid and every
existing upload keeps working.

*OCR + evidence route* — `_store_document` now returns `/api/evidence/{object}`,
and `GET /api/evidence/{path}` searches both the `evidence` and `documents`
buckets, so a correctly-referenced OCR document no longer 404s purely for living
in the sibling bucket. The OCR response gains `object_path` / `object_name`.

**Verified round-trip** (WF4 steps 7a–7a4): a Form-13 upload carrying
`form13/QA-AUDIT-EVIDENCE.jpg` persisted
`object_path = 'form13/QA-AUDIT-EVIDENCE.jpg'`,
`evidence_uri = '/api/evidence/form13/QA-AUDIT-EVIDENCE.jpg'`, the reference is
surfaced on `GET /api/gate-docs/form13`, and `GET /api/evidence/{that path}`
resolves the route. It returns 404 rather than bytes **only** because MinIO is not
running on this host — the database→route linkage that was missing is now in place.

---

## 4. Tests

```
tests/test_workflow_audit_fixes.py .............  12 passed
```

| Test | Pins |
|---|---|
| `test_reroute_is_persisted_when_the_truck_sim_is_down` | T-1 — advisory + audit survive a dead sim |
| `test_reroute_still_enriches_from_a_live_truck_sim` | T-1 — happy path not regressed |
| `test_ack_without_an_advisory_is_rejected` | T-2 — 404, no phantom audit, no WS frame |
| `test_ack_succeeds_for_a_real_advisory` | T-2 — legitimate ACK still works, audited once |
| `test_gate_doc_duplicate_upload_reports_zero_imported` | G-1 |
| `test_cfs_ecy_duplicate_upload_reports_zero_imported` | G-1 (E-1) |
| `test_fresh_upload_still_reports_its_real_imported_count` | G-1 — genuine imports not zeroed |
| `test_evidence_uri_is_the_gateway_proxy_path` | G-2 |
| `test_form13_insert_carries_the_evidence_reference` | G-2 |
| `test_form13_without_evidence_stays_valid` | G-2 — additive |
| `test_form13_parser_picks_up_an_evidence_column` | G-2 — optional column, template unchanged |
| `test_evidence_router_searches_both_buckets` | G-2 |

Written in the existing in-memory-fake style of `tests/test_demo_failure_fixes.py` —
no database, no network, so they run in CI as-is.

---

## 5. Findings deliberately not changed

The remaining audit findings are lower severity and each would touch behaviour
beyond the four items in scope. Left as-is, unchanged from the audit report:
**C-1** (`cargo.verified` on a failed scan), **C-2** (`SCAN_PENDING` in the declared
contract), **C-3** (yard plan vs. assignment), **G-3** (MOCK OCR confidence 0.75),
**T-3** (`trip_id` derivation), **E-2** (CFS-ECY emits no events), the berthing route
ordering, and **§8.1** — the DB-integration tests hardcoded to `localhost:5433`,
which remains the highest-value process fix.

---

## 6. Why Transport is 24/27, not 26/26

The three remaining failures are **infrastructure**, not code. They were
environment-limited in the original audit and stayed that way:

| Step | Needs | Behaviour |
|---|---|---|
| `2b. A device_id is resolvable` | `truck-sim` container | `/api/trucks` returns `{count: 0, degraded: true}` — no device to resolve |
| `6. /api/traffic/metrics` | `ai/congestion` container | 503 `congestion_metrics_unavailable` — the documented degradation |
| `6. /api/traffic/congestion/metrics` | `ai/congestion` container | same |

There is no Docker daemon on this host. I launched Docker Desktop and waited ~8
minutes; the daemon never became available (it needs GUI sign-in), so these could
not be closed here. They are not defects and no code change would legitimately make
them pass — masking a missing dependency would defeat the point of the fallback
posture the audit confirmed is working.

**To close them:**

```bash
make up && sleep 25 && make bootstrap-check
```

then re-run the Transport workflow. Step count also rose from 26 to 27 because the
T-2 fix made a new assertion possible: `4e. DB ack_state recorded on the advisory`,
which verifies the ACK actually reached `core.reroute_advisory.ack_state`.

---

## 7. Notes

- **Migration 0117 has been applied to the live RDS** (`jnpa_schema_v3`) via `scripts/migrate.py`; the ledger records it. It is idempotent and safe to re-run.
- **QA rows left in the shared RDS**, all clearly marked: cargo `QATU7788228` / `QATU7788338`, vehicle `MH04QA9911` (`TRK-000031`), advisory on `TRUCK-0001`, and Form-13 `F13QAEVID001` with `object_path = form13/QA-AUDIT-EVIDENCE.jpg` (kept deliberately — it is the evidence that G-2 works end-to-end). Delete when convenient.
- **Not committed**, as instructed. `git status` shows 8 modified files and 4 new (2 audit reports, the migration, the regression tests).
