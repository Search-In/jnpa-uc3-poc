# WorldTides Integration — `/api/weather` tide block

WorldTides (https://www.worldtides.info) supplies authoritative tide heights
and high/low extremes for the JNPA approaches. It slots into the EXISTING
weather surface as an **additive, key-gated `tide` block** — no new endpoints,
no breaking change; a deployment without the key behaves exactly as before
(status/source aggregation excludes the provider, mirroring OpenWeatherMap's
DISABLED precedent).

## Degradation ladder (per the WS3 integration plan §2.7)

```
WORLDTIDES  (LIVE — heights + extremes, datum=MSL)
    ↓
OPEN_METEO_MARINE  (live sea_level_height_msl; extremes/state degrade to null)
    ↓
CACHED  (Redis jnpa:cache:weather:{lat}:{lon}, then the last
         core.weather_reading payload — only ever a previously-LIVE block)
    ↓
ANALYTIC  (deterministic M2 model, amplitude 1.7 m about MSL,
           clearly tagged synthetic:true — never presented as observation)
```

All rungs are MSL-relative so heights are directly comparable across rungs.
`sources.tide` in the response names the rung that served the block
(`LIVE | OPEN_METEO_MARINE | CACHED | ANALYTIC`); the block's own
`tide_source` field names the provider
(`WORLDTIDES | OPEN_METEO_MARINE | WORLDTIDES_CACHE | ANALYTIC`).

## Configuration (backend-only; never a VITE_ variable)

| Variable               | Default                              | Meaning |
| ---------------------- | ------------------------------------ | ------- |
| `WORLDTIDES_API_KEY`   | *(empty = provider disabled)*        | pay-per-call credits key |
| `WORLDTIDES_BASE_URL`  | `https://www.worldtides.info/api/v3` | override only to proxy |
| `WORLDTIDES_TIMEOUT_S` | `5`                                  | per-attempt budget |
| `WORLDTIDES_RETRIES`   | `2`                                  | retries after the first try (timeout/network/5xx only; 4xx fail fast — retrying burns credits) |

One LIVE call fetches `heights` + `extremes` for 2 days (`datum=MSL`) — 2
credits. With the default 600 s response cache the steady-state spend is ≤ 288
credits/day per coordinate bucket.

## Response fields added (additive only)

`GET /api/weather/current` gains `tide` and `sources.tide`:

```json
"tide": {
  "tide_height": 1.24,
  "next_high_tide": {"time": "2026-07-30T17:42:00+00:00", "height": 2.31},
  "next_low_tide":  {"time": "2026-07-30T23:55:00+00:00", "height": -1.87},
  "tide_state": "RISING",
  "station": "Mumbai (Bombay)",
  "datum": "MSL",
  "observed_at": "2026-07-30T14:30:00+00:00",
  "tide_source": "WORLDTIDES",
  "fetched_at": "2026-07-30T14:33:12.481+00:00",
  "synthetic": false
}
```

Persistence: migration `infra/postgres/v3/0110_weather_tide.sql` adds
`core.weather_reading.tide_height` / `tide_state` (additive `ADD COLUMN IF NOT
EXISTS`; the full block rides in `payload` for the DATABASE fallback rung).
`gateway/weather_ext.py` mirrors it for `JNPA_RUNTIME_DDL=1` dev databases;
`tests/test_worldtides.py` asserts the lock-step.

## Deployment

1. Put the key in `.env.local` (`WORLDTIDES_API_KEY=…`) — already scaffolded.
2. Apply migration 0110 (RDS/schema-v3) or rely on runtime DDL in dev.
3. `make up` (or `docker compose --env-file .env.local up -d gateway`).
4. Verify: `curl :8000/api/weather/health` → `worldtides.configured: true`,
   then `curl :8000/api/weather/current | jq .tide` → `tide_source:
   "WORLDTIDES"`. Pull the key and the same call must keep answering with
   `tide_source: "OPEN_METEO_MARINE"` (never a 5xx) — that walk-down is the
   demo evidence.
