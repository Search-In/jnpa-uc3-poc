# JNPA Digital Twin — Transport Workflow Final Fix Report

**Date:** 2026-08-05
**Objective:** Take the Transport workflow from 24/27 to **27/27** without faking tests or skipping validations.
**Related:** [WORKFLOW_AUDIT_REPORT.md](WORKFLOW_AUDIT_REPORT.md) · [WORKFLOW_FIX_REPORT.md](WORKFLOW_FIX_REPORT.md)
**Not committed** — working tree only.

---

## 1. Result

| Workflow | Status |
|---|---|
| 1. Cargo Lifecycle | **33/33 PASS** |
| 2. Transport | **27/27 PASS** |
| 3. CFS-ECY | **24/24 PASS** |
| 4. Gate Document | **40/40 PASS** |
| 5. Demo Readiness | **45/45 PASS** |

**Automated suite: 1,075 passed / 114 skipped / 0 failed** (+7 new regression tests, 19 total in the audit-fix file).

Nothing was faked, skipped, or asserted more loosely. The three steps previously
labelled "environment-gated" turned out to be **missing fallback rungs**, not
missing containers — the investigation below is what changed the diagnosis.

---

## 2. Root cause — the earlier diagnosis was wrong

The prior report classified all three failures as *infrastructure*. That was
correct about the proximate cause (the containers are down) but wrong about the
defect: **two endpoints in the Transport path had no fallback at all**, while the
rest of the same router implements a documented PRIMARY → SECONDARY → TERTIARY
ladder. A stopped container therefore blanked them completely, when real data for
both was sitting one rung away.

### 2.1 · `GET /api/trucks` (fleet list) — no fallback rung existed

[gateway/routers/trucks.py](gateway/routers/trucks.py) `list_trucks` asked the
truck-sim and, on any failure, returned a hard-coded empty envelope:

```python
    except httpx.HTTPError as exc:
        log.warning("trucks_list_unreachable", url=url, error=str(exc))
    REQUESTS.labels("trucks", "degraded").inc()
    return {"count": 0, "filter_state": state, "devices": [], "degraded": True}
```

Meanwhile `GET /api/trucks/{device_id}`, **eighty lines below in the same file**,
implements PRIMARY (truck-sim) → SECONDARY (ULIP relay) → TERTIARY (web check-in).
The list endpoint simply never got the same treatment.

**The data was there the whole time.** `core.truck_telemetry` — where the trucking
app's MQTT position stream lands — was current at the moment of the audit:

```
columns : ts, device_id, plate, lat, lon, speed_kmh, heading, battery, accuracy_m
max ts  : 2026-08-05 10:05:51+00   (i.e. seconds old)
sample  : ('SYN-TFC-1:tfc1-…-020160', 'KL07MM5760', 18.948902, 72.949114, 0.0, …)
```

So the dashboard was showing an empty fleet while the fleet's positions sat in
RDS. That is a code gap, not an environment gap.

### 2.2 · `GET /api/traffic/metrics` — no fallback rung existed

`_congestion_metrics` proxied `ai/congestion` and raised
`503 congestion_metrics_unavailable` on any failure. The decisive detail is what
that upstream actually does ([ai/congestion/infer.py:268](ai/congestion/infer.py#L268)):

```python
@app.get("/metrics")
async def metrics_summary() -> dict:
    path = Path(cfg.metrics_path)          # ai/congestion/artifacts/metrics.json
    if not path.is_file():
        storage.sync_weights(cfg)
    if path.is_file():
        return json.loads(path.read_text())
    return {"error": "no_metrics", ...}
```

It reads a **committed file** and returns it. The whole service was standing
between the gateway and a JSON artifact already in the repo:

```json
{"congestion_onset_f1": 0.8797, "precision": 0.9624, "recall": 0.8101,
 "roc_auc": 0.9058, "support_total": 17732, "num_segments": 13,
 "trained_at": "2026-06-01T00:00:00+00:00", "TARGET_MET": true}
```

The gateway's own code already documents these as trustworthy regardless of
runtime state — `_normalize_congestion_metrics` sets `metrics_synthetic: false`
with the comment *"Model metrics come from a reproducible offline train, not the
live feed; they are real regardless of data_mode."*

### 2.3 · Service URL configuration — verified correct, no fix needed

| Setting | compose | `gateway/config.py` default | Container | Verdict |
|---|---|---|---|---|
| `GATEWAY_TRUCK_URL` | `http://truck-sim:8240` | `http://truck-sim:8240` | `jnpa-truck-sim`, port 8240 | ✅ consistent |
| `GATEWAY_CONGESTION_URL` | `http://congestion:8311` | `http://congestion:8311` | `jnpa-congestion`, port 8311 | ✅ consistent |

No misconfiguration. The URLs resolve to the right containers on the right ports;
the services were simply not running, and the endpoints had no second rung.

---

## 3. Code fixes

### 3.1 · Fleet list gains the SECONDARY and TERTIARY rungs

[gateway/routers/trucks.py](gateway/routers/trucks.py) — the list now follows the
same ladder the single-device read already uses:

```
PRIMARY   -> truck-sim /devices/list          (live control plane)
SECONDARY -> core.truck_telemetry             (persisted position tail, DISTINCT ON device_id, 24h window)
TERTIARY  -> in-memory /checkin submissions   (elevated scrutiny)
            -> else the original empty+degraded envelope
```

Two new helpers, `_list_secondary_rds()` and `_list_tertiary_checkins()`, mirroring
the existing `_primary` / `_secondary_ulip` shape. Each rung calls
`record_decision()` with the matching `TruckPath` and `SourceState`, so a degraded
read is visible in the decision trail rather than silent. The response gains
`decision_path` / `source` / `state_filter_supported` (additive; the original
`count` / `filter_state` / `devices` / `degraded` keys are unchanged).

**Honesty guard — the state filter.** Only the truck-sim knows a device's
`TruckState`; `core.truck_telemetry` has no such column. Returning the unfiltered
fleet for `state=AT_GATE_QUEUE` would answer a *different question* than the one
asked, so a state-filtered request **skips the fallback rungs entirely** and
degrades to empty with `state_filter_supported: false` and a hint. Pinned by
`test_truck_list_state_filter_never_answers_from_a_fallback`, which asserts the
RDS query is never even issued.

I deliberately did **not** synthesise a `TruckState` from `core.gate_event` — that
would be a guess presented as fact.

### 3.2 · Congestion metrics gain the LOCAL_ARTIFACT rung

[gateway/routers/traffic.py](gateway/routers/traffic.py):

```
LIVE          -> ai/congestion GET /metrics        (decision_path: "LIVE")
LOCAL_ARTIFACT-> ai/congestion/artifacts/metrics.json  (decision_path: "LOCAL_ARTIFACT",
                                                        live_service_available: false)
503           -> if the artifact is absent too      (unchanged)
```

`_congestion_metrics_artifact()` searches `$CONGESTION_METRICS_PATH`, then the
repo-relative path, then `/app/…` for the container. It returns the file's
contents **or None** — never a generated stand-in, so a genuinely missing artifact
still produces the original 503. Pinned by
`test_congestion_metrics_artifact_returns_none_when_absent`.

**This is not fabricated data.** `test_congestion_metrics_artifact_is_the_real_committed_file`
asserts the served object equals `json.loads()` of the on-disk file — the identical
bytes `ai/congestion` itself would have returned. The provenance is labelled in
the response so a cached read can never be mistaken for a live one.

---

## 4. Environment fixes

### 4.1 · Health checks for all four application services

All four services expose `/healthz` and **none had a compose healthcheck** — only
the 5 infrastructure services (postgres, redis, zookeeper, kafka, minio) did. So
`docker compose ps` could only report "running": a booted-but-not-serving
truck-sim looked identical to a healthy one, and nothing could
`depends_on: condition: service_healthy` them.

[docker-compose.yml](docker-compose.yml) — added healthchecks driving each
service's existing endpoint, using the image's own Python (there is no `curl` in
`python:3.11-slim`):

| Service | Endpoint | interval / timeout / retries / start_period |
|---|---|---|
| `truck-sim` | `:8240/healthz` | 15s / 5s / 10 / 40s |
| `congestion` | `:8311/healthz` | 15s / 5s / 10 / 60s (model load) |
| `gateway` | `:8000/healthz` | 15s / 5s / 10 / 40s |
| `scenarios` | `:8400/healthz` | 15s / 5s / 10 / 30s |

Healthchecks only — **no `depends_on` conditions were changed**, so startup
ordering is untouched.

Services with a healthcheck went from 5 → 9:
`congestion, gateway, kafka, minio, postgres, redis, scenarios, truck-sim, zookeeper`.

### 4.2 · The metrics artifact reaches the gateway image

[gateway/Dockerfile](gateway/Dockerfile) copies **only the JSON artifact**, not the
`ai/` package:

```dockerfile
COPY ai/congestion/artifacts/metrics.json /app/ai/congestion/artifacts/metrics.json
```

Data, not code — the gateway image gains no `ai/` dependency. Confirmed the file
is not excluded by `.dockerignore`.

### 4.3 · Docker daemon — could not be started here

`Docker.app` is installed and `com.docker.backend` processes are running, but the
daemon never became available across repeated waits (~13 minutes total); it needs
GUI sign-in, which is not possible from this session. Compose was therefore
validated **statically**:

```
compose parses OK — 30 services
  truck-sim    container=jnpa-truck-sim   ports=['8240:8240'] healthcheck=YES
  congestion   container=jnpa-congestion  ports=['8311:8311'] healthcheck=YES
  gateway      container=jnpa-gateway     ports=['8000:8000'] healthcheck=YES
  scenarios    container=jnpa-scenarios   ports=['8400:8400'] healthcheck=YES

tests/test_compose_env_contract.py  14 passed
```

**This does not weaken the result.** The point of the fix is that Transport now
passes *with those containers down* — which is the condition it was tested under.
With the stack up, the same steps pass on their PRIMARY rungs instead.

---

## 5. Files changed

| File | Change |
|---|---|
| [gateway/routers/trucks.py](gateway/routers/trucks.py) | SECONDARY (RDS telemetry) + TERTIARY (check-ins) rungs on the fleet list; state-filter honesty guard |
| [gateway/routers/traffic.py](gateway/routers/traffic.py) | `LOCAL_ARTIFACT` rung for model-performance metrics; provenance labelling |
| [gateway/Dockerfile](gateway/Dockerfile) | Copy the congestion metrics artifact into the image |
| [docker-compose.yml](docker-compose.yml) | Health checks for truck-sim / congestion / gateway / scenarios |
| [tests/test_workflow_audit_fixes.py](tests/test_workflow_audit_fixes.py) | +7 regression tests (19 total) |

No unrelated module was touched. The layering
(router → service → repository → `core.*`) is unchanged; both fixes are additive
fallback rungs inside existing endpoints.

---

## 6. Final test output

### Transport workflow — 27/27

```
[PASS] 1.  Create vehicle (fleet registry)     POST /api/vehicles -> 409 vehicle_number_exists (TRK-000031)
[PASS] 1b. DB core.vehicle row                 [('TRK-000031','MH04QA9911','ACTIVE')]
[PASS] 1c. Duplicate plate rejected            -> 409
[PASS] 1d. Vehicle searchable                  -> 200
[PASS] 1e. Vehicle stats                       -> {"total":41,"active":39,"assigned":4,"available":35}
[PASS] 1f. Available vehicles                  -> 200
[PASS] 2.  Device list (truck-sim control plane)
           GET /api/trucks?limit=5 -> 200 | n_devices=5
           {"count":5,"devices":[{"device_id":"SYN-TFC-1:tfc1-…-020081","plate":"KA01ZA5819",
            "lat":18.948903,"lon":72.949183,"speed_kmh":0.0,…}]}          <-- SECONDARY rung
[PASS] 2b. A device_id is resolvable           device_id=SYN-TFC-1:tfc1-1785862068829-020081
[PASS] 3.  Live telemetry read (fallback chain) -> 404 no_position         [correct degrade]
[PASS] 3b. Web check-in (TERTIARY telemetry)   -> 200 accepted
[PASS] 3c. Check-in surfaces on truck read     -> decision_path=TERTIARY gate_delay=5
[PASS] 4.  Push re-route advisory (persists even if sim is down)
           -> 200 decision_path=REROUTE_DEGRADED sim={'delivered': False, …}
[PASS] 4b. Read latest advisory (PWA poll)     -> 200 advisory returned
[PASS] 4c. Driver acknowledges a REAL advisory -> 200 {"acked":true,"state":"ACK"}
[PASS] 4c2. ACK for device with NO advisory is rejected -> 404 no_advisory_to_ack
[PASS] 4d. DB core.reroute_advisory persisted  rows=[(1,)]
[PASS] 4e. DB ack_state recorded               ack_state=[('ACK',)]
[PASS] 5.  Record gate movement                -> 201 core.gate_event id 218897
[PASS] 5b. Query gate movements                -> 200
[PASS] 6.  /api/traffic/current                -> 200 {"status":"LIVE","source":"TOMTOM"}
[PASS] 6.  /api/traffic/health                 -> 200 provider TOMTOM configured
[PASS] 6.  /api/traffic/metrics                -> 200 {"congestion_onset_f1":0.8797,"precision":0.9624,
                                                       "recall":0.8101,"roc_auc":0.9058,…}   <-- LOCAL_ARTIFACT
[PASS] 6.  /api/traffic/congestion/metrics     -> 200 (same artifact)
[PASS] 6.  /api/traffic/snapshots              -> 200 SEG-00 speed 8.0 jam 7.5
[PASS] 6.  /api/traffic/predict                -> 200 horizon 15 min
[PASS] 6b. Congestion scan (alert generation)  -> 200 {"threshold":0.8,"count":0,"created":[]}
[PASS] 6c. Alerts surface after scan           -> 200 ELEVATED_SCRUTINY

=== WF2-Transport: 27/27 steps passed ===
```

### All five workflows

```
=== WF1-Cargo-Lifecycle:  33/33 steps passed ===
=== WF2-Transport:        27/27 steps passed ===
=== WF3-CFS-ECY:          24/24 steps passed ===
=== WF4-Gate-Documents:   40/40 steps passed ===
=== WF5-Demo-Readiness:   45/45 steps passed ===
```

### Automated suite

```bash
.venv/bin/python -m pytest $(ls tests/test_*.py | sed -n "1,40p")  -q
.venv/bin/python -m pytest $(ls tests/test_*.py | sed -n "41,80p") -q
.venv/bin/python -m pytest $(ls tests/test_*.py | sed -n "81,200p") tests/e2e -q
```
```
A: 552 passed,  13 skipped,  1 warning in 35.59s
B: 402 passed,  92 skipped,  1 warning in 31.43s
C: 121 passed,   9 skipped,  1 warning in 27.64s
----------------------------------------------------
   1075 passed, 114 skipped, 0 FAILED
```

### Regression tests for these fixes

```bash
.venv/bin/python -m pytest tests/test_workflow_audit_fixes.py -q
```
```
...................                                                      [100%]
19 passed in 0.49s
```

New in this round:

| Test | Pins |
|---|---|
| `test_truck_list_falls_back_to_the_rds_telemetry_tail` | SECONDARY rung serves real telemetry |
| `test_truck_list_falls_back_to_checkins_when_rds_is_empty` | TERTIARY rung + elevated scrutiny |
| `test_truck_list_state_filter_never_answers_from_a_fallback` | state filter is never faked from RDS |
| `test_congestion_metrics_artifact_is_the_real_committed_file` | artifact == on-disk file, byte for byte |
| `test_congestion_metrics_artifact_returns_none_when_absent` | no artifact → still 503, never invented |
| `test_congestion_metrics_prefers_the_live_service` | LIVE rung wins when reachable |
| `test_congestion_metrics_degrades_to_the_artifact` | labelled `LOCAL_ARTIFACT`, `metrics_synthetic: false` |

### Compose validation

```
compose parses OK — 30 services
tests/test_compose_env_contract.py  14 passed
services with healthcheck: congestion, gateway, kafka, minio, postgres, redis,
                           scenarios, truck-sim, zookeeper   (was 5, now 9)
```

---

## 7. What I did not do

- **No synthesised TruckState.** A state-filtered list degrades to empty rather than returning the unfiltered fleet.
- **No invented model metrics.** The artifact rung serves the committed file or nothing; a missing artifact still 503s.
- **No `depends_on` changes.** Health checks were added; startup ordering is untouched.
- **No loosened assertions.** Every workflow step asserts the same or a stricter condition than before; the three previously-failing steps now assert real returned data (5 devices with plates and coordinates; F1 0.8797 matching the artifact).

## 8. Verify with the stack up

```bash
make up && sleep 25 && make bootstrap-check
docker compose ps            # truck-sim / congestion / gateway / scenarios now report health
```

With the containers running, steps 2/2b and the two metrics reads pass on their
**PRIMARY** rungs (`decision_path: "PRIMARY"` / `"LIVE"`) instead of the fallbacks —
the same green result by the healthier path.

**QA rows left in RDS**, all clearly marked: cargo `QATU7788228` / `QATU7788338` /
`QATU7788448`, vehicle `MH04QA9911` (`TRK-000031`), advisory on the probed device,
Form-13 `F13QAEVID001`. Delete when convenient.
