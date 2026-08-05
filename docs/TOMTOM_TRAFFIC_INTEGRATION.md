# TomTom Traffic Integration

Live traffic intelligence for the NH-348 JNPA corridor, built end-to-end on the
same Clean-Architecture pattern as the Weather module (Open-Meteo /
OpenWeatherMap): integration client → service → repository → router → typed
frontend client → tile. Everything is **additive** — the pre-existing
`/api/traffic` congestion-model endpoints (`/predict`, `/congestion-scan`,
`/metrics`, `/snapshots`) and the `core.traffic_snapshot` pipeline are
untouched.

## Architecture flow

```
React UI  (TrafficTile / DriverAdvisory — TanStack Query, polls the gateway)
   ↓
FastAPI   (gateway/routers/traffic.py — GET /api/traffic/current | /health)
   ↓
TrafficService        (services/traffic/service.py — fallback + normalisation)
   ↓                       ↓                    ↓
TomTom Client         Redis cache        core.traffic_reading
(integrations/tomtom) (CACHED rung)      (DATABASE rung, via repository.py)
   ↓
TomTom APIs  (Traffic Flow v4 · Traffic Incidents v5 · Routing v1 [optional])
```

The **backend is the only TomTom consumer**. The API key is read from the
process environment, sent only to `api.tomtom.com`, redacted from logs and
exception text, and never exposed to the frontend (no `VITE_` variable, no
browser call).

## Layers

| Layer | Files | Responsibility |
|---|---|---|
| Integration | `integrations/tomtom/client.py`, `schemas.py`, `exceptions.py` | HTTP-only client (timeout, retry w/ exponential backoff), pydantic validation, normalisation, typed failure vocabulary |
| Service | `services/traffic/service.py` | Orchestrates flow + incidents concurrently, fallback chain, source metadata, Redis write-back, best-effort persistence |
| Repository | `services/traffic/repository.py` | The only layer that speaks SQL to `core.traffic_reading` (raw SQL, no ORM) |
| Router | `gateway/routers/traffic.py` | Thin `/api/traffic/current` + `/api/traffic/health` endpoints (additive to the existing router) |
| Frontend | `web/src/lib/api.ts`, `lib/types.ts`, `lib/traffic.ts`, `components/panels/TrafficTile.tsx` | Typed gateway client + pure presentation helpers + tile |

### Retry policy (client)

Retried with exponential backoff (`TOMTOM_RETRIES` attempts after the first):
timeouts, network errors, **5xx**. Fail-fast (never retried): **401** invalid
key, **403** forbidden, **429** rate limit — retrying a rejected request cannot
help and would burn the daily call budget.

### Fallback strategy (service)

```
LIVE  (fresh TomTom flow + incidents)
  ↓ on failure / no key
CACHED     (Redis, key jnpa:cache:tomtom:{lat}:{lon}, TTL GATEWAY_CACHE_TTL_TOMTOM_S)
  ↓
DATABASE   (last persisted core.traffic_reading row for the coordinate)
  ↓
SYNTHETIC  (deterministic near-free-flow corridor values, clearly tagged)
```

The API **never breaks** because TomTom is unavailable: `status`
(`LIVE`/`DEGRADED`/`OFFLINE`), `source`
(`TOMTOM`/`TOMTOM_CACHE`/`TOMTOM_DB`/`SYNTHETIC`) and `decision_path` always
say which rung answered. A fully-LIVE answer is written back to Redis and
appended to `core.traffic_reading` (both best-effort).

### Congestion level

Derived from the speed ratio `current_speed / free_flow_speed`:

| Ratio | Level |
|---|---|
| ≥ 0.80 | `LOW` |
| ≥ 0.60 | `MEDIUM` |
| ≥ 0.40 | `HIGH` |
| < 0.40 (or road closure) | `SEVERE` |
| speeds missing | `UNKNOWN` |

`delay_seconds = max(0, current_travel_time − free_flow_travel_time)`.

## Environment variables

```bash
TOMTOM_API_KEY=            # backend-only; empty -> LIVE rung disabled
TOMTOM_TIMEOUT_S=5         # per-attempt HTTP budget
TOMTOM_RETRIES=2           # retries after the first try (timeout/network/5xx only)
# Optional endpoint overrides (defaults are the official TomTom endpoints):
TOMTOM_FLOW_URL=https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json
TOMTOM_INCIDENTS_URL=https://api.tomtom.com/traffic/services/5/incidentDetails
TOMTOM_ROUTING_URL=https://api.tomtom.com/routing/1/calculateRoute
GATEWAY_CACHE_TTL_TOMTOM_S=120   # Redis CACHED-rung TTL
```

Declared in `.env.local.example`, `.env.aws.example`, `docker-compose.yml`
(gateway service) and `gateway/config.py` (`GatewayConfig.tomtom_*`,
`tomtom_enabled`). `TOMTOM_API_KEY` was already listed in the shared settings
(`shared/jnpa_shared/config.py`).

## Database

Additive migration `infra/postgres/v3/0107_traffic_reading.sql` creates:

```
core.traffic_reading (
    id bigserial PK, latitude, longitude,
    current_speed, free_flow_speed, congestion_level, delay_seconds,
    source text DEFAULT 'TOMTOM', payload jsonb, created_at timestamptz)
-- indexes: created_at DESC; (round(lat,2), round(lon,2), created_at DESC)
```

Distinct from the pre-existing `core.traffic_snapshot` (per-corridor-segment
simulation/ingest rows for the map overlay) — no duplication. `payload` keeps
the full normalised traffic block + incident list so the DATABASE fallback
rung loses nothing. `gateway/traffic_ext.py` mirrors the DDL for
`JNPA_RUNTIME_DDL=1` dev bootstraps (lock-step asserted by
`tests/test_tomtom.py`).

## API

### `GET /api/traffic/current[?latitude=..&longitude=..]`

Coordinates default to the configured port location (`port_lat`/`port_lon`).

```json
{
  "status": "LIVE",
  "source": "TOMTOM",
  "decision_path": "LIVE",
  "location": {"latitude": 18.9489, "longitude": 72.9492},
  "traffic": {
    "current_speed": 43, "free_flow_speed": 50,
    "current_travel_time": 540, "free_flow_travel_time": 465,
    "congestion_level": "LOW", "delay_seconds": 75.0,
    "road_closure": false, "confidence": 0.94, "road_class": "FRC0"
  },
  "incidents": [
    {"type": "ROAD_WORKS", "description": "Roadworks", "severity": "MODERATE",
     "road": "NH-348 (Y Junction → Karal Phata)", "delay": 120}
  ],
  "incident_count": 1,
  "sources": {"traffic": "LIVE", "incidents": "LIVE"},
  "cache_age_s": null,
  "units": {"current_speed": "km/h", "delay_seconds": "s", "...": "..."},
  "timestamp": "2026-07-28T10:00:00+00:00"
}
```

### `GET /api/traffic/health`

```json
{
  "system": "TRAFFIC", "provider": "TOMTOM", "configured": true,
  "api_key_required": true,
  "flow_url": "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json",
  "incidents_url": "https://api.tomtom.com/traffic/services/5/incidentDetails",
  "timeout_s": 5.0, "retries": 2, "cache_ttl_s": 120,
  "default_location": {"latitude": 18.9489, "longitude": 72.9492}
}
```

The key itself is never echoed.

## UI

- **Live Operations** (`web/src/screens/LiveOperations.tsx`) — `TrafficTile`
  sits in the capability-tile grid: WeatherTile → **TrafficTile** → CarbonTile
  → ParkingBoard (→ EmptyContainerBoard). Shows status chip
  (LIVE/DEGRADED/OFFLINE), current + free-flow speed, congestion chip
  (LOW/MEDIUM/HIGH/SEVERE), delay, incident list/count, `TOMTOM` source chip,
  decision-path badge, staleness footer. Polls every 60 s.
- **Driver Advisory** (`web/src/screens/DriverAdvisory.tsx`) — a "Traffic
  Advisory" card fed by `api.trafficCurrent()` (real corridor data, not
  placeholders), between the accident and weather advisories.

## Testing

- Backend: `pytest tests/test_tomtom.py` (22 tests: success, 401/403/429
  fail-fast, timeout/5xx retry, key redaction, normalisation thresholds,
  cache/DB/synthetic fallback, router paths, config, DDL lock-step).
  Note: run per-file — the whole-suite run has a pre-existing native abort on
  macOS (see tests/README if any).
- Frontend: `npm test` (`web/src/lib/traffic.test.ts` — presentation helpers
  behind TrafficTile), `npx tsc -b`, `npm run build`.

## Deployment

```bash
docker compose build gateway web
docker compose up -d
docker logs jnpa-gateway --tail 50
curl -s localhost:8000/api/traffic/health | jq
curl -s localhost:8000/api/traffic/current | jq '.status, .source, .traffic.congestion_level'
# UI: Live Operations → TrafficTile; Driver Advisory → Traffic Advisory card
```

Apply the migration on a schema-v3 database:

```bash
psql "$DSN" -f infra/postgres/v3/0107_traffic_reading.sql
```
