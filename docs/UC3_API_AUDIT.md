# JNPA UC-III Platform — Complete API Audit

**Date:** 2026-07-10  **Gateway:** `jnpa/gateway:0.1.0` (single public FastAPI, port 8000)
**Method:** static code scan of every router + live runtime validation (docker, DB, health, and a full smoke sweep of every mounted endpoint). Route existence was **not** trusted — each path was exercised against the running stack and its code path traced.

**Headline:** the platform is broad and mostly working in the demo profile, but there are **two production-blocking code defects** and several runtime/config gaps. Overall production-readiness: **6.0 / 10** (see §F).

---

## Executive summary of defects

| # | Severity | Issue | Effect |
|---|---|---|---|
| 1 | 🔴 BLOCKER | Gateway image ships a **stale `jnpa_shared`** (missing `iso6346.py` + `assumptions.py`). `journey.py` imports `jnpa_shared.iso6346` at module load. | `journey`, `workflows`, `meta` routers are **not mounted** (all 404). A **fresh `import gateway.main` crashes** → the gateway will **not survive a restart / redeploy**. It only serves now because the running process is a 5-hour-old in-memory copy. |
| 2 | 🔴 BLOCKER | `gateway/audit_client.py:110` reads `request.content` on a **streaming** request without `read()` → `httpx.RequestNotRead`. | Every gateway→service **multipart image forward 500s**: `/api/anpr/infer`, `/api/violations/detect`, `/api/violations/enforce`. The entire image-based enforcement flow is broken. |
| 3 | 🟠 HIGH | `FASTAG_DEMO_MODE=true` is in `.env.local` but **absent from the running gateway container env**; no ULIP URL set. | `/api/fastag/balance|transactions|toll-enroute` all **500 (config)**. FASTag write-path unusable until env applied. `toll-enroute` has **no demo path at all**. |
| 4 | 🟠 HIGH | **`jnpa-minio` container is `Exited (0)`.** | Evidence storage/serving (`/api/evidence`), identity enrolment photo persistence (`/api/identity/enrollments/{id}/approve`), and report-embedded images are degraded/broken. |
| 5 | 🟡 MED | `jnpa-truck-sim` container **unhealthy** (healthz 000). | `/api/trucks/{id}/route` reroute → **502**; `/api/trucks` list still degrades to 200. |
| 6 | 🟡 MED | ANPR AI service is in **degraded fallback OCR** (`degraded:true`, engine `fallback`, held-out `exact_match = 0.0`). | Plate recognition returns synthetic/low-accuracy reads; real Paddle+YOLO weights not loaded. |
| 7 | 🟡 MED | `AUTH_ENABLED=false` (dev). Every endpoint is open. | Acceptable for demo; **must** be enabled + `AUTH_JWT_SECRET` rotated for production. |

**Fix ordering matters:** issue #3 tempts an operator to `restart gateway`, but because of issue #1 the gateway **will not come back up**. Fix `jnpa_shared` first (§G).

---

## Runtime verification (observed)

| Component | State | Notes |
|---|---|---|
| Gateway (8000) | ✅ healthy | mode `development`, `surepass_enabled:false`, serving **125** paths (stale — missing journey/workflows/meta) |
| Postgres (5433→5432) | ✅ healthy | role `postgres` db `postgres`, schema `jnpa`, **39 tables** seeded |
| Redis (6379) | ✅ healthy | `PONG`; cache + ANPR frame-bus |
| Kafka (9092) | ✅ healthy | 11 topics: anpr.reads, alerts, traffic.predictions, truck.telemetry, rfid.reads, weighbridge.reads, carbon.records, empty.container.moves, parking.state, face.verifications, truck.eta |
| **MinIO** | 🔴 **Exited (0)** | evidence/photo object store down |
| ANPR AI (8301) | ⚠️ healthy but **degraded** | fallback OCR, exact_match 0.0 |
| Congestion AI (8311) | ✅ healthy | model_loaded, 13 segments |
| Identity (8360) | ✅ healthy | 50 enrolled, provider onnx, liveness off |
| Empty-container (8330) | ✅ healthy | 6 depots, 40 demand |
| Carbon (8340) | ✅ healthy | calc mode |
| Gate-data (8350) | ✅ healthy | sim mode, 202 containers |
| Parking (8370) | ✅ healthy | 6 facilities |
| Scenarios-runner (8400) | ✅ healthy | tfc1/tfc2/tfc3 |
| Vahan sim/live (8201/8202) | ✅ healthy | "live" is a Surepass-token-gated sim wrapper |
| **truck-sim (8240)** | 🔴 **unhealthy** | reroute POST 502 |

**Seed data row counts (key tables):** drivers 6, vehicle_master 4, anpr_reads 3 263, alerts 947, digital_twin_events 2 406, gate_captures 808, empty_container_inventory 744, truck_telemetry 7.4 M, traffic_snapshots 26, parking_facilities 6, geofence_zones 6, fastag_balance 1, fastag_transactions 8, driver_enrollments 5. **Empty:** `driver_faces = 0`, `verification_logs = 0` (identity verify always hits admit-on-trust PROVISIONAL because no face templates are persisted to `jnpa.driver_faces`).

---

## Production-critical API groups — status at a glance

| Group | Prefix | Mounted? | Runtime | Notes |
|---|---|---|---|---|
| **Auth** | `/api/auth`, `/api/auth/otp` | ✅ | ✅ PASS | seeded PoC users (pwd=username); dev-token 404s in prod; OTP SMS is a log-stub |
| **ANPR** | `/api/anpr` | ✅ | ⚠️ | cameras/read/eval OK; **`/infer` 500** (#2); model degraded (#6) |
| **Vahan** | `/api/vahan` | ✅ | ✅ | RC 4-rung chain works (LIVE_FALLBACK/PROVISIONAL); DL 404 on unseeded number |
| **Identity** | `/api/identity` | ✅ | ✅* | verify/enrol/gallery OK; approve needs MinIO (#4); no persisted templates |
| **Gate Data** | `/api/gate-data` | ✅ | ✅ | proxy 8350 + in-proc LEO fallback |
| **Journey Twin** | `/api/journey` | 🔴 **NO** | 🔴 404 | **unmounted (#1)** — Follow-the-Box broken |
| **Truck Tracking** | `/api/trucks` | ✅ | ⚠️ | list OK; **reroute 502** (#5); TERTIARY check-in in-memory |
| **Traffic/Congestion** | `/api/traffic` | ✅ | ✅ | predict LIVE/CACHED/SYNTHETIC; metrics real |
| **Parking** | `/api/parking` | ✅ | ✅ | RDS-direct fallback, **no synthetic** occupancy |
| **Empty Container** | `/api/empty` | ✅ | ✅ | proxy 8330 + in-proc seed fallback |
| **FASTag** | `/api/fastag` | ✅ | 🔴 | health/history OK; **balance/transactions/toll-enroute 500** (#3) |
| **Violations/Enforcement** | `/api/violations` | ✅ | 🔴 | catalog/case/commit OK; **detect/enforce 500** (#2) |
| **Reports** | `/api/reports` | ✅ | ✅ | **real Playwright-Chromium PDF** (verified 3-page A4), not persisted |
| **KPI** | `/api/kpi` | ✅ | ✅ | 6 whitelisted views; `/strip` mixes baselines |
| **Scenarios** | `/api/scenarios`, `scenario_ext` | ✅ | ✅ | runner 8400; e-Challan + TAS are stubs |
| **Workflow Engine** | `/api/workflows` | 🔴 **NO** | 🔴 404 | **unmounted (#1)** |
| **Carbon** | `/api/carbon` | ✅ | ✅ | proxy 8340 + in-proc calculator |
| **Alerts** | `/api/alerts` | ✅ | ✅ | anomaly proxy + `jnpa.alerts` fallback |
| **Geo/Geofence** | `/api/gates,corridor,zones,geo` | ✅ | ✅ | fully local; static corridor + RDS zones |
| **Meta** | `/api/assumptions`, `/api/oss-inventory` | 🔴 **NO** | 🔴 404 | **unmounted (#1)** |

---

## API INVENTORY

Auth column reflects RBAC **when `AUTH_ENABLED=true`** (currently off → all open). Config lists the non-obvious env beyond the always-present `POSTGRES_DSN`/`REDIS_URL`. "Test Payload" is a validated demo body.

### Auth & Session
| API | Method | Router | Purpose | Dependency | Required Config | Test Payload |
|---|---|---|---|---|---|---|
| `/api/auth/login` | POST | auth.py | Password→JWT | — | `AUTH_JWT_SECRET`, `AUTH_USERS` | `{"username":"admin","password":"admin"}` |
| `/api/auth/dev-token` | POST | auth.py | Role token (dev only) | — | `AUTH_DEV_TOKENS`,`APP_ENV` | `{"role":"DTCCC_ADMIN"}` |
| `/api/auth/device-token` | POST | auth.py | PWA DRIVER pairing | — | `PWA_PAIRING_SECRET` | `{"device_id":"DEV-1","pairing_secret":"s"}` |
| `/api/auth/roles` | GET | auth.py | List roles | — | — | — |
| `/api/auth/otp/request` | POST | otp.py | Issue OTP | Postgres, SMS stub | `SMS_PROVIDER` | `{"mobile":"9876543210","device_id":"DEV-1"}` |
| `/api/auth/otp/verify` | POST | otp.py | Verify→DRIVER JWT | Postgres | `AUTH_JWT_SECRET` | `{"mobile":"9876543210","otp":"123456","device_id":"DEV-1"}` |
| `/api/auth/otp/refresh` | POST | otp.py | Re-issue token | Postgres | — | `{"device_id":"DEV-1"}` |
| `/api/auth/otp/logout` | POST | otp.py | Revoke device | Postgres | — | `{"device_id":"DEV-1"}` |
| `/api/auth/otp/session/{device_id}` | GET | otp.py | Binding status | Postgres | — | path only |

### ANPR
| API | Method | Router | Purpose | Dependency | Config | Test Payload |
|---|---|---|---|---|---|---|
| `/api/anpr/cameras` | GET | anpr.py | Per-camera LIVE/CACHED/SYNTHETIC | Redis frame-bus | `GATEWAY_ANPR_LAG_S` | — |
| `/api/anpr/eval` | GET | anpr.py | Held-out OCR benchmark | ANPR 8301 | `GATEWAY_ANPR_URL` | — (slow ~30s) |
| `/api/anpr/infer` | POST(multipart) | anpr.py | Plate OCR on frame | ANPR 8301 | `GATEWAY_ANPR_URL` | `image=@frame.jpg` **← 500 (#2)** |
| `/api/anpr/read/{camera_id}` | GET | anpr.py | Current read for camera | Postgres, Redis | — | path `CAM-COR-01` |

### Vahan (all GET)
| API | Method | Router | Purpose | Dependency | Config | Test |
|---|---|---|---|---|---|---|
| `/api/vahan/rc/{plate}` | GET | vahan.py | RC 4-rung lookup | vahan-live/sim, Redis, PG | `SUREPASS_API_TOKEN`, `GATEWAY_VAHAN_*_URL` | `MH04AB1234` |
| `/api/vahan/dl/{dl}` | GET | vahan.py | Sarathi DL lookup | vahan-live/sim, Redis | same | seeded DL |
| `/api/vahan/fastag/{plate}` | GET | vahan.py | FastTag balance | vahan-live/sim, Redis | same | `MH04AB1234` |
| `/api/vahan/vehicle-intel/{plate}` | GET | vahan.py | Aggregate vehicle intel | Postgres | — | `MH04AB1234` |
| `/api/vahan/driver-intel/{driver_key}` | GET | vahan.py | Aggregate driver intel | Postgres | — | `DRV-001` |
| `/api/vahan/verification-history` | GET | vahan.py | Recent RC lookups | Postgres | — | `?limit=100` |
| `/api/vahan/dl-history` | GET | vahan.py | Recent DL lookups | Postgres | — | `?limit=100` |

### Identity
| API | Method | Router | Purpose | Dependency | Config | Test Payload |
|---|---|---|---|---|---|---|
| `/api/identity/verify` | POST | identity.py | 1:1 face verify | identity 8360, PG | `ALLOW_REAL_BIOMETRICS` | `{"driver_id":"DRV-001","is_synthetic":true,"purpose":"GATE_VERIFICATION","simulate":"genuine"}` |
| `/api/identity/identify` | POST | identity.py | 1:N identify | identity 8360, PG | — | `{"image":"<b64>","is_synthetic":true}` |
| `/api/identity/enrol` | POST | identity.py | Capture template | identity 8360 | — | `{"driver_id":"DRV-001","is_synthetic":true}` |
| `/api/identity/enrol-request` | POST | identity.py | Driver self-enrol → PENDING | Postgres | — | `{"driver_id":"DRV-9","consent":true,"images":["<b64>"]}` |
| `/api/identity/enrollments[/{id}]` | GET | identity.py | Enrolment queue/record | Postgres | — | `?status=PENDING` |
| `/api/identity/enrollments/{id}/approve` | POST | identity.py | Mint template + store photo | identity, **MinIO** | `MINIO_*` | `{}` **← MinIO down (#4)** |
| `/api/identity/enrollments/{id}/reject\|reenroll` | POST | identity.py | Enrolment decision | Postgres | — | `{"reason":"blurry"}` |
| `/api/identity/drivers` | GET | identity.py | Active drivers | Postgres | — | — |
| `/api/identity/gallery`,`/verifications`,`/threshold` | GET | identity.py | Gallery/audit/config | identity/PG | — | — |

### Gate Data
| API | Method | Router | Purpose | Dependency | Config | Test |
|---|---|---|---|---|---|---|
| `/api/gate-data/leo/queue` | GET | gate_data.py | Reconcile all (Auto-LEO) | gate-data 8350 | `GATEWAY_GATE_DATA_URL` | — |
| `/api/gate-data/leo` | POST | gate_data.py | Reconcile one container | gate-data 8350 | — | `{"container_no":"MSKU1234567"}` |
| `/api/gate-data/customs/flags\|history` | GET | gate_data.py | Customs feed | gate-data 8350 | — | — |
| `/api/gate-data/captures\|reconciliations\|providers` | GET | gate_data.py | RDS-backed views | gate-data 8350 | — | — |
| `/api/gate-data/records/{container_no}` | GET | gate_data.py | Raw records | gate-data 8350 | — | ISO-6346 |

### Journey Twin — 🔴 UNMOUNTED (#1)
| API | Method | Router | Purpose | Dependency | Config | Test |
|---|---|---|---|---|---|---|
| `/api/journey/container/{container_no}` | GET | journey.py | Follow-the-Box timeline | gate-data 8350, `jnpa_shared.iso6346` | `DATA_MODE` | `MSCU1234566` → **404** |

### Truck Tracking
| API | Method | Router | Purpose | Dependency | Config | Test Payload |
|---|---|---|---|---|---|---|
| `/api/trucks` | GET | trucks.py | Live fleet (sampled) | truck-sim 8240 | `GATEWAY_TRUCK_URL` | `?limit=200` |
| `/api/trucks/{id}` | GET | trucks.py | PRIMARY/SECONDARY/TERTIARY pos | truck-sim, ULIP, PG | — | `TRK-000001` |
| `/api/trucks/{id}/route` | POST | trucks.py | Push re-route | **truck-sim 8240** | VAPID, `SMS_PROVIDER` | `{"gate_id":"G-JNPCT","reason":"x"}` **← 502 (#5)** |
| `/api/trucks/{id}/route/latest` | GET | trucks.py | Poll advisory | in-memory | — | — |
| `/api/trucks/{id}/route/ack` | POST | trucks.py | Ack advisory | in-memory | — | `{"state":"ACK"}` |

### Traffic
| API | Method | Router | Purpose | Dependency | Config | Test |
|---|---|---|---|---|---|---|
| `/api/traffic/predict` | GET | traffic.py | Segment congestion forecast | congestion 8311, Redis | `GATEWAY_CONGESTION_URL` | `?horizon_min=15` |
| `/api/traffic/metrics` + `/congestion/metrics` | GET | traffic.py | Model metrics | congestion 8311 | — | — |
| `/api/traffic/snapshots` | GET | traffic.py | Latest per-segment | Postgres | — | — |

### Parking
| API | Method | Router | Purpose | Dependency | Config | Test Payload |
|---|---|---|---|---|---|---|
| `/api/parking/availability\|summary\|facilities` | GET | parking.py | Occupancy/board | parking 8370 + RDS | `GATEWAY_PARKING_URL` | — |
| `/api/parking/allocate` | POST | parking.py | Allocate slot | parking 8370 | — | `{"facility_id":"PF-01","vehicle_id":"MH04AB1234"}` |
| `/api/parking/release` | POST | parking.py | Release slot | parking 8370 | — | `{"vehicle_id":"MH04AB1234"}` |
| `/api/parking/violation` | POST | parking.py | Record event | parking 8370 | — | `{"vehicle_id":"MH04AB1234","type":"NO_PARKING"}` |
| `/api/parking/history\|violations` | GET | parking.py | Transaction/violation log | parking 8370 | — | `?limit=100` |

### Empty Container
| API | Method | Router | Purpose | Dependency | Config | Test Payload |
|---|---|---|---|---|---|---|
| `/api/empty/allocations\|supply\|demand\|kpi` | GET | empty_container.py | Book + KPI | empty-container 8330 (+in-proc seed) | `GATEWAY_EMPTY_CONTAINER_URL` | — |
| `/api/empty/containers/available` | GET | empty_container.py | RDS inventory | empty-container 8330 | — | `?limit=200` |
| `/api/empty/containers/allocate` | POST | empty_container.py | Allocate (persist) | empty-container 8330 | — | `{"container_type":"20GP","demand_id":"D-102","depot_id":"ECD-1"}` |
| `/api/empty/containers/allocation/history` | GET | empty_container.py | History | empty-container 8330 | — | — |

### FASTag — 🔴 write-path 500 (#3)
| API | Method | Router | Purpose | Dependency | Config | Test Payload |
|---|---|---|---|---|---|---|
| `/api/fastag/balance` | POST | fastag.py | RC→balance (UPSERT) | ULIP/demo, PG | `FASTAG_DEMO_MODE`/`FASTAG_ULIP_URL` | `{"rc_number":"MH04AB1234"}` **← 500** |
| `/api/fastag/transactions` | POST | fastag.py | RC→txns (dedup persist) | ULIP/demo, PG | same | `{"rc_number":"MH04AB1234"}` **← 500** |
| `/api/fastag/toll-enroute` | POST | fastag.py | Toll plazas enroute | ULIP only (**no demo**) | `FASTAG_ULIP_URL` | `{"source_state":"Maharashtra","source_name":"Nhava Sheva","destination_state":"Maharashtra","destination_name":"Pune","vehicle_type":"TRUCK"}` **← 500** |
| `/api/fastag/transactions/history` | GET | fastag.py | RDS history | Postgres | — | `?rc_number=MH04AB1234` ✅ |
| `/api/fastag/health` | GET | fastag.py | Vendor+DB probe | Postgres | — | ✅ (`degraded`, ulip_configured:false) |

### Violations / Enforcement — 🔴 detect/enforce 500 (#2)
| API | Method | Router | Purpose | Dependency | Config | Test Payload |
|---|---|---|---|---|---|---|
| `/api/violations/catalog` | GET | violations.py | Kinds + fines | — | — | ✅ |
| `/api/violations/detect` | POST(multipart) | violations.py | ANPR+enrich+evidence | ANPR 8301, MinIO | `GATEWAY_ANPR_URL`,`MINIO_*` | `image=@f.jpg` **← 500** |
| `/api/violations/commit` | POST | violations.py | Persist case→challan | Postgres | — | `{"case_id":"<uuid>","plate":"MH04AB1234","violations":["OVERSPEEDING"],"issue_challan":true}` |
| `/api/violations/enforce` | POST(multipart) | violations.py | One-shot auto-enforce | ANPR, MinIO, WS | same | `image=@f.jpg` **← 500** |
| `/api/violations/cases/{id}` | GET | violations.py | Case bundle | Postgres | — | path |
| `/api/violations/cases/{id}/transition` | POST | violations.py | Lifecycle hop | Postgres | — | `{"to_status":"PAID","payment_ref":"R1"}` |

### Reports / KPI / Alerts / AI-events / Geo / Scenarios / Workflows / Carbon / Push / Control / Debug / Meta / Evidence
| API | Method | Router | Purpose | Dependency | Config | Test |
|---|---|---|---|---|---|---|
| `/api/reports/police` | GET | reports.py | Incident JSON/PDF/HTML | Postgres, **Playwright** | — | `?format=pdf` ✅ real PDF |
| `/api/kpi[/{view}]`,`/strip`,`/sources`,`/cameras` | GET | kpi.py | KPI views | Postgres | — | view∈throughput,dwell,… |
| `/api/alerts[/]` | GET | alerts.py | Recent alerts | anomaly, PG | `GATEWAY_ANOMALY_URL` | `?since=PT1H` |
| `/api/alerts/{id}/ack` | POST | alerts.py | Acknowledge | Postgres | — | — |
| `/api/ai/event` | POST | ai_events.py | Ingest AI event | Postgres | — | `{"event_type":"ILLEGAL_PARKING","vehicle_id":"MH04AB1234","severity":"warning"}` |
| `/api/ai/events` | GET | ai_events.py | Recent events | Postgres | — | `?limit=100` |
| `/api/gates`,`/api/corridor`,`/api/zones` (GET/PUT) | GET/PUT | geo.py | Gates/corridor/geofence | Postgres | — | zones PUT `{"zones":[…]}` |
| `/api/geo/events`(GET/POST),`/violations`,`/evaluate`,`/vehicles-in-zones`,`/zones-active` | GET/POST | geo.py | Geofence engine | Postgres | — | evaluate `{"vehicle_id":"MH04AB1234","lat":18.9489,"lon":72.9492}` |
| `/api/scenarios[/]`,`/{name}/run`,`/{name}/reset`,`/handles`,`/handle/{id}/timeline` | GET/POST | scenarios.py | What-If runner | scenarios 8400 | `GATEWAY_SCENARIOS_URL` | run `{"severity":"high"}` |
| `/api/routing/best_alt_gate` | POST | scenario_ext.py | Alt-gate pick | truck-sim, congestion | — | `{"exclude":["G-NSICT"],"eta_min":15}` |
| `/api/echallan/issue` | POST | scenario_ext.py | **STUB** e-Challan | self /vahan | — | `{"plate":"MH04AB1234","kind":"WRONG_WAY"}` |
| `/api/tas/slots\|reschedule\|restore` | GET/POST | scenario_ext.py | **STUB** in-mem TAS | in-memory | — | reschedule `{"gate_id":"G-JNPCT"}` |
| `/api/scenario_step` | POST | scenario_ext.py | WS fan-out | WebSocket | — | `{"step":"demo"}` |
| `/api/workflows/*` | GET/POST/PUT/DELETE | workflows.py | No-code rules engine | Postgres | — | **404 unmounted (#1)** |
| `/api/carbon/rollup` | GET | carbon.py | CO2e rollup | carbon 8340 | `GATEWAY_CARBON_URL` | — |
| `/api/carbon/estimate` | POST | carbon.py | Per-trip emissions | carbon 8340 | — | `{"vehicle_class":"HGV","distance_km":42.5,"idle_minutes":12}` |
| `/api/push/*` | GET/POST | push.py | WebPush subs | VAPID/pywebpush | `VAPID_*` | subscribe `{"device_id":"TRK-1","subscription":{…}}` |
| `/api/ulip/proxy/{device_id}` | GET | ulip.py | GPS relay (mock w/o key) | ULIP relay | `GATEWAY_ULIP_URL`,`ULIP_API_KEY` | `TRK-000001` |
| `/api/control/fault[/{domain}]` | GET/POST/DELETE | control.py | Presenter fault inject | in-memory, WS | — | `{"rung":"PROVISIONAL"}` |
| `/api/debug/decisions[/summary]` | GET | debug.py | Decision ring buffer | in-memory | `GATEWAY_DECISION_RING_SIZE` | — |
| `/api/assumptions`,`/api/oss-inventory` | GET | meta.py | Platform metadata | `jnpa_shared.assumptions` | — | **404 unmounted (#1)** |
| `/api/evidence/{object_path}` | GET | evidence.py | **PUBLIC** MinIO proxy | **MinIO** | `MINIO_*` | needs MinIO up (#4) |
| `/checkin` | GET/POST | checkin.py | Manual truck check-in form | in-memory | — | form `device_id&plate&lat&lon` |
| `/api/ws` | WS | ws.py | Event fan-out | in-memory | `AUTH_JWT_SECRET` | `?token=` |

---

## Per-module functional verification (behaviour, not just HTTP)

- **ANPR** — camera list ✅ (all SYNTHETIC: no live frames on Redis stream). Inference path ❌ (`/infer` 500, audit_client bug #2). Real-vs-synthetic: service `degraded:true`, engine `fallback`, held-out `exact_match 0.0` → effectively synthetic/low-accuracy OCR (#6).
- **Vahan** — RC lookup ✅ (`MH04AB1234`→LIVE_FALLBACK with record; unseeded→PROVISIONAL admit + alert persisted to `jnpa.alerts`). DL lookup ⚠️ 404 for unseeded numbers (correct — no provisional for licences). Vehicle/driver-intel ✅ RDS aggregates.
- **Identity** — enrolment ✅ (`enrol-request` PENDING queue). Verification ✅ but always PROVISIONAL `driver_not_enrolled` because `jnpa.driver_faces` = 0 (no persisted templates). Approve path needs MinIO (down, #4).
- **Journey** — container twin chain / events / timestamps ❌ **entire module 404 (#1)**. Code is sound (hash-derived stages, ISO-6346-gated) but never mounted.
- **Violation** — detect ❌ 500 (#2); commit ✅ (opens case, walks DETECTED→REVIEWED→CONFIRMED, mints immutable `jnpa.challans` row w/ `ECH-YYYY-NNNNNN`); case lifecycle ✅ (validated transitions, 409 on illegal hop); challan generation ✅ but is a **self-issued Postgres record — no real e-Challan/VAHAN backend**; `_auto_classify` is a hash stand-in, not an AI model.
- **Reports** — PDF generation ✅ **verified real** (Playwright Chromium, 3-page A4, `application/pdf`, no fallback header). Not persisted; streamed inline.
- **FASTag** — demo-mode ❌ (env not applied → 500) / real ULIP ❌ (not configured). DB persistence ✅ ready (`jnpa.fastag_*` tables exist, history read works). `toll-enroute` has **no demo path**.

---

## A. Working APIs (verified 200 + correct code path)
Auth (login/dev-token/otp), Vahan (rc/fastag/intel/history), ANPR (cameras/read/eval), Identity (verify/enrol/enrol-request/gallery/drivers/threshold/verifications), Gate-data (all 8), Traffic (predict/metrics/snapshots), Parking (all 8), Empty-container (all 7), Carbon (rollup/estimate), Geo (all 11 gates/corridor/zones/geo), KPI (all 6), Alerts (list/ack), AI-events (event/events), Scenarios (list/run/reset/handles), scenario_ext (routing/echallan-stub/tas-stub/step), Reports (police JSON+PDF), Violations (catalog/commit/case/transition), Push, ULIP, Control, Debug, Checkin, FASTag (health/history). **≈106 endpoint checks PASS.**

## B. Failing APIs
| Endpoint(s) | Code | Root cause |
|---|---|---|
| `/api/journey/*`, `/api/workflows/*`, `/api/assumptions`, `/api/oss-inventory` | 404 | Routers unmounted — stale `jnpa_shared` (#1) |
| `/api/anpr/infer`, `/api/violations/detect`, `/api/violations/enforce` | 500 | `audit_client.py` RequestNotRead on multipart forward (#2) |
| `/api/fastag/balance\|transactions\|toll-enroute` | 500 | `FASTAG_DEMO_MODE` not in container env; no ULIP URL (#3) |
| `/api/trucks/{id}/route` | 502 | truck-sim container unhealthy (#5) |
| `/api/evidence/{path}`, identity approve | degraded | MinIO exited (#4) |

*(Smoke-test 404s on `/api/vahan/dl/<num>`, `/api/kpi/gate_throughput`, `/api/identity/enrollments/DRV-001`, `/api/scenarios/tfc1/timeline` are **demo-ID/param mismatches, not code bugs** — those routes work with valid IDs.)*

## C. Missing configuration
- `FASTAG_DEMO_MODE=true` present in `.env.local` but **not propagated to the running `jnpa-gateway` container** (added after container start).
- `AUTH_ENABLED` unset (=false) → no authentication; `AUTH_JWT_SECRET` at insecure default.
- `SUREPASS_API_TOKEN`, `ULIP_API_KEY`, `GATEWAY_ULIP_URL` empty → Vahan "live" and FASTag/ULIP run in sim/mock/config-error.
- `VAPID_PRIVATE_KEY` unset → WebPush silently no-ops.
- `MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY` missing at compose-interpolation time (`docker compose ps` errored on `anomaly-train`).

## D. Missing database data
- `jnpa.driver_faces = 0` and `jnpa.verification_logs = 0` → identity verify can never return VERIFIED (always PROVISIONAL admit-on-trust). Seed face templates for a real 1:1/1:N demo.
- `jnpa.vehicle_master = 4`, `fastag_balance = 1` → thin; most plates fall to PROVISIONAL/miss.
- `container_movement_history = 3` → Follow-the-Box has little real gate data even once mounted.

## E. Runtime / service issues
- 🔴 **MinIO `Exited (0)`** — object store down.
- 🔴 **truck-sim `unhealthy`** — reroute 502.
- ⚠️ **ANPR `degraded`** — fallback OCR, 0.0 exact-match; real weights not loaded.
- ⚠️ Gateway serving **stale in-memory code**; a restart crashes it until #1 is fixed.

## F. Production readiness score — **6.0 / 10**
| Dimension | Score | Rationale |
|---|---|---|
| API breadth/design | 9 | 32 routers, clean fallback-chain architecture, decision_path transparency |
| Runtime stability | 4 | gateway won't survive restart (#1); MinIO + truck-sim down |
| Core enforcement flow | 5 | commit/case/challan/PDF solid; detect/enforce 500 (#2) |
| Data authenticity | 5 | heavy reliance on synthetic/derived; ANPR degraded; e-Challan self-issued |
| Config/secrets hygiene | 5 | auth off, default JWT secret, keys empty |
| Data completeness | 6 | good telemetry volume; identity/vehicle/container seed thin |
| **Overall** | **6.0** | Strong PoC; **not** deployable to prod until #1–#4 fixed |

## G. Exact commands to fix
**Fix order is mandatory — #1 before any gateway restart, or the gateway will not come back.**

```bash
cd "/Users/pandurangdhage/Downloads/ jnpa uc3 poc/jnpa-uc3-poc"

# ---- #1 stale jnpa_shared (BLOCKER): rebuild gateway image so shared/ (incl.
#         iso6346.py + assumptions.py) is reinstalled, then recreate.
docker compose build gateway
docker compose up -d --force-recreate gateway
#   Hot-patch alternative (no rebuild) — copy the two missing modules in:
docker cp shared/jnpa_shared/iso6346.py    jnpa-gateway:/usr/local/lib/python3.11/site-packages/jnpa_shared/
docker cp shared/jnpa_shared/assumptions.py jnpa-gateway:/usr/local/lib/python3.11/site-packages/jnpa_shared/
docker compose restart gateway
#   Verify journey/workflows/meta now mount:
curl -s localhost:8000/openapi.json | python3 -c 'import sys,json;p=json.load(sys.stdin)["paths"];print("journey" ,any("journey" in x for x in p),"workflows",any("workflows" in x for x in p),"assumptions","/api/assumptions" in p)'

# ---- #2 audit_client RequestNotRead (BLOCKER): guard the body read.
#   In gateway/audit_client.py:110 replace  req_body = _decode_body(request.content)
#   with a guarded read, e.g.:
#       try:  req_body = _decode_body(request.content)
#       except httpx.RequestNotRead:  req_body = None   # streaming/multipart upload
#   then rebuild/recreate gateway and re-test:
curl -s -o /dev/null -w '%{http_code}\n' -F image=@data/anpr_real/<any>.jpg localhost:8000/api/anpr/infer   # expect 200

# ---- #3 FASTag demo mode: ensure env reaches the container, then restart (AFTER #1).
grep -q '^FASTAG_DEMO_MODE=true' .env.local || echo 'FASTAG_DEMO_MODE=true' >> .env.local
docker compose up -d --force-recreate gateway
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/api/fastag/balance -H 'Content-Type: application/json' -d '{"rc_number":"MH04AB1234"}'  # expect 200
#   (toll-enroute still needs a real FASTAG_ULIP_URL — no demo path exists.)

# ---- #4 MinIO: bring the object store back up.
docker compose up -d minio
#   ensure MINIO_ACCESS_KEY / MINIO_SECRET_KEY are exported in .env.local (fixes the
#   `docker compose ps` interpolation error too), then restart evidence consumers.

# ---- #5 truck-sim unhealthy: recreate.
docker compose up -d --force-recreate truck-sim
curl -s -o /dev/null -w '%{http_code}\n' localhost:8240/healthz   # expect 200

# ---- #6 ANPR real weights (optional for demo): load Paddle+YOLO artifacts so
#         /api/anpr/eval reports degraded:false; otherwise document synthetic mode.

# ---- #7 production auth hardening (before any real deployment):
#   set AUTH_ENABLED=true, AUTH_JWT_SECRET=<32+ random>, AUTH_DEV_TOKENS=false, APP_ENV=production

# ---- Re-validate everything:
GW=http://localhost:8000 ./scripts/uc3_smoke_test.sh   # expect FAIL(hard) → 0
```
