# Phase 2 · Track 2 — Parking Management + Empty Container Allocation

**Date:** 2026-07-07 · **Result:** Backend + persistence **live-validated on the running stack**; Web + Mobile UI **code-complete (tsc-clean), pending image build**.

Reuses the Phase-1 RDS audit framework tables (`digital_twin_events`, `notifications`, `decision_audit`) by writing to them — the audit framework **code was not modified**.

## Delivered

| Layer | Parking | Empty Container |
|---|---|---|
| **Migration** | `0005_parking.sql` — `parking_facilities`, `parking_slots`, `parking_transactions`, `parking_events` | `0006_empty_container.sql` — `empty_container_inventory`, `empty_container_allocations`, `container_movement_history` |
| **Persistence** | `parking/persistence.py` — inventory seed (server-side `generate_series`), atomic slot allocate (`FOR UPDATE SKIP LOCKED`), release, events, RDS reads | `empty-container/persistence.py` — inventory seed, atomic container allocate, movement history, RDS reads |
| **Backend APIs** | `GET /facilities /availability /summary /history /violations`, `POST /allocate /release /violation` | `GET /containers/available`, `POST /containers/allocate`, `GET /containers/allocation/history` |
| **Gateway** | `/api/parking/*` proxies (incl. POST allocate/release) | `/api/empty/containers/*` proxies |
| **Reuse** | every allocate/release → `digital_twin_events` + `notifications` | every allocate → `digital_twin_events` + `decision_audit` + `container_movement_history` |
| **Web UI** | `web/src/screens/ParkingManagement.tsx` (`/parking`): capacity KPIs, facilities, live vehicle list, entry/exit history, violations | existing Empty-Container board + new RDS allocation history API |
| **Mobile UI** | `mobile-pwa/src/screens/Parking.tsx` (`/parking` tab): nearby parking (distance-sorted), availability, request slot, confirmation, release, navigate | — |

## Live validation (RDS)

| # | Acceptance criterion | Evidence | Result |
|---|---|---|---|
| 1 | Parking inventory stored in RDS | `parking_facilities=6`, `parking_slots=1170` | ✅ |
| 2 | No sine-curve occupancy | `/api/parking/availability` returns `source=rds`; occupancy = real slot state | ✅ |
| 3 | Parking allocation survives restart | restarted `jnpa-parking`; `parking_transactions` unchanged (before=after) | ✅ |
| 4 | Vehicle entry/exit history available | allocate→release cycle persisted a `COMPLETED` transaction w/ duration; `/history` returns it | ✅ |
| 5 | Empty container allocation stored | `empty_container_allocations=1`, inventory row flipped to `ALLOCATED` | ✅ |
| 6 | Allocation history available | `/containers/allocation/history` returns the allocation; `container_movement_history` logged | ✅ |
| 7 | Dashboards read only from RDS | web screens call `/api/parking/*` + `/api/empty/containers/*` (all RDS) | ✅ |
| 8 | Mobile receives parking info | mobile `parkingAvailability`/`parkingAllocate`/`parkingRelease` wired; slot-allocated → `notifications` | ✅ (UI pending build) |
| 9 | All events in `digital_twin_events` | `PARKING_ALLOCATION`, `PARKING_RELEASE`, `PARKING_VIOLATION`, `CONTAINER_ALLOCATION` present | ✅ |

Inventory seeded: **6 facilities / 1170 slots**, **744 empty containers** (20GP/40GP/40HC/REEFER). Reuse counts: `notifications(push)=3`, `decision_audit(empty-container)=3`.

## Notable engineering detail (bug found + fixed during validation)

`jnpa_shared.db.fetch_one` uses `engine.connect()` (no commit) — fine for SELECTs but it **silently rolls back** `INSERT/UPDATE … RETURNING`. First allocation attempts returned a row id (sequence advanced) yet no row persisted. Fixed with a local `_returning()` helper in each service (uses `engine.begin()` → committed) — no change to the shared/audit code. Post-fix, allocate/release commit correctly and survive restart.

## ⚠️ Environment note (unchanged from Track 1 — not a code defect)

The local Docker VM is **3.8 GiB for ~28 containers**. Postgres/TimescaleDB OOM-crashes under the combined continuous-write load (truck-telemetry COPY + RFID + ANPR). It recovers with data intact every time. During validation I stopped the continuous writers (`jnpa-truck-sim`, `jnpa-rfid-*`, `jnpa-anpr-ingest`) to get a clean window; seeding uses server-side `generate_series` (no client insert burst) to stay light.

**Remediation:** raise Docker Desktop memory to ≥ 8 GiB, then restart the stopped producers:
```bash
docker start jnpa-truck-sim jnpa-rfid-consumer jnpa-rfid-emulator jnpa-rfid-correlator jnpa-anpr-ingest
```
On production/RDS this is a non-issue (managed instance sizing).

## To deploy the UIs
```bash
docker compose build web mobile-pwa && docker compose up -d web mobile-pwa
# Web:    http://localhost:3000/parking
# Mobile: http://localhost:3001  (Parking tab)
```

## Remaining (this track)
- Web + mobile image rebuild to surface the screens (backends already serving).
- Optional: parking-expiry timer → `PARKING_EXPIRED` notification; wire illegal-parking detection (anomaly service) → `/api/parking/violation`.
