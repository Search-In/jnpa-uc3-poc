# Phase 2 · Track 3 — Geo-fencing Enforcement + AI Event Persistence

**Date:** 2026-07-08 · **Result:** Backend **live-validated on the running stack**; Web + Mobile UI **code-complete (tsc-clean), pending image build**.

Reuses the Phase-1 RDS framework tables (`geofence_events`, `digital_twin_events`, `alerts`, `notifications`, `decision_audit`) — the audit framework **code was not modified**. The geo-fence engine and AI-event sink write those tables directly (reuse) and call the framework helpers where convenient.

## The headline fix

The audit found geo-fence **detection was disconnected** from the editor: the anomaly service read hardcoded `jnpa_shared.corridor.NO_PARK_ZONES`, so editing zones in the DB had **no effect**. Track 3 introduces a **DB-driven enforcement engine** (`gateway/geofence.py`) that reads `jnpa.geofence_zones` live (30 s cache) and drives all enforcement. **Proven:** setting `warn_min=0` on a zone in the DB made the next entry instantly a `NO_PARKING_VIOLATION` — DB zone edits now change enforcement behaviour.

## Delivered

| Item | Detail |
|---|---|
| **Migration** | `0007_geofence_events_ext.sql` — adds `driver_id`, `event_type`, `dwell_seconds` to `geofence_events` (idempotent, indexed) + `init.sql` |
| **Geo-fence engine** | `gateway/geofence.py` — loads DB zones ([lon,lat]→(lat,lon)), point-in-zone (`point_in_polygon`), per-vehicle enter/exit state, dwell timing, escalation thresholds (`warn/notice/challan_min`). Emits `ENTER / EXIT / DWELL / NO_PARKING_VIOLATION / RESTRICTED_ENTRY` → `geofence_events` (+ `alerts` + `digital_twin_events` + `notifications` on violation) |
| **GPS integration** | hooked into the gateway **MQTT truck-telemetry pump** (real GPS → engine, sampled) + `POST /api/geo/evaluate` (mobile location / explicit push) |
| **AI event sink** | `gateway/routers/ai_events.py` — `POST /api/ai/event` (ANPR / vehicle-detection / illegal-parking / wrong-direction / queue / density) → `digital_twin_events` + `alerts` (when warranted) + `notifications`; `GET /api/ai/events` |
| **Read APIs** | `GET /api/geo/zones-active`, `/api/geo/vehicles-in-zones`, `/api/geo/events`, `/api/geo/violations`, `/api/ai/events` |
| **Web UI** | `web/src/screens/GeofenceEnforcement.tsx` (`/geofence-events`): active zones, vehicles-in-zone, entry/exit, violations, AI incidents (map polygons + live vehicles remain on the existing Geofencing/Live map) |
| **Mobile UI** | `mobile-pwa/src/screens/Zones.tsx` (`/zones` tab): `watchPosition` → engine; current zone, restricted vs no-parking lists, live warnings (restricted entry / no-parking / dwell) with vibration |

## API list (new)
```
POST /api/geo/evaluate            {vehicle_id,lat,lon,driver_id?}  -> transitions + inside_zones
GET  /api/geo/zones-active                                          -> zones the engine enforces
GET  /api/geo/vehicles-in-zones                                     -> live occupancy
GET  /api/geo/events?event_type=&limit=                             -> geofence_events
GET  /api/geo/violations?limit=                                     -> violations only
POST /api/ai/event                {event_type,vehicle_id,severity,…}-> persist + alert + notify
GET  /api/ai/events?event_type=&limit=                             -> AI events (digital_twin_events)
```

## Live validation (all ✅)

| # | Criterion | Evidence |
|---|---|---|
| 1 | Detection uses DB `geofence_zones` | `/api/geo/zones-active` → `source=jnpa.geofence_zones` (6) |
| 2 | No hardcoded zones drive enforcement | engine reads DB; `warn_min=0` DB edit changed behaviour |
| 3 | Entry events stored | `geofence_events ENTER` present |
| 4 | Exit events stored | `EXIT` present, with `dwell_seconds` |
| 5 | Dwell events stored | `NO_PARKING_VIOLATION` carries dwell |
| 6 | No-parking violation stored | `geofence_events` + `alerts(NO_PARKING_VIOLATION)` |
| 7 | AI events persisted | `digital_twin_events(WRONG_DIRECTION)` + `alerts` |
| 8 | Alerts generated | `RESTRICTED_ENTRY`, `NO_PARKING_VIOLATION`, `WRONG_DIRECTION` in `jnpa.alerts` |
| 9 | Notifications logged | `jnpa.notifications` grew on every violation/AI event |
| 10 | Restart doesn't lose history | data survived gateway restarts + PG crash-recoveries (all in Postgres) |

Snapshot: `geofence_events` = ENTER/EXIT/NO_PARKING_VIOLATION/RESTRICTED_ENTRY; `alerts` = 3 geofence/AI kinds; `digital_twin_events` GEOFENCE_VIOLATION + WRONG_DIRECTION; `notifications` accumulating. Web + mobile `tsc` exit 0.

## ⚠️ Environment note (unchanged — not a code defect)
3.8 GiB Docker VM; Postgres/TimescaleDB OOM-crashes under full continuous write load (recovers intact each time). Continuous writers (`truck-sim`, `rfid-*`, `anpr-ingest`) were paused during validation. **Raise Docker Desktop to ≥ 8 GiB**, then `docker start jnpa-truck-sim jnpa-rfid-consumer jnpa-rfid-emulator jnpa-rfid-correlator jnpa-anpr-ingest`. The geo-fence engine only writes on transitions (bounded), and evaluates on the sampled telemetry stream to stay light.

## To deploy UIs
```bash
docker compose build web mobile-pwa && docker compose up -d web mobile-pwa
# Web:    http://localhost:3000/geofence-events
# Mobile: http://localhost:3001  (Zones tab)
```

## Remaining (this track)
- Web + mobile image rebuild to surface screens (backend already serving).
- Wire the anomaly-service illegal-parking output to `POST /api/ai/event` (currently the engine covers geo-fence; anomaly-camera path can funnel here too).
- Optional: full-rate (unsampled) geo-fence evaluation once Postgres has headroom.
