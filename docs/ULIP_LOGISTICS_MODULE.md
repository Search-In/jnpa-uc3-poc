# ULIP Logistics Intelligence (`/api/logistics/*`)

Additive UC-3 integration module in the same mould as the TomTom traffic and
OpenAQ air-quality modules: a typed HTTP client (`integrations/ulip`), a
service with a graceful fallback chain (`services/logistics`), a thin gateway
router (`gateway/routers/logistics.py`), an additive migration
(`infra/postgres/v3/0109_logistics_ulip.sql`), and a Live Operations tile
(`web/src/components/panels/LogisticsTile.tsx`).

Nothing existing was modified: the trucking-app ULIP GPS relay
(`/api/ulip/proxy`), the FASTag vertical (`/api/fastag/*`) and the LDB
adapter (`/api/ldb/*`) are untouched neighbours.

## What it does

ULIP (Unified Logistics Interface Platform, DPIIT — https://www.ulip.dpiit.gov.in/)
fronts the national logistics source systems behind one API gateway. This
module consumes two of its APIs, normalises the answers into one
cross-modal *logistics event* stream, and persists everything:

| ULIP API (env-overridable) | Reference | Normalised event |
|---|---|---|
| `FASTAG/01` (`ULIP_FASTAG_API`) | vehicle registration no. | `TOLL_CROSSING` (plaza, geocode, reader time) |
| `LDB/01` (`ULIP_LDB_API`) | ISO-6346 container no. | `CONTAINER_MOVEMENT` (event, location, time) |

`GET /api/logistics/tracking/{id}` classifies the reference automatically
(ISO-6346 pattern → container, else vehicle).

## Fallback chain

    LIVE      fresh ULIP fetch (audited to core.ulip_api_audit)
    CACHED    last good answer from Redis (jnpa:cache:ulip:*)
    DATABASE  persisted core.logistics_tracking / core.logistics_event rows
    FALLBACK  explicitly EMPTY answer (data_available: false), clearly tagged

Deliberately unlike the weather/traffic SYNTHETIC rungs: the logistics
surface **never fabricates shipment data** — a made-up toll crossing or
container movement would be operationally misleading. `status` is
`LIVE | DEGRADED | OFFLINE`, `source` is `ULIP | ULIP_CACHE | ULIP_DB | NONE`.

## Endpoints

    GET /api/logistics/health         ULIP posture (no credential material)
    GET /api/logistics/current        corridor summary (24 h volumes, latest
                                      events, tracked references)
    GET /api/logistics/tracking/{id}  per-reference tracking (full chain)
    GET /api/logistics/events         persisted history, paged
                                      (?ref_id=&event_type=&limit=&offset=)

RBAC: no dedicated `_POLICY` entry — like `/api/traffic`, visible to any
authenticated stakeholder when `AUTH_ENABLED=true`. OpenAPI docs appear on
`/docs` automatically.

## Authentication (backend-only, never in the browser)

Two modes, resolved by `UlipClient.auth_mode`:

* **login** — `ULIP_CLIENT_ID` + `ULIP_CLIENT_SECRET`: the client POSTs
  `{base}/user/login`, caches the issued bearer token for
  `ULIP_TOKEN_TTL_S` (default 1800 s), and forces exactly one re-login when
  an API call answers 401/403; a second rejection is a terminal
  `UlipAuthError`.
* **static** — a pre-issued `ULIP_API_KEY` (shared with the relay config) is
  sent as-is as the bearer; takes precedence when set.

No credential configured → the LIVE rung is skipped and the surfaces degrade;
credentials/tokens are redacted from every log line and exception message.

## Environment variables (compose defaults in `docker-compose.yml`)

    ULIP_API_URL=                 default https://www.ulip.dpiit.gov.in/ulip/v1.0.0
    ULIP_API_KEY=                 static bearer (alternative to the pair below)
    ULIP_CLIENT_ID=               login username
    ULIP_CLIENT_SECRET=           login password
    ULIP_TIMEOUT_S=5              per-attempt budget
    ULIP_RETRIES=2                retries after the first try (timeout/network/5xx)
    ULIP_TOKEN_TTL_S=1800         login-token reuse window
    ULIP_FASTAG_API=              default FASTAG/01
    ULIP_LDB_API=                 default LDB/01
    GATEWAY_CACHE_TTL_ULIP_S=300  Redis rung TTL

## Database (migration 0109, additive only)

* `core.logistics_event` — normalised events; null-safe dedup unique index on
  `(ref_type, ref_id, event_type, COALESCE(event_ts,'epoch'), COALESCE(location,''))`.
* `core.logistics_tracking` — one upserted snapshot per reference
  (`UNIQUE (ref_type, ref_id)`).
* `core.ulip_api_audit` — one row per outbound ULIP call, success or failure.

Apply with `psql -d jnpa_schema_v3 -f infra/postgres/v3/0109_logistics_ulip.sql`,
or let `gateway/logistics_ext.ensure_logistics_schema` create the objects at
boot when `JNPA_RUNTIME_DDL=1` (dev only). The `_DDL` list and the migration
are held in lock-step by `tests/test_ulip_logistics.py`.

## Frontend

`LogisticsTile` ("Logistics Intelligence" 🚛, ULIP source badge) sits in the
Live Operations capability grid beside Weather/Traffic/AirQuality. It polls
`/api/logistics/current` every 2 min and shows 24 h event/vehicle/container
volumes, the latest events, and the `STATUS • SOURCE` + decision-path
provenance chips. Pure render helpers live in `web/src/lib/logistics.ts`
(unit-tested in `logistics.test.ts` — the repo has no DOM test environment).

## Tests

    .venv/bin/python -m pytest tests/test_ulip_logistics.py   # 31 tests
    cd web && npx vitest run src/lib/logistics.test.ts        # 11 tests

Client success/timeout/retry/5xx/429/invalid-body, login + token cache +
single re-login, credential redaction, normalisation, all four service rungs
(including "FALLBACK is empty, never fabricated"), router paths,
existing-routes-untouched guard, config gating, migration lock-step.
