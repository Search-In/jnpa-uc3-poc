# jnpa_portdata_sim — contract-faithful JNPA Port-Data API simulator

Serves the exact surface of `https://dt.jnpa.in/poc-api-data-access` (JNPA
API Reference v2.0, 31-Jul-2026), seeded from the real sample-data-pack
`Data/` folder. The live endpoint sits behind a port filter (reported to
JNPA 04-Aug-2026); this sim makes the whole integration testable offline and
plug-and-play when the filter lifts — cut over by changing
`JNPA_PORTDATA_API_URL` alone.

## Surface

| Route | Notes |
|---|---|
| `GET /` | service description, no auth |
| `GET /v2/health` | liveness, no auth |
| `POST /v2/auth/token` | client key → 1-h bearer (keygen-derived keys accepted) |
| `GET /v2/groups` | 13-group catalogue, non-uniform per-group schema (as live) |
| `GET /v2/groups/{g}/records` | indexed records / report JSON / static-empty |
| `GET /v2/files/{fileRef}` | bytes + ETag + Content-Disposition; If-None-Match → 304 |
| `POST /admin/force-429` | **test-only**, not part of the live surface |

## Deliberate defect parity

With `JNPA_SIM_FAITHFUL=true` (default) the sim reproduces the live
service's catalogued defects (see `docs/JNPA_API_DEFECTS.md`) so client
defenses are exercised against reality:

- record ids are sequential integers under a base64 coat; `fileRef` is the
  `recordId` with the prefix swapped (D2)
- `nextCursor` **is** the last item's `fileRef` (shared namespace, D13)
- no secondary sort key; publishedAt ties exist, including at page
  boundaries (D13b — the last 4 records of every sizable group share one
  timestamp)
- report groups answer with the 5-field envelope: no `order`, no `matched`,
  no `hasMore`, no `nextCursor` (D9); item schema empty by default (D10)
- no `requestId` in any response (D3)
- `RateLimit-Remaining` only on 200s — never on errors or 304 (D5); 429
  carries no `Retry-After` (D6)
- the bad-client-key path is slowed ~250 ms and answers a vague 401
- the undocumented `admin:read` scope is granted (D0b)
- a raw `+05:30` in a query decodes to a space and earns `400 bad_parameter`
  — the server-side face of JNPA's own encoding bug (D22)

## Environment

| Var | Default | Meaning |
|---|---|---|
| `JNPA_SIM_DATA_DIR` | `data/jnpa_dump` | the sample-pack `Data/` folder |
| `JNPA_SIM_CLIENT_EMAILS` | `sim@keltron.test` | emails whose derived keys authenticate |
| `JNPA_SIM_EXTRA_KEYS` | — | literal extra client keys (tests) |
| `JNPA_SIM_TOKEN_TTL_S` | `3600` | bearer lifetime (shrink to test expiry) |
| `JNPA_SIM_FAITHFUL` | `true` | defect parity on/off |
| `JNPA_SIM_REPORT_ITEMS` | `empty` | `synthetic` serves a plausible (guessed) report-item shape for mapper development |
| `JNPA_SIM_FORCE_429` | `0` | arm N forced 429 answers at startup |
| `PORT` | `8500` | listen port |

## Run

```bash
PYTHONPATH=ingest:shared .venv/bin/python -m jnpa_portdata_sim.app
# or via compose: the jnpa-sim service in docker-compose.override.yml
```

Point the gateway at it with `JNPA_PORTDATA_API_URL=http://localhost:8500`
and any registered key, e.g. the one derived from `sim@keltron.test` by
`Data/15-API Access/keygen.py`.
