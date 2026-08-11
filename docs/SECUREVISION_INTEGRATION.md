# SecureVision AI Surveillance — Integration

Mirrors the conventions of `docs/TOMTOM_TRAFFIC_INTEGRATION.md` and
`docs/ULIP_LOGISTICS_MODULE.md`: an env-driven vendor client, a gateway proxy, a
service layer that owns every vendor-specific decision, and screens that consume
normalised models.

**Status:** implemented and verified against a stub vendor. Live-vendor
verification against `svapidev.phylon.in` is pending credentials (see _Blockers_).

---

## 1. Why the integration is proxied, not called from the browser

SecureVision authenticates at `POST /api/auth/login` and `GET /api/auth/me` — the
**same relative paths** this application's own sign-in uses
(`gateway/routers/auth.py`). The SPA calls relative `/api/*` (proxied by Vite in
dev, nginx in prod), so a browser-direct integration would collide with JNPA
authentication.

Everything therefore reaches the browser under **`/api/sv/*`**, authorised by the
existing JNPA RBAC, while the SecureVision service credential stays in the
gateway process.

```
Browser ──JNPA JWT──▶ nginx/Vite ──▶ gateway /api/sv/*
                                        │ _POLICY: CONTROL_ROOM | CUSTOMS
                                        │ services/securevision (normalise)
                                        ▼
                              integrations/securevision (login + cached token)
                                        ▼
                                  SecureVision API
```

The browser never holds a SecureVision token, never sees the vendor host, and no
`VITE_SECUREVISION_*` variable exists.

## 2. Layout

| Path                                      | Role                                                   |
| ----------------------------------------- | ------------------------------------------------------ |
| `integrations/securevision/client.py`     | the only layer that speaks HTTP to the vendor          |
| `integrations/securevision/schemas.py`    | tolerant pydantic view of vendor shapes                |
| `integrations/securevision/exceptions.py` | typed failure vocabulary                               |
| `services/securevision/normalize.py`      | media URLs, timestamps, ISO-6346 cross-check, verdicts |
| `services/securevision/cameras.py`        | explicit SecureVision ↔ JNPA camera mapping            |
| `services/securevision/analyses.py`       | in-process registry of this gateway's uploads          |
| `services/securevision/tickets.py`        | short-lived MJPEG stream tickets                       |
| `gateway/routers/securevision.py`         | the `/api/sv/*` surface                                |
| `web/src/lib/securevision.ts`             | client types + pure presentation helpers               |
| `web/src/hooks/useSecureVision.ts`        | shared query hooks                                     |
| `web/src/components/panels/sv/*`          | panels embedded in existing screens                    |
| `web/src/screens/VideoAnalytics.tsx`      | the one new screen                                     |

## 3. Configuration

Gateway-only (never `VITE_`):

```
SECUREVISION_BASE_URL=https://svapidev.phylon.in
SECUREVISION_USERNAME=            # service account
SECUREVISION_PASSWORD=            # service account
SECUREVISION_TIMEOUT_S=15
SECUREVISION_UPLOAD_TIMEOUT_S=180
SECUREVISION_RETRIES=2
SECUREVISION_TOKEN_TTL_S=1800
SECUREVISION_STREAM_TICKET_TTL_S=120
SECUREVISION_CAMERA_MAP={"CAM-01":"CAM-NSICT-ENT"}
```

No credential ⇒ every `/api/sv` route answers a clean `NOT_CONFIGURED`; no other
console surface is affected.

## 4. Endpoints

| Method                | Path                                                        | Notes                                                      |
| --------------------- | ----------------------------------------------------------- | ---------------------------------------------------------- |
| GET                   | `/api/sv/health`                                            | posture + reachability + face-model status; never raises   |
| GET                   | `/api/sv/cameras`                                           | the configured camera mapping                              |
| GET                   | `/api/sv/analyses`                                          | uploads from **this gateway process** (`persisted: false`) |
| POST                  | `/api/sv/analytics/video/upload`                            | multipart; MIME + magic-byte checked, size-capped          |
| DELETE                | `/api/sv/analytics/video/{id}`                              | drops one cached analysis                                  |
| GET                   | `/api/sv/analytics/incident/{i01\|i02\|i07\|i09\|i12\|all}` | normalised                                                 |
| POST                  | `/api/sv/analytics/video/{id}/stream-ticket`                | mints the MJPEG credential                                 |
| GET                   | `/api/sv/analytics/video/{id}/stream`                       | MJPEG relay (ticket-authenticated)                         |
| GET                   | `/api/sv/media/{path}`                                      | evidence/snapshot proxy (authenticated)                    |
| GET/POST/PATCH/DELETE | `/api/sv/faces[/{pk}]`                                      | site-personnel gallery                                     |
| GET                   | `/api/sv/faces/{pk}/photo`                                  | enrolment photo (binary)                                   |
| GET                   | `/api/sv/faces/events`, `/api/sv/faces/status`              | recognition log, model diagnostics                         |

**Not implemented, by decision:** vendor user management (`/api/auth/users`) — the
integration uses one service account and authorises with JNPA `_POLICY`; and the
bulk `DELETE /api/analytics/video`, which wipes every analysis irreversibly with
no server-side confirmation.

## 5. Screen mapping

| Vendor capability                  | Host screen                                                        | Placement                                                                                 |
| ---------------------------------- | ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------- |
| I-01 plate, I-09 container         | **Vehicle & Driver Intelligence** (primary), Camera AI (secondary) | "SecureVision AI Intelligence" section in Vehicle 360; "SecureVision AI" tab on Camera AI |
| I-02 vehicle count                 | Camera AI                                                          | same tab                                                                                  |
| I-07 person in zone                | **Geo Analytics → AI Events**                                      | below the JNPA AI-event table, separately badged                                          |
| I-12 camera tamper                 | **Gate & Lane Board → Camera Degraded**                            | separate "AI Tamper Check", never merged with the ANPR rung                               |
| Incident ALL                       | **Reports & Enforcement**                                          | new "AI Video Analysis" tab, AI-generated badge on the narrative                          |
| Faces CRUD                         | **Driver Enrollment**                                              | new "Site Personnel" tab (drivers unchanged)                                              |
| Face events                        | Geo Analytics → AI Events                                          | below I-07                                                                                |
| Face model status                  | **System Health → Integrations**                                   | SecureVision card                                                                         |
| Upload → analyse → replay → delete | **`/video-analytics`** (NEW)                                       | the only workflow with no existing home                                                   |

## 6. Decisions recorded

- **Upload-clip analytics, not live CCTV.** The supplied API documents no
  continuous-ingestion endpoint. The UI is named and worded accordingly, and
  `/api/sv/health` reports `mode: UPLOAD_CLIP_ANALYTICS`.
- **No persistence.** SecureVision publishes no incident-history API. Persisting
  normalised incidents needs a migration, a retention rule and — for I-07, which
  carries person names — a DPDP retention decision that has not been made. The
  analysis list is session-scoped and says so (`persisted: false`).
- **Face galleries stay separate.** `/api/identity` remains authoritative for
  drivers; `/api/sv/faces` is site personnel. No dual-write, no sync.
- **DPDP.** Enrolling a real face is real biometric processing, so it goes through
  the existing `enforce_dpdp` gate: **403 unless `ALLOW_REAL_BIOMETRICS=true`**.
  Reads of I-07 / face events emit `audit_identity_access` records
  (`purpose=AUDIT_REVIEW`). No biometric is stored on the JNPA side.
- **Camera mapping is explicit.** No fuzzy matching. An unmapped code renders
  "Camera mapping unavailable" and keeps the vendor's own code visible.
- **Stream auth is a ticket.** An `<img>` cannot send an `Authorization` header;
  rather than making footage public, an authenticated, RBAC-checked call mints an
  opaque ticket scoped to one analysis with a 120 s TTL — the same shape
  `/api/ws?token=` already uses.

## 7. Production proxy

`web/nginx/default.conf` and `default.local.conf` add a regex location, declared
**before** the generic `/api/` block, for the stream only:

```
location ~ ^/api/sv/analytics/video/[^/]+/stream$ {
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 3600s;
    ...
}
```

Without `proxy_buffering off` nginx holds frames back until a buffer fills, which
turns a replay into a stutter; the 60 s read timeout would also cut a `loop=true`
stream.

## 8. Tests

- `tests/test_securevision.py` — 36 tests: login/token cache/one forced re-login,
  403/404/409/422/503, timeout + 5xx retry, credential redaction, normalisation,
  camera mapping, tickets, router error mapping, upload validation, the DPDP gate,
  and that neighbouring JNPA policies are unchanged.
- `web/src/lib/securevision.test.ts` — 14 tests, incl. UNVERIFIED never rendering
  as an accusation and every vendor error mapping to actionable copy.
- `web/src/data/adapter.contract.test.ts` — SecureVision added to the adapter
  contract (Mock parity, DEMO labelling, three-verdict coverage).
- `web/e2e/` — nav contract includes Video Analytics; Camera AI keeps its tabs.

## 9. Blockers

1. **Production base URL + credentials.** `svapidev.phylon.in` is a dev host; no
   live verification has been performed.
2. **Camera mapping** must be supplied by the client (`SECUREVISION_CAMERA_MAP`);
   until then detections cannot be attributed to a JNPA camera.
3. **SecureVision zone names** are unmapped to `core.zone` — no vendor zone API.
4. **Persistence + DPDP retention** decisions outstanding (see §6).
5. **Rate limits / SLA / CORS** are undocumented by the vendor.
