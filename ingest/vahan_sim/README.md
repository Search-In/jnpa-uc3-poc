# vahan-sim — Vahan / Sarathi / FASTag schema-faithful simulator

A FastAPI service (port **8201**) that mirrors the Parivahan **Vahan** (RC),
**Sarathi** (DL) and **FASTag** (NETC) schemas with a deterministic local
dataset, so the rest of the JNPA UC-III system is API-correct before production
credentials are provisioned. The live Surepass path lives in
[`ingest/vahan_live`](../vahan_live/).

## Endpoints

| Method | Path                       | Returns        |
| ------ | -------------------------- | -------------- |
| GET    | `/vahan/rc/{plate}`        | `VahanRecord`  |
| GET    | `/sarathi/dl/{dl_number}`  | `SarathiRecord`|
| GET    | `/fastag/balance/{plate}`  | `FastagPing`   |
| POST   | `/admin/seed`              | reseed + rewrite the demo fixture |
| GET    | `/healthz`                 | liveness       |
| GET    | `/metrics`                 | Prometheus exposition |

Schemas are defined once in `jnpa_shared.schemas` and reused by both services.

## Deterministic dataset

`seed.py` generates **25,000** distinct, regex-valid Indian plates across the
MH-04, MH-43, MH-06, GJ-01, KA-01, TN-22, KL-07 series plus a ~2% slice of new
BH-series plates. Everything (owners, validity dates, FASTag balances) is
derived from a fixed `SEED` and anchored to a fixed `REFERENCE_DATE`, so results
are identical across runs and hosts.

Anomaly distributions (verified at seed time):

| Anomaly              | Target | Actual |
| -------------------- | ------ | ------ |
| Expired fitness      | 8 %    | ~8.1 % |
| Blacklisted (RC)     | 3 %    | ~3.0 % |
| FASTag LOW_BALANCE   | 5 %    | ~5.0 % |
| FASTag BLACKLISTED   | 1 %    | ~1.0 % |

### Demo fixture

At startup and on every `POST /admin/seed`, the service writes
`./data/fixtures/known_plates.json` — the **50 plates** the demo script
(Prompt 9) queries: 25 guaranteed-benign and 25 carrying at least one issue
(expired / blacklisted / low-FASTag). Regenerate standalone with:

```bash
PYTHONPATH=ingest:shared python -m vahan_sim.seed --out data/fixtures/known_plates.json
```

## Behaviour notes

- **Artificial latency:** each lookup sleeps ~`100ms ± 50ms` (deterministic per
  key) to mimic Parivahan's real response times. Tunable via
  `VAHAN_LATENCY_MEAN_MS` / `VAHAN_LATENCY_JITTER_MS`.
- **Vehicle-master writeback:** every successful `/vahan/rc/*` upserts into
  `jnpa.vehicle_master` with `provisional=false`, `provisional_until=null` —
  the row the dashboard reads when showing "verified" trucks at the gate.
- **Service registry:** on startup the service upserts its row into
  `jnpa.services` (`name='vahan'`, `kind='sim'`) for the fallback orchestrator
  (Prompt 4) to discover.

## Verify

```bash
curl -s http://localhost:8201/vahan/rc/MH04AB1234 | jq .
curl -s http://localhost:8201/healthz | jq .
psql ... -c "select count(*) from jnpa.vehicle_master;"
```
