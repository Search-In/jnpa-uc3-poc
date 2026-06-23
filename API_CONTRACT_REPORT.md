# API_CONTRACT_REPORT.md

**Project:** JNPA UC-3 Port Traffic Digital Twin
**Scope:** Contract validation of every frontend API call (Driver PWA + Main Dashboard) against the FastAPI gateway.
**Date:** 2026-06-23
**Method:** Static audit of call sites vs. router definitions and shared schemas.

Legend: ✅ aligned · ⚠️ works but mismatch/degraded · ❌ missing/broken

---

## 1. Driver PWA → Gateway

All PWA HTTP calls originate from [`mobile-pwa/src/lib/api.ts`](mobile-pwa/src/lib/api.ts) via one `http()` wrapper. **No `Authorization` header is ever attached** (device-pairing only).

| Frontend File | API Called | Method | Backend Route | Status | Issue |
|---|---|---|---|---|---|
| lib/api.ts:30 | `/healthz` | GET | main.py:174 | ✅ | — |
| lib/api.ts:33 | `/api/trucks/{id}` | GET | trucks.py:216 `truck_position` | ✅ | — |
| lib/api.ts:36 | `/api/gates` | GET | geo.py:49 `gates` | ✅ | — |
| lib/api.ts:37 | `/api/corridor` | GET | geo.py:88 `corridor_geometry` | ✅ | — |
| lib/api.ts:40 | `/api/tas/slots?gate_id=` | GET | scenario_ext.py:164 `tas_slots` | ✅ | Slots come from `tas_mock` (in-memory) |
| lib/api.ts:46 | `/api/trucks/{id}/route/latest` | GET | trucks.py:181 `latest_reroute` | ✅ | Served from in-memory `LAST_REROUTE` dict |
| lib/api.ts:50 | `/api/trucks/{id}/route/ack` | POST `{state}` | trucks.py:192 `ack_reroute` | ✅ | — |
| lib/api.ts:57 | `/api/alerts?since&kind&limit` | GET | alerts.py:65 `recent_alerts` | ✅ | — |
| lib/api.ts:65 | `/api/alerts/{id}/ack` | POST | alerts.py:103 `ack_alert` | ✅ | Resp `{id,ack,persisted}` matches |
| lib/api.ts:72 | `/api/parking/summary` | GET | parking.py:65 `summary` | ⚠️ | PWA reads `total_capacity/total_available/facilities` (all optional); gateway returns `{decision_path, minute_of_day, …upstream}` — verify keys present |
| lib/api.ts:78 | `/api/vahan/rc/{plate}` | GET | vahan.py:190 `vahan_rc` | ⚠️ | **Field mismatch — see §3** |
| lib/api.ts:79 | `/api/vahan/fastag/{plate}` | GET | vahan.py:241 `fastag_balance` | ✅ | Defined client-side, not called by any screen (dead client method) |
| lib/api.ts:85 | `/api/push/vapid-public-key` | GET | push.py:27 | ✅ | — |
| lib/api.ts:86 | `/api/push/subscribe` | POST `{device_id,subscription}` | push.py:34 | ✅ | — |
| lib/api.ts:91 | `/api/push/test/{id}` | POST | push.py:72 | ✅ | — |
| hooks/RealtimeContext.tsx:42 | `/api/ws` | WS | main.py (ws mount) | ✅ | Frames: `hello, reroute, reroute_ack, alert, truck_position, traffic` |

**PWA verdict:** 15/15 REST endpoints + WS exist with matching methods. Two issues: Vahan field mismatch (§3) and **no auth header** (breaks under `AUTH_ENABLED=true`, §4).

---

## 2. Main Dashboard → Gateway

Dashboard calls go through [`web/src/lib/api.ts`](web/src/lib/api.ts) and [`web/src/data/live.ts`](web/src/data/live.ts) (LiveAdapter). Bearer token attached when `getToken()` returns a session.

| Frontend File | API Called | Method | Backend Route | Status | Issue |
|---|---|---|---|---|---|
| lib/api.ts:34 | `/api/gates` | GET | geo.py:49 | ✅ | — |
| lib/api.ts:35 | `/api/corridor` | GET | geo.py:88 | ✅ | — |
| lib/api.ts:38 | `/api/traffic/snapshots` | GET | traffic.py:93 | ✅ | — |
| lib/api.ts:40 | `/api/traffic/predict?horizon_min` | GET | traffic.py:43 | ✅ | — |
| lib/api.ts:44 | `/api/trucks?limit&state` | GET | trucks.py:81 `list_trucks` | ✅ | — |
| lib/api.ts:48 | `/api/trucks/{id}/route` | POST | trucks.py:110 `reroute_truck` | ✅ | Resp incl. `advisory,push_delivered,sms` |
| lib/api.ts:58 | `/api/alerts` | GET | alerts.py:65 | ✅ | — |
| lib/api.ts:69 | `/api/kpi` | GET | kpi.py:60 | ✅ | — |
| live.ts:74 | `/api/kpi/strip` | GET | kpi.py:71 | ✅ | Catches failure → `[]` |
| lib/api.ts:70 | `/api/kpi/sources` | GET | kpi.py:109 | ✅ | — |
| lib/api.ts:71 | `/api/kpi/cameras` | GET | kpi.py:125 | ✅ | Camera health surface (not `/api/anpr/cameras`) |
| lib/api.ts:72 | `/api/debug/decisions?limit&api` | GET | debug.py:18 | ✅ | — |
| lib/api.ts:78 | `/api/zones` | GET | geo.py:106 | ✅ | — |
| lib/api.ts:79 | `/api/zones` | PUT | geo.py:138 `put_zones` | ✅ | — |
| lib/api.ts:86 | `/api/reports/police?format=json` | GET | reports.py:149 | ✅ | — |
| lib/api.ts:93 | `/api/reports/police?format=pdf` | GET | reports.py:149 | ✅ | — |
| lib/api.ts:100 | `/api/scenarios` | GET | scenarios.py:42 | ✅ | — |
| lib/api.ts:102 | `/api/scenarios/{name}/run` | POST | scenarios.py:55 | ✅ | — |
| lib/api.ts:107 | `/api/scenarios/{name}/reset` | POST | scenarios.py:67 | ✅ | — |
| lib/api.ts:112 | `/api/scenarios/handle/{id}/timeline` | GET | scenarios.py:78 | ✅ | — |
| auth.ts:99 | `/api/auth/login` | POST | auth.py:66 | ✅ | Resp `{access_token,role}` |
| lib/api.ts:121 | `/healthz` | GET | main.py:174 | ✅ | — |
| live.ts:100 | `/api/empty/allocations` | GET | empty_container.py:37 | ✅ | — |
| live.ts:102 | `/api/empty/kpi` | GET | empty_container.py:69 | ✅ | — |
| live.ts:103 | `/api/carbon/rollup` | GET | carbon.py:34 | ✅ | — |
| live.ts:105 | `/api/gate-data/leo/queue` | GET | gate_data.py:42 | ✅ | — |
| live.ts:107 | `/api/gate-data/customs/flags` | GET | gate_data.py:64 | ✅ | — |
| live.ts:109 | `/api/identity/gallery` | GET | identity.py:116 | ✅ | — |
| live.ts:111 | `/api/identity/verify` | POST | identity.py:87 | ✅ | — |
| live.ts:112 | `/api/parking/availability` | GET | parking.py:51 | ✅ | — |
| live.ts:118 | `/api/parking/summary` | GET | parking.py:65 | ✅ | — |
| live.ts:126 | `/api/control/fault` | GET | control.py:31 | ✅ | — |
| live.ts:127 | `/api/control/fault/{domain}` | POST | control.py:38 | ✅ | — |
| live.ts:129 | `/api/control/fault[/{domain}]` | DELETE | control.py:56/70 | ✅ | — |
| live.ts:147 | `/api/anpr/eval` | GET | **none** (only `/cameras,/infer,/read`) | ❌ | **Missing route** — 404, degrades to `null`; OCR-accuracy panel never populates |
| live.ts:167 | `/api/traffic/metrics` **or** `/api/congestion/metrics` | GET | **none** | ❌ | **Missing route** — 404, degrades to `null`; congestion-F1 panel blank |
| useGatewaySocket.ts:11 | `/api/ws` | WS | main.py | ✅ | Frames: `hello, alert, traffic, truck_position, decision, scenario_step, operator_banner` |

**Dashboard verdict:** 34/36 endpoints aligned. Two "realism probe" routes (`/api/anpr/eval`, `/api/traffic|congestion/metrics`) are **absent from the gateway**; the client is written to degrade gracefully, so these are silent feature-gaps, not crashes.

---

## 3. Vahan RC field-level contract mismatch (the only payload mismatch)

`GET /api/vahan/rc/{plate}` returns `VahanRecord` ([shared/jnpa_shared/schemas.py:109](shared/jnpa_shared/schemas.py#L109)). PWA Profile ([mobile-pwa/src/screens/Profile.tsx](mobile-pwa/src/screens/Profile.tsx)) consumes it:

| PWA field read | Backend field provided | Result |
|---|---|---|
| `owner_name_masked \|\| owner_name` | `owner_name_masked` ✅ | OK |
| `vehicle_class \|\| vehicle_category` | `vehicle_class` ✅ | OK |
| `fuel_type` | `fuel_type` ✅ | OK |
| `maker`, `model` | *(neither exists)* | ❌ "No data" |
| `insurance_upto \|\| insurance_validity` | `insurance_valid_to` | ❌ "No data" |
| `fitness_upto` | `fitness_valid_to` | ❌ "No data" |

No crash (every row has a `|| t("common.noData")` fallback), but **3 of 6 driver-facing vehicle fields render empty**. Fix in [FIX_PLAN.md](FIX_PLAN.md) §1.

---

## 4. Authentication contract

| Client | Token attached? | Mechanism | Prod behaviour |
|---|---|---|---|
| Dashboard | Yes, when session exists | `Authorization: Bearer` (`VITE_AUTH_ENABLED` build flag); login via `/api/auth/login` | Works |
| Driver PWA | **No — never** | Device-pairing in `localStorage`; device_id in path/body | **Breaks** — gateway enforces auth in prod-like env (`auth.py:275`); PWA calls return 401 |

Gateway RBAC (`gateway/auth.py` `_POLICY`) protects `/api/control,/api/scenarios,/api/identity,/api/reports,/api/gate-data,/api/debug,/checkin,/api/push`; everything else allows any authenticated role. There is **no DRIVER-scoped policy** for the PWA's core endpoints (`/api/trucks/{id}`, `/route/*`, `/api/vahan/rc`, `/api/alerts`). Fix in [FIX_PLAN.md](FIX_PLAN.md) §4.

---

## Summary

- **PWA:** 100% endpoint existence, 100% method match. 1 payload mismatch (Vahan), 1 auth gap.
- **Dashboard:** 94% (34/36). 2 missing optional probe routes, handled gracefully.
- **No broken methods, no 5xx contracts.** Backend's "never blank during demo" degradation masks the two missing routes.
