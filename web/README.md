# JNPA UC-III — Control-Room Dashboard (Sub-Criterion 4)

A React 18 + Vite + TypeScript SPA (Tailwind + shadcn-style primitives) that
talks to the API gateway (Prompt 8). It is the screen the evaluator sees during
scoring.

## Screens

| Route          | Screen                  | What it shows |
| -------------- | ----------------------- | ------------- |
| `/live`        | Live Operations         | Full-screen MapLibre: 4 gates (coloured by throughput vs. target), the 40 km NH-348 corridor (coloured by `jam_factor`), live truck dots (1:50) with 5-min fading trails, a jam-factor heatmap. Side panel: top-10 active alerts (click → pan + evidence image from MinIO). KPI row: gate throughput / queue / target. |
| `/advisory`    | Driver Advisory         | Trucks `AT_GATE_QUEUE` with ETA-to-gate + a re-route recommendation. **Push Re-route** → `POST /api/trucks/{id}/route` (TFC-3). |
| `/geofencing`  | Geo-fencing Manager     | `terra-draw` editor for no-parking / restricted polygons → `PUT /api/zones` (Postgres; the anomaly service reads them live). Editable 5/15/30-min escalation timeline per zone. |
| `/reports`     | Traffic-Police Reports  | Table of `WRONG_WAY / ILLEGAL_PARKING / OVERSPEEDING / ROUTE_DEVIATION` alerts, filterable by date/gate/severity/kind. **Export PDF** → `/api/reports/police?format=pdf` (server-side Playwright; one page per incident, evidence + RC + pre-filled e-Challan). |
| `/health`      | System Health           | Per-source chips (ANPR per camera, Vahan, Sarathi, FASTag, traffic, RFID, trucking app, ULIP, anomaly) with decision-path state, `last_ok`, p95 latency. Click → decision-log drawer. |
| `/what-if`     | What-If Console         | Scaffold for Prompt 10 (arms the header scenario banner; lists backend scenarios). |

## Maps

Two basemap providers (spec):

- **Primary** — Mapbox style, via `VITE_MAPBOX_TOKEN` (free tier OK).
- **Fallback** — Bhuvan (ISRO) WMS tiles (`VITE_BHUVAN_WMS`), used automatically
  when no Mapbox token is set, so the map always renders without a paid key.

## Data

- REST via TanStack Query against the gateway `/api`.
- Live alerts + sampled truck positions + traffic + fallback decisions over the
  `/api/ws` WebSocket (auto-reconnecting, shared app-wide).

The app always calls the **relative** `/api` path; the Vite dev proxy (dev) and
nginx (prod) forward it to the gateway, so there is no CORS to configure.

## Accessibility

Colour-blind-safe (Okabe–Ito) severity/flow palette in `src/lib/palette.ts` and
`tailwind.config.ts`; all foreground/background pairs meet WCAG AA contrast.
Visible focus rings, semantic landmarks, `role="status"` on the live indicator.

## Run

```bash
# Dev (hot reload on :5173, proxies /api -> gateway :8000)
make dev-web              # or: cd web && npm install && npm run dev

# Production image (nginx serving the bundle + proxying /api on :3000)
docker compose up -d web
open http://localhost:3000/live

# Type check / build / e2e
cd web
npm run typecheck
npm run build
npm run test:e2e          # Playwright; stack must be up (E2E_BASE_URL to override)
```
