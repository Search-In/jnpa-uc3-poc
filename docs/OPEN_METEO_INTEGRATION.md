# Open-Meteo Weather + Marine Integration

Production-ready integration of the free **Open-Meteo Weather** and **Open-Meteo
Marine** forecast APIs into the UC-III backend. No account, no API key — both
endpoints are public. An Open-Meteo outage **never breaks the API**: the surface
degrades `LIVE → CACHED → SYNTHETIC` and reports which rung served the request.

## Purpose

Port operations (berthing, gate flow, corridor traffic) are weather-sensitive.
This module gives every stakeholder screen one authoritative, always-available
source for current conditions at the JNPA / Mumbai Port area:

* **Weather** — temperature, wind speed / direction / gusts, visibility,
  precipitation, WMO weather condition.
* **Marine** — wave height, wave period, swell wave height, sea level height.

## Architecture

Follows the existing module layering (mirrors `services.shipping_lines` /
`services.fastag`):

| Layer | Path | Responsibility |
|---|---|---|
| Integration | `integrations/openmeteo/client.py` | The ONLY layer speaking HTTP to Open-Meteo. Timeouts, bounded retries with backoff, typed failures. |
| Integration | `integrations/openmeteo/schemas.py` | Pydantic validation of raw responses + normalisation into flat blocks. |
| Integration | `integrations/openmeteo/exceptions.py` | `OpenMeteoError` hierarchy (`Timeout` / `Unavailable` / `HTTPError` / `InvalidResponse`). |
| Service | `services/weather/service.py` | `WeatherService` — concurrent weather+marine fetch, combine, normalise, source metadata, fallback orchestration, cache write-back, persistence. |
| Repository | `services/weather/repository.py` | Raw-SQL speaker for `core.weather_reading` (audit trail + DB fallback rung). |
| Router | `gateway/routers/weather.py` | `/api/weather/*` REST surface. |
| Schema | `infra/postgres/v3/0105_weather_reading.sql` | Migration (additive, idempotent). Dev bootstrap mirror: `gateway/weather_ext.py` (gated on `JNPA_RUNTIME_DDL=1`). |

## Endpoints

### `GET /api/weather/current`

Query parameters (all optional):

| Param | Default | Notes |
|---|---|---|
| `latitude` | configured `port_lat` (18.9489) | `-90..90` |
| `longitude` | configured `port_lon` (72.9492) | `-180..180` |
| `forecast_hours` | `0` | `0..48`; include N hours of hourly forecast |

Sample response:

```json
{
  "status": "LIVE",
  "source": "OPEN_METEO",
  "decision_path": "LIVE",
  "location": { "latitude": 18.9489, "longitude": 72.9492 },
  "weather": {
    "temperature": 30.1,
    "wind_speed": 15.4,
    "wind_direction": 240,
    "wind_gusts": 28.8,
    "visibility": 8000,
    "precipitation": 0.2,
    "weather_code": 80,
    "condition": "Slight rain showers",
    "observed_at": "2026-07-27T10:00"
  },
  "marine": {
    "wave_height": 1.2,
    "wave_period": 5.0,
    "swell_wave_height": 0.9,
    "sea_level_height": 0.6,
    "observed_at": "2026-07-27T10:00"
  },
  "sources": { "weather": "LIVE", "marine": "LIVE" },
  "cache_age_s": null,
  "units": { "temperature": "°C", "wind_speed": "km/h", "visibility": "m",
             "wave_height": "m", "wave_period": "s", "precipitation": "mm" },
  "timestamp": "2026-07-27T10:02:11.512345+00:00"
}
```

Units are Open-Meteo defaults: °C, km/h, degrees, metres, mm, seconds.

### `GET /api/weather/readings`

Persisted reading history (newest first), standard `limit`/`offset` paging plus
optional `latitude`/`longitude` scoping (coordinate-bucketed at 2 dp ≈ 1.1 km).
Returns the usual `{items, total, limit, offset, count}` page envelope with an
`X-Total-Count` header.

### `GET /api/weather/health`

Integration posture: effective endpoint URLs, timeout/retry budget, cache TTL
and the configured default location. `configured` is always `true` — the API is
public and keyless.

RBAC: no dedicated policy entry — weather is operational data, visible to any
authenticated stakeholder (same posture as traffic / kpi).

## Failure handling

Per data block (weather and marine degrade independently):

```
LIVE        fresh Open-Meteo fetch (weather + marine concurrently)
  ↓ on OpenMeteoError (timeout / network / 5xx after retries / 4xx / bad body)
CACHED      last good combined answer from Redis (jnpa:cache:weather:{lat}:{lon},
            TTL GATEWAY_CACHE_TTL_WEATHER_S, default 600 s)
  ↓ on Redis miss
CACHED      last persisted core.weather_reading row for the coordinate
  ↓ on empty table / DB down
SYNTHETIC   deterministic port-area conditions, tagged "synthetic": true
```

The response always says what happened:

* `status` — `LIVE` (all fresh) / `DEGRADED` (any fallback rung fired) /
  `OFFLINE` (both blocks synthetic).
* `source` — `OPEN_METEO` / `OPEN_METEO_CACHE` / `SYNTHETIC` (worst rung).
* `sources` — the per-block rung, `cache_age_s` — staleness when cached.

Client behaviour: timeouts, network errors and 5xx are retried
(`OPEN_METEO_RETRIES`, exponential backoff); 4xx fail fast (a rejected request
cannot succeed on retry). A fully-LIVE answer is written back to Redis and
appended to `core.weather_reading` — both best-effort, an infra blip never
fails the request being served.

## Configuration

All env-driven, all optional (defaults shown; see `docker-compose.yml`):

| Variable | Default | Purpose |
|---|---|---|
| `OPEN_METEO_WEATHER_URL` | `https://api.open-meteo.com/v1/forecast` | Weather endpoint (set to proxy / self-hosted) |
| `OPEN_METEO_MARINE_URL` | `https://marine-api.open-meteo.com/v1/marine` | Marine endpoint |
| `OPEN_METEO_TIMEOUT_S` | `5` | Per-attempt HTTP budget |
| `OPEN_METEO_RETRIES` | `2` | Retries after the first attempt |
| `GATEWAY_CACHE_TTL_WEATHER_S` | `600` | Redis CACHED-rung TTL |
| `PORT_LAT` / `PORT_LON` | `18.9489` / `72.9492` | Default JNPA coordinates (`jnpa_shared` settings) |

No secrets required.

## Database

`core.weather_reading` (v3 migration `0105_weather_reading.sql`, additive —
touches no existing table): one row per successful LIVE fetch with the
normalised scalar fields (`temperature`, `wind_speed`, `wind_direction`,
`visibility`, `precipitation`, `wave_height`, `wave_period`, `source`,
`created_at`) plus the full combined blocks in `payload jsonb` so the DB
fallback rung loses nothing.

Dev databases that never ran the migration get the table lazily from
`gateway/weather_ext.ensure_weather_schema` when `JNPA_RUNTIME_DDL=1`
(schema-v3 posture: DDL is owned by the `infra/postgres/v3` migrations).

## Local testing

```bash
# 1. Unit tests (no DB / no network — Open-Meteo is mocked)
.venv/bin/python -m pytest tests/test_weather.py -q

# 2. Apply the migration to the local stack (port 5433 mapping)
psql "postgresql://postgres:jnpa_pw@localhost:5433/postgres" \
     -f infra/postgres/v3/0105_weather_reading.sql

# 3. Bring the stack up and hit the surface
make up
curl -s "http://localhost:8000/api/weather/current" | jq .
curl -s "http://localhost:8000/api/weather/current?latitude=19.0&longitude=72.9&forecast_hours=6" | jq .status
curl -s "http://localhost:8000/api/weather/health" | jq .
curl -s "http://localhost:8000/api/weather/readings?limit=5" | jq .total

# 4. Watch the fallback fire: cut the network (or set OPEN_METEO_WEATHER_URL to
#    an unreachable host), call /current twice — first answer is CACHED
#    (status DEGRADED), and after the Redis TTL + an empty table it floors at
#    SYNTHETIC (status OFFLINE). The API never 5xxes for an upstream outage.
```
