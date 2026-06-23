# GAP_REPORT.md

**Project:** JNPA UC-3 Port Traffic Digital Twin
**Date:** 2026-06-23
**Auditor view:** Solution Architect + Principal Full-Stack + QA

---

## PHASE 1 — Full audit (inventory)

### 1. Driver PWA (`mobile-pwa/`)
- **Pages/screens:** `Pairing`, `Trip`, `Reroute`, `Inbox`, `Profile` (`src/screens/`). Routing via `App.tsx` (pairing-gated).
- **API calls:** 15 REST + 1 WS — all defined in `src/lib/api.ts`. Inventory & status in [API_CONTRACT_REPORT.md](API_CONTRACT_REPORT.md) §1.
- **Auth flow:** Device-pairing only (QR + 6-digit code → `device_id` in `localStorage`). **No bearer token, no login.**
- **WebSocket:** `/api/ws` in a Worker thread (`workers/realtime.worker.ts`), 25 s ping, exponential-backoff reconnect, 3 s fallback polling of `…/route/latest` while socket down.
- **Push:** Web Push (VAPID) — `vapid-public-key` → `subscribe` → `test`; service worker `sw.ts`.
- **Error handling:** central `http()` throws `${status} … — ${detail}`; screens degrade (404 → "Awaiting first GPS fix"); silent catch on best-effort calls.
- **State management:** React context (`RealtimeContext`) + IndexedDB advisory cache (24 h offline).

### 2. Dashboard (`web/`)
- **Pages:** `LiveOperations`, `DriverAdvisory`, `PoliceReports`, `SystemHealth`, `Geofencing`, `WhatIf`, `DemoConsole` (`src/screens/`).
- **Widgets/panels:** KPI strip, camera-health chips, alert panel + evidence dialog, traffic/congestion, source-health, AutoLEO/customs, identity, parking, fault-injection.
- **API calls:** 36 endpoints via `lib/api.ts` + `data/live.ts` (LiveAdapter). Inventory in [API_CONTRACT_REPORT.md](API_CONTRACT_REPORT.md) §2.
- **Data layer:** `DataAdapter` abstraction with `MockAdapter` (dev) / `LiveAdapter` (prod), selected by compile-time `__JNPA_DATA_MODE__`; ship-guard sentinel `JNPA_MOCK_ADAPTER_PRESENT_DO_NOT_SHIP`.
- **Auth/RBAC:** `VITE_AUTH_ENABLED` build flag; bearer token; role-filtered nav (`auth.ts`).

### 3. Backend (`gateway/` + `ai/` + `ingest/`)
- **Routers:** anpr, auth, vahan, alerts, traffic, trucks, kpi, scenarios, checkin, ulip, debug, control, push, gate_data, carbon, identity, parking, empty_container, reports, geo, scenario_ext. ~70 endpoints.
- **Integrations:** ai/anpr (real YOLO+OCR), ai/anomaly, ai/congestion; vahan_sim / vahan_live (Surepass); gate-data, identity, parking, carbon, empty-container services with in-process fallback.
- **DB:** TimescaleDB/Postgres (`infra/postgres/init.sql`): `vehicle_master, gates, cameras, anpr_reads, rfid_reads, truck_telemetry, alerts, traffic_snapshots, scenario_*, geofence_zones, services` + KPI views.
- **Bus:** Kafka (`anpr.reads, rfid.reads, truck.telemetry, traffic.snapshots, alerts, vehicle.confirmed`, …), MQTT (`rfid/readers/+`, `trucks/{id}/telemetry|eta`), Redis (frame bus + congestion cache).
- **WS events:** `hello, alert, traffic, truck_position, decision, scenario_step, operator_banner, reroute, reroute_ack`.
- **Auth/RBAC:** HS256 JWT, path-based `_POLICY`, public-path allowlist; startup guard forces `AUTH_ENABLED=true` in prod-like envs.

---

## PHASE 2 — Missing / broken APIs

| Gap | Severity | Detail |
|---|---|---|
| `GET /api/anpr/eval` not defined in gateway | P1 | Dashboard `live.ts:147` calls it; `ai/anpr` exposes `GET /eval` but it is **not proxied**. Degrades to `null` → OCR-accuracy panel blank. |
| `GET /api/traffic/metrics` / `/api/congestion/metrics` not defined | P1 | Dashboard `live.ts:167` calls both; neither exists → congestion-F1 panel blank. |

No truly broken (5xx/contract-violating) endpoints — the gateway's degradation policy prevents hard failures.

---

## PHASE 3 — Broken / incomplete flows

| Flow | Severity | Detail |
|---|---|---|
| ANPR OCR off by default | **P0** | `ingest/anpr` `DRY_RUN=True` (`config.py:55`) → real `ai/anpr /infer` is skipped, synthetic confidence used. Out-of-box "AI" flow is synthetic. |
| Evidence images as base64 data-URLs | **P0** | In DRY_RUN, `anpr_reads.image_url` holds `data:image/jpeg;base64,…` instead of an object-store URL — not production-shaped for police evidence. |
| Raw ANPR read stream not displayed | P2 | `/api/anpr/read/{camera_id}` + `/infer` + `/cameras` have **no frontend consumer**; plates surface only via alerts/KPIs. Orphan endpoints. |
| Driver PWA unauthenticated | **P0** | No bearer token; prod gateway enforces auth → all PWA calls 401. |

---

## PHASE 4 — Production readiness

| Concern | Status | Detail |
|---|---|---|
| `DRY_RUN` | ❌ defaults `True` | Must be `false` in prod; no startup guard ties it to `APP_ENV`. |
| Mock data (frontend) | ✅ guarded | Dead-code-eliminated; ship sentinel; AWS sets `VITE_DATA_MODE=live`. |
| Simulators (data sources) | ⚠️ | `vahan_sim`, `trucking_app` (20k synthetic trucks), `rfid` emulator, ANPR clip-replay are the default feeds. Only Vahan has a live path. |
| Missing integrations | ⚠️ | Real telematics, real RFID readers, live RTSP ANPR not wired (sim only). |
| Hardcoded values | ⚠️ | `_SYNTH_PLATES`, synthetic predictions, deterministic seeds, `LAST_REROUTE`/`tas_mock` in-memory state. |
| Disabled auth | ⚠️→✅ | Off locally (`AUTH_ENABLED=false`) but gateway refuses prod-like start without it; AWS example sets `true`. PWA still tokenless. |
| Missing env vars | ⚠️ | Prod requires `SUREPASS_API_TOKEN` (else Vahan live → 503), `AUTH_JWT_SECRET` (non-default), `DRY_RUN=false`. |

---

## Classified gap list

### P0 — Production blocking
1. `DRY_RUN=True` default → synthetic OCR in the AI flow.
2. Driver PWA has no authentication → 401 wall under prod auth.
3. ANPR evidence stored as base64 data-URLs, not object-store URLs.

### P1 — High priority
4. Vahan RC field mismatch (PWA `insurance_upto/fitness_upto/maker/model`).
5. Missing gateway proxies `/api/anpr/eval` and `/api/traffic|congestion/metrics`.
6. Simulator feeds need real-integration cutover plan (telematics, RFID, RTSP).

### P2 — Medium priority
7. Orphan ANPR endpoints (`/infer`, `/read`, `/cameras`) — wire a UI or retire.
8. Two parallel camera-health surfaces (`/api/kpi/cameras` vs `/api/anpr/cameras`).
9. Dashboard mock alert-kind set under-represents backend (`ABANDONED`, `ROUTE_DEVIATION`).
10. Unused PWA client method `fastag()`; verify `/api/parking/summary` key shape.

Fix details & code: [FIX_PLAN.md](FIX_PLAN.md).
