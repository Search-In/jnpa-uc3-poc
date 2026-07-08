# Phase 3 — Production PWA Redesign + Final Hardening

**Date:** 2026-07-08 · No backend contracts changed; all screens consume real RDS-backed APIs.

**Honesty note on deliverables:** the web/mobile run as **built images**, and the web bundle (@arcgis/core + Calcite) **does not finish building on this host** alongside the running stack. So I do not provide fabricated "screenshots" or claim visual validation I didn't perform. What I *did* validate is stated as such; what needs a real device/build session is called out with exact repro.

## What was delivered & how it was validated

| Area | Deliverable | Validation |
|---|---|---|
| **Security hardening** | JWT refresh, logout (session revocation), session status, 8h expiry — `gateway/routers/otp.py` | ✅ **live-validated** (below) |
| **Mobile redesign** | 6-tab IA (Home · Trip · Map · Parking · Alerts · Profile); new **Alert Center** (categories) + **Map** screen; **Profile** DL/compliance + real logout; offline cache | ✅ `tsc` clean + **real `vite build` succeeds** (deployable PWA bundle, 65 modules, SW precache) |
| **Offline mode** | `cacheGet/cacheSet/cached` (idb-keyval) — profile/route/parking/alerts/zones cached; fetch-through-cache falls back offline | ✅ builds; used by Map + Alert Center |
| **Web new pages** | Gate-Customs, Parking, Geofence, Intelligence (built earlier) + RBAC + nav + i18n | ✅ `tsc -b` clean; ⚠️ full ArcGIS bundle **build times out on this host** (see below) |

### Security hardening — live validation
```
otp verify           -> 200 (device bound)
otp refresh (active) -> token, expires_in=28800 (8h)
session status       -> bound=true, active=true
otp logout           -> 200 (binding active=false)
refresh AFTER logout -> 401  ✅ session revoked
decision_audit(device-logout) -> 1
```
Part 4 (Security) complete: OTP login ✅ · device binding ✅ · JWT refresh ✅ · session expiry ✅ · logout/revocation ✅.

## Mobile screens (6-tab, all real-data)
- **Home** — `screens/Home.tsx` (status chip already driven by `DriverSession`).
- **Trip** — `screens/Trip.tsx` (ETA, parking tile, live truck).
- **Map** — `screens/MapView.tsx` (NEW): MiniMap + truck + gates + corridor + congestion legend + parking/zone counts (cached).
- **Parking** — `screens/Parking.tsx` (Track 2: nearby/allocate/release).
- **Alerts** — `screens/AlertCenter.tsx` (NEW): category tabs Traffic/Parking/Customs/Geo-fence/AI/Vehicle over `/api/alerts`, with per-kind "required action" text + offline cache.
- **Profile** — `screens/Profile.tsx`: Vahan RC + **DL/compliance** (via `/api/auth/otp/session` → `/api/vahan/driver-intel`) + **real logout** (server-side revoke).
- **Login** — `screens/Pairing.tsx`: real mobile→OTP→verify→session (Track 4/5).

## Web build — the one thing I could NOT complete here
`vite build` for `web/` (ArcGIS Maps SDK + Calcite) does not finish within a 5-min window on this machine while the 28-container stack runs — it is memory/CPU-bound, not a code error (**`tsc -b --noEmit` passes**). The 4 new pages (`/gate-customs`, `/parking`, `/geofence-events`, `/intelligence`) are wired (routes + RBAC in `auth.ts` + nav in `Shell.tsx` + en/hi/mr i18n).

**Repro on an adequately-resourced machine / CI:**
```bash
# Web (needs ~4-6 GB free RAM for the ArcGIS bundle)
cd web && pnpm build            # or: docker compose build web
docker compose up -d web        # http://localhost:3000
# Mobile (builds in ~2s here)
cd mobile-pwa && pnpm build && docker compose up -d mobile-pwa
```
**Screenshots** must be captured from a running build (Playwright is configured in `web/e2e` and `mobile-pwa/e2e`):
```bash
cd web && npx playwright test --update-snapshots   # or a screenshot script per route
```
I did not capture screenshots because I cannot complete the web bundle build in this environment; producing mock screenshots would misrepresent the state.

## Performance notes (Part 5)
- **PWA loading / offline startup** — `injectManifest` SW precaches 11 entries (~1.2 MB); `cached()` serves last profile/route/alerts with zero network.
- **Map rendering** — maplibre (mobile) is light and builds in ~2 s; the heavy ArcGIS bundle is web-only (control room), not on the driver device.
- **Background GPS / battery** — Zones screen uses `watchPosition` with `maximumAge: 10s`; geo-fence evaluation is server-side + sampled (Track 3) so the device isn't doing heavy compute.
- **Recommendation** — code-split the web ArcGIS bundle (dynamic import of the map module) to cut build time + first-load; add a route-level `React.lazy` for the heavy screens.

## Final tender compliance matrix
| Track | Backend + RDS | UI code | UI deployed | Notes |
|---|:--:|:--:|:--:|---|
| RDS persistence framework | ✅ | — | — | validated |
| 1 Customs & Gate | ✅ | ✅ | build web | |
| 2 Parking + Empty Container | ✅ | ✅ | build web/mobile | |
| 3 Geo-fencing + AI events | ✅ | ✅ | build web/mobile | |
| 4 Vahan/Sarathi Intelligence | ✅ | ✅ | build web | |
| 5 OTP + security hardening | ✅ | ✅ | mobile **builds ✅** | |
| **3 Mobile redesign** | ✅ (reuses APIs) | ✅ | **mobile builds ✅** | screenshots need running build |

## Production readiness
**Ready:** all 9 RDS migrations idempotent; every module RDS-backed + audited (`api_audit_log`, `digital_twin_events`, `notifications`, `decision_audit`); OTP/session security; mobile PWA builds & precaches for offline; RBAC on every web route.
**Before go-live:**
1. **Raise host/CI resources** and run `docker compose build web mobile-pwa` (the ArcGIS web bundle can't build here) → deploy → capture screenshots.
2. **Provision credentials** for LIVE mode: `SUREPASS_API_TOKEN` (Vahan/Sarathi), `FASTAG_ULIP_URL`+key, `VAPID_*`, `SMS_PROVIDER` (OTP/SMS), live traffic keys, gate/customs (e-Seal/ICEGATE) endpoints.
3. **DB sizing** — this local 3.8 GB VM OOMs Timescale under load; production RDS must be sized for the telemetry/ANPR write rate.
4. **Sarathi name mapping** — driver name currently defaults to "DL Holder" (sim field ≠ extraction keys); one-line fix once the real provider field is confirmed.
5. Run the Playwright e2e + screenshot suites against the deployed build.
