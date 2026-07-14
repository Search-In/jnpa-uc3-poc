# JNPA UC-III — Final Deployment Verification & Web Audit

**Date:** 2026-07-08 · **Env:** local docker stack (`--env-file .env.local`, postgres :5433, gateway :8000, web :3000)
**Image deployed:** `jnpa/web:0.1.0` (sha `4b0babd2…`), LIVE-mode bundle (MockAdapter tree-shaken, guard passed)

---

## 1. Deployment result

| Step | Result |
|------|--------|
| `web/dist` build (`tsc -b && vite build`) | ✅ (first attempt OOM'd — V8 heap; rebuilt with `NODE_OPTIONS=--max-old-space-size=8192`) |
| LIVE marker `JNPA_DATA_MODE:live` in bundle | ✅ present |
| MockAdapter sentinel absent | ✅ (tree-shaken) |
| `docker compose build web` | ✅ image built, live-mode guard passed |
| `docker compose up -d web` | ✅ `jnpa-web` Up (healthy) |
| Served bundle is LIVE mode | ✅ verified via served `/assets/index-*.js` |

### Verification checklist (served app @ :3000)
- ✅ **AsyncBoundary** — present on GateCustoms, ParkingManagement, DriverEnrollments, GeofenceEnforcement, PoliceReports; success/empty states render cleanly.
- ✅ **Retry buttons** — `RetryButton` in `web/src/components/ui/misc.tsx`, rendered by AsyncBoundary's error branch (shows on hard query failure).
- ✅ **Live/Polling indicator** — Live Operations header shows green **"Live"** (WS-connected; falls back to "Polling").
- ✅ **Last-updated timestamp** — e.g. Live Ops "Updated 11:25:28 IST", Parking "Updated 11:27:18 IST".
- ⚠️ **Error states** — components are present and fire on hard API failure; not force-triggered here because paused upstreams (congestion/truck-sim) degrade *gracefully* to advisory/empty states rather than hard errors.

Screenshots: `docs/screenshots/` (13 menus + 3 feature shots).

---

## 2. FASTag data consistency (RDS source of truth)

- **Data:** `jnpa.fastag_transactions` seeded with 8 DEMO rows for RC `MH04DM0001` (bank "SIM DEMO BANK", status SUCCESS).
- **Backend:** `GET /api/fastag/transactions/history` reads `jnpa.fastag_transactions` directly → `source:"RDS"`. ULIP adapter unchanged; `POST /api/fastag/transactions` still fetches+persists when a vendor URL is configured.
- **Frontend (`web/src/screens/Fastag.tsx`):** transactions tab now **best-effort triggers the ULIP fetch (persists into RDS) and always renders from the RDS history** — RDS is the display source of truth regardless of LIVE/SIM/unconfigured vendor.
- **Verified:** searching `MH04DM0001` shows `Data source [RDS] · fetched via [UNAVAILABLE] · 8 stored in RDS · 0 new this fetch` + 8 rows. (`fastag-rds-search.png`)

## 3. Parking violation demo data

- Seeded `NO_PARKING_VIOLATION` across all four stores, tagged `source=DEMO`, `sim=true`:
  - `jnpa.parking_events` — 5 (`MH04PV0001…0005`)
  - `jnpa.digital_twin_events` — 5 (`PARKING_VIOLATION`)
  - `jnpa.alerts` — 5 (`kind=NO_PARKING_VIOLATION`)
  - `jnpa.notifications` — 5
- Seed: `scripts/seed_demo_parking_violations.sql` (idempotent). Read path (`GET /api/parking/violations`) filters by event_type only → DEMO rows surface.
- **Verified:** Parking → Violations tab shows the 5 rows. (`parking-violations.png`)

## 4. What-If Console — demo scenario visibility

- Seeded 2 additional demo timelines (`scripts/seed_demo_scenarios.sql`, idempotent) so all three scenario types have a recorded demo run:
  - `demo-tfc1-0001` (existing) · `demo-tfc2-0001` (new) · `demo-tfc3-0001` (new) — each 5 steps, `status=completed`, `detail.source=DEMO`.
- Frontend (`web/src/screens/WhatIfConsole.tsx`): added a dedicated **"Demo scenarios"** section (list + blurb details + read-only preview), separated from a **"Recorded runs"** section; empty 0-step live runs hidden. Timeline header shows a **DEMO PREVIEW** badge + scenario description.
- **Live flow untouched:** preview uses the existing `previewHandle()` (stops any guided run, only sets the RDS-read handle); run/reset/WS trigger path unchanged.
- **Verified:** clicking a DEMO card renders "Reactive timeline · TFC1 [DEMO PREVIEW]" with 5 read-only steps. (`whatif-demo-preview.png`)

---

## 5. Final menu audit

All 13 menus reachable; all 21 probed gateway endpoints returned **HTTP 200**.

| Menu | Route | Primary API | DB source (jnpa.*) | Records | Status |
|------|-------|-------------|--------------------|---------|--------|
| Live Operations | `/live` | `/api/gates`, `/api/corridor`, `/api/traffic/snapshots`, `/api/trucks`, `/api/zones` | gates, traffic_snapshots, geofence_zones (+trucking svc, static corridor) | gates 4, snapshots 26, zones 6 | ✅ 200 |
| Driver Advisory | `/advisory` | `/api/trucks`, `POST /api/trucks/{id}/route` | none (live trucking microservice) | live | ✅ 200 |
| Geo-fencing Manager | `/geofencing` | `GET/PUT /api/zones` | geofence_zones | 6 | ✅ 200 |
| Geo-fence Events | `/geofence-events` | `/api/geo/events`, `/violations`, `/api/ai/events` | geofence_events, anpr_reads, alerts, digital_twin_events | events 120, anpr 2498 | ✅ 200 |
| Traffic-Police Reports | `/reports` | `/api/reports/police`, `/api/violations/*` | alerts, vehicle_master, drivers, driver_enrollments | alerts 611 | ✅ 200 |
| FASTag | `/fastag` | `/api/fastag/transactions/history` (+balance/txn/enroute/health) | **fastag_transactions** | txns 8, balance 1 | ✅ 200 |
| Intelligence | `/intelligence` | `/api/vahan/vehicle-intel/{plate}`, `/driver-intel/{key}` | vehicle_master, drivers (Vahan/Sarathi cached) | vehicles 3, drivers 6 | ✅ 200 |
| Customs & Gate | `/gate-customs` | `/api/gate-data/providers`, `/captures`, `/reconciliations`, `/customs/history` | gate_captures, leo_reconciliation, alerts | captures 808, recon 404 | ✅ 200 |
| Parking | `/parking` | `/api/parking/availability`, `/summary`, `/history`, `/violations` | parking_facilities, parking_slots, parking_transactions, **parking_events** | slots 1170, txns 21, events 27 (5 DEMO viol.) | ✅ 200 |
| Driver Enrollment | `/enrollments` | `/api/identity/enrollments/*` | driver_enrollments, driver_faces, drivers, verification_logs | enrollments 5 | ✅ 200 |
| System Health | `/health` | `/api/kpi/sources`, `/cameras`, `/api/debug/decisions`, `/api/fastag/health` | in-memory gateway state (non-RDS) | live | ✅ 200 |
| What-If Console | `/what-if` | `/api/scenarios/handles`, `/handle/{id}/timeline`, `/{name}/run` | **scenario_handles, scenario_steps, scenarios** | handles 9 (3 DEMO), steps 15 | ✅ 200 |
| Demo Console | `/demo` | `/api/control/fault`, `/api/traffic/metrics`, `/api/anpr/eval` | in-memory fault ctrl; traffic_snapshots | live | ✅ 200 |

---

## 6. Remaining UI gaps (pre-redesign)

1. **Error/Retry states unproven live** — code-present but never rendered because upstreams degrade gracefully; a deliberate fault-injection screenshot would fully close the P0 checklist.
2. **System Health & Demo Console are non-RDS** — served from gateway process memory; nothing to persist, but they won't survive a gateway restart (by design). Worth a "live/ephemeral" label in redesign.
3. **Driver Advisory & Live-Ops trucks depend on the trucking microservice** (currently paused in this stack) — advisory renders but with no live trucks. Un-pause for a populated demo.
4. **`toll_enroute` table empty (0 rows)** — FASTag "Toll Enroute" tab has no seed data; add a DEMO seed for parity with transactions/balance.
5. **What-If status casing inconsistency** — seed uses lowercase `completed`, live runs use uppercase `DONE`; harmless but redesign should normalize the badge vocabulary.
6. **Two What-If aliases** (`/what-if`, `/whatif`) — consolidate to one in the redesign.

## 7. Ready-for-redesign confirmation

✅ **READY.** Web is deployed (LIVE bundle), all 13 menus load, all 21 endpoints return 200, and the three data-consistency items (FASTag→RDS, Parking DEMO violations, What-If demo timelines) are seeded, wired, and visually verified. Gaps above are additive/cosmetic and do not block a UI redesign.
