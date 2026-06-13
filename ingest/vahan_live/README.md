# vahan-live — live Surepass-backed Vahan / Sarathi / FASTag adapter

A FastAPI service (port **8202**) exposing the **same surface** as the simulator
([`ingest/vahan_sim`](../vahan_sim/)) but proxying to Surepass's commercial KYC
API. This is the optional "live" path; JNPA will facilitate production Parivahan
credentials post-award, and Surepass acts as the commercial proxy meanwhile.

## Upstream endpoints

| Lookup | Surepass endpoint                                                  |
| ------ | ------------------------------------------------------------------ |
| RC     | `POST https://kyc-api.surepass.io/api/v1/rc/rc-full`               |
| DL     | `POST https://kyc-api.surepass.io/api/v1/driving-license/driving-license` |
| FASTag | `POST https://kyc-api.surepass.io/api/v1/fastag/fastag-search`     |

Authenticated with `SUREPASS_API_TOKEN` (`Authorization: Bearer <token>`).

## Gating

If `SUREPASS_API_TOKEN` is **missing or empty**, every endpoint returns:

```
HTTP/1.1 503 Service Unavailable
{"error": "live_disabled"}
```

This layer **does not** fall back to the simulator — that is the fallback
orchestrator's job (Prompt 4), which reads `jnpa.services` to choose between the
`sim` and `live` rows.

## Endpoints

`GET /vahan/rc/{plate}`, `GET /sarathi/dl/{dl_number}`,
`GET /fastag/balance/{plate}`, `POST /admin/seed` (no-op; 503 when disabled),
`GET /healthz`.

Surepass payloads are normalized into the shared `VahanRecord` / `SarathiRecord`
/ `FastagPing` schemas by [`mappers.py`](./mappers.py) (tolerant of the field-
name variants across Surepass products). Successful RC lookups are written back
to `jnpa.vehicle_master` (`provisional=false`) exactly like the simulator.

## Verify

```bash
# Without a token (default) -> 503:
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8202/vahan/rc/MH04AB1234   # 503
# With SUREPASS_API_TOKEN set in .env.local, the same call proxies to Surepass.
```
