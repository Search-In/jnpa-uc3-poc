# OpenWeatherMap Integration

Production-ready integration of the **OpenWeatherMap current-weather API** into
the UC-III backend, layered ON TOP of the existing Open-Meteo Weather + Marine
integration (see `docs/OPEN_METEO_INTEGRATION.md`). OpenWeather is **additive
and key-gated**: without `OPENWEATHER_API_KEY` the weather surface behaves
exactly as the original Open-Meteo-only build, and an OpenWeather outage
**never breaks the API** — only the `openweather` block degrades
(`LIVE → CACHED → SYNTHETIC`) while Open-Meteo data keeps flowing.

## Purpose

Additional weather intelligence for the operational dashboards:

* **Rain status** — mm over the last hour (`0` = not raining).
* **Weather condition** — human label (`Cloudy`, `Rain`, …) + raw description.
* **Humidity** — % relative humidity (not available from the Open-Meteo blocks).
* **Cloud coverage** — % cloud cover.
* **Temperature validation** — cross-provider check vs Open-Meteo
  (`temperature_delta`, `temperature_consistent` within a ±3 °C tolerance).
* **Weather label** — operational classification for dashboards:
  `CLEAR / CLOUDY / RAIN / STORM / SNOW / LOW_VISIBILITY`.

## Account creation & API key setup

1. Create a free account at <https://home.openweathermap.org/users/sign_up>.
2. Generate a key under *My API keys* (<https://home.openweathermap.org/api_keys>).
   New keys take ~10 minutes–2 hours to activate (401 until active).
3. The free tier (60 calls/min, 1M calls/month) is more than enough: the
   gateway polls at most once per UI refresh (120 s) per coordinate bucket and
   answers from its 600 s cache in between.
4. Set the key in the environment (see below). **The key is backend-only** —
   it is read by the gateway, sent only to `api.openweathermap.org`, and is
   never exposed as a `VITE_` variable, never called from the browser, and
   never echoed by `/api/weather/health`.

```
Browser ──> Gateway (/api/weather/current) ──> OpenWeatherClient ──> api.openweathermap.org
                                          └──> OpenMeteoClient  ──> api.open-meteo.com (+ marine)
```

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `OPENWEATHER_API_KEY` | *(empty — provider disabled)* | Backend-only credential. Empty ⇒ `openweather: null`, `sources.openweather: "DISABLED"`. |
| `OPENWEATHER_URL` | `https://api.openweathermap.org/data/2.5/weather` | Override only to route through a proxy. |
| `OPENWEATHER_TIMEOUT_S` | `5` | Per-attempt HTTP budget. |
| `OPENWEATHER_RETRIES` | `2` | Retries after the first try (timeouts / network / 5xx only). |

Configured in: `gateway/config.py` (`openweather_api_key` / `openweather_url`,
falling back to `jnpa_shared` settings), `docker-compose.yml` (gateway
environment), `.env.local.example`, `.env.aws.example`. The same key already
powers the ANPR ingest's coarse weather tagger
(`ingest/anpr/src/anpr_ingest/weather.py`) — one credential, two consumers.

## Architecture

Mirrors `integrations/openmeteo` exactly — no duplicate weather architecture:

| Layer | Path | Responsibility |
|---|---|---|
| Integration | `integrations/openweather/client.py` | The ONLY layer speaking HTTP to OpenWeatherMap. Timeout, bounded retries with backoff, typed failures, key never logged. |
| Integration | `integrations/openweather/schemas.py` | Pydantic validation + normalisation into the flat `openweather` block (wind converted m/s → km/h). |
| Integration | `integrations/openweather/exceptions.py` | `OpenWeatherError` hierarchy (`NotConfigured` / `Timeout` / `Unavailable` / `HTTPError` / `InvalidResponse`). |
| Service | `services/weather/service.py` | The EXISTING `WeatherService`, extended: concurrent Open-Meteo + OpenWeather fetch, per-block fallback, temperature validation, cache write-back, persistence. |
| Repository | `services/weather/repository.py` | `core.weather_reading` — reused, extended with `humidity` / `clouds`. |
| Router | `gateway/routers/weather.py` | The EXISTING `/api/weather/*` surface — no new endpoint. |
| Schema | `infra/postgres/v3/0106_weather_openweather.sql` | **Additive-only** migration (`ADD COLUMN IF NOT EXISTS humidity, clouds`). Dev bootstrap mirror: `gateway/weather_ext.py`. |

## Endpoint details

`GET /api/weather/current` (unchanged route; `latitude` / `longitude` /
`forecast_hours` as before). Request sent upstream by the client:
`lat`, `lon`, `appid` (the key), `units=metric`.

Sample response with OpenWeather enabled and both providers LIVE:

```json
{
  "status": "LIVE",
  "source": "OPEN_METEO+OPENWEATHER",
  "decision_path": "LIVE",
  "location": { "latitude": 18.9489, "longitude": 72.9492 },
  "weather":  { "temperature": 29.8, "wind_speed": 15.4, "visibility": 8000.0,
                "precipitation": 0.2, "condition": "Overcast", "...": "..." },
  "marine":   { "wave_height": 1.2, "wave_period": 5.0, "...": "..." },
  "openweather": {
    "temperature": 30.4,
    "feels_like": 35.1,
    "humidity": 70,
    "pressure": 1004,
    "rain": 0.0,
    "clouds": 40,
    "condition": "Cloudy",
    "condition_id": 802,
    "description": "scattered clouds",
    "label": "CLOUDY",
    "wind_speed": 18.0,
    "wind_direction": 250,
    "visibility": 6000,
    "station": "Uran",
    "observed_at": "2026-07-28T09:00:00+00:00",
    "temperature_delta": 0.6,
    "temperature_consistent": true
  },
  "sources": { "weather": "LIVE", "marine": "LIVE", "openweather": "LIVE" },
  "cache_age_s": null,
  "units": { "humidity": "%", "clouds": "%", "rain": "mm", "...": "..." },
  "timestamp": "2026-07-28T09:01:00+00:00"
}
```

`GET /api/weather/health` now reports the combined posture:
`provider: "OPEN_METEO + OPENWEATHER"`, `providers: [...]`, and an
`openweather` sub-object (`configured`, `url`, `timeout_s`, `retries` — never
the key). `GET /api/weather/readings` returns the persisted history including
the new `humidity` / `clouds` columns and the full `openweather` payload block.

## Failure handling

Per-block fallback ladder, aggregated into `status`:

```
LIVE  ──►  CACHED (Redis 600 s ──► last core.weather_reading row)  ──►  SYNTHETIC
```

| Scenario | Result |
|---|---|
| Both providers LIVE | `status: LIVE`, `source: OPEN_METEO+OPENWEATHER` |
| OpenWeather down / 401 / 429 / timeout | `status: DEGRADED`; Open-Meteo blocks still LIVE; `openweather` served from cache, else synthetic (clearly `"synthetic": true`) — **the API never fails because OpenWeather is down** |
| No API key | `status` unaffected; `openweather: null`, `sources.openweather: "DISABLED"` |
| Everything down, nothing cached | `status: OFFLINE`, all blocks synthetic |

Client failure semantics: timeouts / network errors / 5xx retried with
exponential backoff (`OPENWEATHER_RETRIES`); 4xx — including **401 invalid
key** and **429 rate limit** — fail fast (retrying cannot help and burns the
free-tier budget). Only a fully-LIVE Open-Meteo answer is cached/persisted;
a LIVE OpenWeather block rides along, a stale one is never written back.

## Frontend surfaces (no new components)

| Screen | Component | Addition |
|---|---|---|
| Live Operations | `web/src/components/panels/WeatherTile.tsx` | Humidity / Rain / Cloud stat row, operational label chip, temperature-validation notice, per-provider source chips (`OPEN_METEO`, `OPENWEATHER`). |
| Driver Advisory | `web/src/screens/DriverAdvisory.tsx` | Condition (OpenWeather-preferred), Rain (OW last-hour), Humidity added; provenance badges kept. |
| UC3 Reports | `web/src/screens/reports/Uc3ReportTabs.tsx` | WEATHER row shows `Provider: OPEN_METEO + OPENWEATHER` from `/api/weather/health`. |

Shared pure helpers (unit-tested): `web/src/lib/weather.ts` +
`web/src/lib/weather.test.ts`.

## Testing

* Backend: `pytest tests/test_openweather.py tests/test_weather.py`
  — valid response, invalid key (401 fail-fast), timeout retry, rate limit
  (429 fail-fast), unavailable/5xx, invalid body, combined-LIVE service path,
  OW-down ⇒ Open-Meteo still returns, cached rung, disabled contract,
  DB-fallback replay, migration/ext DDL lock-step.
* Frontend: `cd web && npx vitest run src/lib/weather.test.ts`
  — OpenWeather field selection, disabled fallback, loading/error tone logic.

## Deployment

1. Apply migration `infra/postgres/v3/0106_weather_openweather.sql`
   (additive, idempotent; dev stacks with `JNPA_RUNTIME_DDL=1` self-apply via
   `gateway/weather_ext.py`).
2. Set `OPENWEATHER_API_KEY` in the environment (`.env.local` locally,
   deployment secrets in AWS — see `.env.aws.example`).
3. Rebuild + restart the gateway (`make up` locally — the compose file passes
   the new variables through).
4. Verify: `curl localhost:8000/api/weather/health` shows
   `"openweather": {"configured": true, ...}`; `/api/weather/current` carries
   the `openweather` block with `sources.openweather: "LIVE"`.
