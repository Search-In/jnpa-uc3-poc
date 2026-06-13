# Trucking-App PWA (Prompt 11)

The driver-facing **ETA / re-route advisory** app for the JNPA UC-III PoC. It is
the channel the platform uses to push live re-routes during the **TFC-1** (gate
closure) and **TFC-3** (cargo-surge) scenarios — the bid spec's "Trucking App
and web platform" deliverable.

Vite + React 18 + TypeScript, installable as a PWA (`vite-plugin-pwa`,
`injectManifest`). It is served two ways:

- **Mobile** — installed on a driver's phone (Add to Home Screen).
- **Web variant** — bundled into the control-room image (`web/`) and served at
  `http://localhost:3000/pwa`, so an evaluator without a phone can pair with
  `?device=DEV-...` and receive the re-route push live during the demo.

```
open http://localhost:3000/pwa            # web variant (stack up)
open http://localhost:3000/pwa?device=DEV-000001   # pre-paired for the demo
```

## Screens

| Screen            | What it shows                                                                                       |
| ----------------- | --------------------------------------------------------------------------------------------------- |
| **Pairing**       | Device pairing — QR + 6-digit code (PoC: no real OTP). `000001` → `DEV-000001`.                      |
| **Trip**          | Target gate, ETA, speed, remaining km, **traffic-ahead mini-map**, and the **"Slot at Gate"** widget (next TAS-mock window). |
| **Re-route**      | Full-screen confirmation when a re-route push arrives. **Accept** sends `state=ACK` back.            |
| **Inbox**         | Advisories, alerts and challans (last 24 h; cached in IndexedDB so it renders offline).              |
| **Profile / Vehicle** | The **VahanRecord** for the truck's plate, pulled through the gateway's orchestrated chain.      |

## How a re-route reaches the driver

When the control room (or a TFC-1/TFC-3 scenario step) pushes a new gate via
`POST /api/trucks/{id}/route`, the gateway dispatches the advisory on **three**
channels so it always lands within the 5 s SLA:

1. **WebSocket** — a `type=reroute` frame on `/api/ws`. A small dedicated worker
   (`src/workers/realtime.worker.ts`) owns the socket off the main thread and
   filters frames to the paired `device_id`. This is the live, foregrounded path.
2. **WebPush** — `pywebpush` delivers a notification to the registered service
   worker (`src/sw.ts`), which shows it *and* forwards the payload to any open
   page. This is the backgrounded path. Best-effort: needs VAPID keys (below).
3. **In-app polling** — while the socket is down, the app polls
   `GET /api/trucks/{id}/route/latest` every 3 s as a fallback.

All three converge on `RealtimeContext`, which de-dupes by timestamp, full-screens
the Re-route confirmation, and caches the advisory to IndexedDB for the Inbox.

## WebPush / VAPID keys

```bash
make vapid-keys     # generates a VAPID keypair and appends it to .env.local
make up             # gateway picks up VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY
```

With no keys configured, push is simply disabled — the PWA falls back to the
WebSocket + polling channels, so the demo never hard-depends on a key.

## Develop / build / test

```bash
make dev-pwa        # Vite dev server on :3002, proxies /api -> gateway :8000
make pwa-build      # production bundle (mobile-pwa/dist), base = /pwa/
make pwa-verify     # smoke-test /pwa + the push channel (stack must be up)
make pwa-e2e        # Playwright: pair, trigger a TFC-1 re-route, banner < 5 s
```

The web image (`web/Dockerfile`) builds this app with `PWA_BASE=/pwa/` and copies
the bundle to `nginx:/usr/share/nginx/html/pwa`, alongside the dashboard at `/`.

## Performance

- maplibre-gl (~700 KB) is lazy-loaded, so the initial JS is ~70 KB gzipped and
  first paint does not wait on the map — keeps FCP < 1.5 s on throttled Fast 3G.
- App shell is precached by the service worker for instant repeat loads.
- Targets: Lighthouse PWA ≥ 90 on a Galaxy A5x baseline; FCP < 1.5 s on Fast 3G.
