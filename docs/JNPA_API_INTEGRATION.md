# JNPA Simulated Port-Data API — Integration Runbook

**What this is.** The live counterpart of the sample-data-pack corpus. JNPA's
API (`dt.jnpa.in/poc-api-data-access`, Reference v2.0) serves the 13 data
groups as indexed records + downloadable files; this integration polls it and
routes every file into the **same** upload services the manual dump-import
path uses. Built once, in `jnpa-uc3-poc` (the shared backend) — poc_1 (UC-1)
and PoC_2 (UC-2) inherit live data through gateway endpoints they already
call.

**Why it's safe to run alongside the dump.** Every upload service dedups on
sha256 through its import ledger. The sync layer additionally checks a
checksum against all ledgers *before downloading* — so a file already loaded
from the July dump is recognised and never re-fetched, and the same logical
record delivered twice (dump + API) is imported once. This is the
"Data Integration & Fallback" story: file dump and live API are two sources of
one truth, reconciled by content hash.

---

## Architecture at a glance

```
dt.jnpa.in ──token──> integrations/jnpa_portdata (client)
                            │  since=watermark-1s, order=asc, cursor paging
                            ▼
              services/jnpa_sync (poller)
        ┌───────────────────┼───────────────────────────┐
   api_record (dedup)   raw file store            api_ingest_run / api_defect_log
   ON CONFLICT          {sha}__{filename}          (audit + evidence)
        │                   │
        │        checksum already in a dump ledger? ──yes──> skip download
        ▼                   ▼
   routing.py ──> the existing upload services (marine / customs / shipping /
                  cfs_ecy / gate_documents / transport / rail) — sha ledger,
                  row errors, SKIPPED_DUPLICATE, all reused
                            │
                     core.* domain tables ──> gateway routers ──> all 3 PoCs
```

## Configuration (backend-only; the key is a credential — never commit, never `VITE_`)

| Env var | Default | Meaning |
|---|---|---|
| `JNPA_PORTDATA_API_URL` | client default (`dt.jnpa.in/poc-api-data-access`) | Base URL; point at the sim for offline work. A trailing `/v2` is stripped automatically (spec defect D1). |
| `JNPA_PORTDATA_CLIENT_KEY` | — | Issued client key. **Empty ⇒ sync never runs; health reports DISABLED.** |
| `JNPA_API_MODE` | `LIVE` | `LIVE` \| `SIM` — labels runs and evidence. |
| `JNPA_SYNC_ENABLED` | `true` | Scheduler gate (a key is still required). |
| `JNPA_SYNC_INTERVAL_S` | `300` | Seconds between scheduled `sync_all` passes. |
| `JNPA_STORE_DIR` | `data/jnpa_api` | Where downloaded raw bytes are kept (re-parse, PoC-2 source, evidence). |

Our registered key is derived from `binsunnt@keltron.org`. Keep it out of git
and Nextcloud; supply it via the environment or a secrets manager.

## Group → consumer map

| Group | Delivery | Consumer |
|---|---|---|
| `nlp-marine`, `port-craft-pilot` | indexed | `MarineUploadService` (registry auto-detects doc type) |
| `customs` | indexed | `CustomsService.import_bytes` |
| `shipping-lines` | indexed | `ShippingLinesUploadService` (IAL/EAL/EDO from filename) |
| `cfs-ecy` | indexed | `CfsEcyUploadService` (CFS/ECY from filename) |
| `gate-documents` | indexed | `GateDocumentService` (EIR/PIN/FORM13; tabular only) |
| `transport` | indexed | `TransportersDriversUploadService` (TRANSPORTER/DRIVER) |
| `rail-fois`, `rail-form11-icd` | indexed | `services/rail` (Phase 4) |
| `edi-messages` | indexed | UNROUTED — landed + stored, awaiting an EDI consumer |
| `berthing-reports`, `daily-reports` | report (JSON) | `services/jnpa_sync/report_ingest` (land-raw-then-map) |
| `bathymetry` | static | Not served by the API — the sample-pack dump is the source |

Anything UNROUTED is still **landed** (`api_record`) and its bytes **stored**;
`POST /api/integrations/jnpa/replay {group}` re-routes it from the store — no
re-download — the moment a consumer exists.

## Endpoints

| Route | Purpose |
|---|---|
| `GET /api/integrations/jnpa/health` | Mode, per-group watermarks, last run (poc_1 status card reads this) |
| `POST /api/integrations/jnpa/sync` | Manual trigger — `{group?, dry_run?}` (CONTROL_ROOM/ADMIN) |
| `POST /api/integrations/jnpa/replay` | Re-route UNROUTED records — `{group}` |
| `GET /api/integrations/jnpa/runs` | Ingest-run audit trail |
| `GET /api/integrations/jnpa/records` | Landed records (filter by group / routed_status) |
| `GET /api/integrations/jnpa/defects` | Runtime defect log — `?format=json\|md` |
| `GET /api/jnpa/files` · `/{sha256}` | Stored raw files (PoC_2's live source) |

---

## Running offline (now — the endpoint is port-filtered)

The live endpoint is unreachable from our networks (TCP 443/80 filtered;
reported to `dtinfo@jnport.gov.in`). Everything is exercised against the
contract-faithful simulator instead.

```bash
# 1. start the sim, seeded from the real dump
JNPA_SIM_DATA_DIR="/Users/aniketchopade/Downloads/Digital Twin - Updated/Data" \
  PYTHONPATH=ingest:shared .venv/bin/python -m jnpa_portdata_sim.app     # :8500
#    or: docker compose up jnpa-sim   (via docker-compose.override.yml)

# 2. point the gateway at the sim + a key the sim accepts
export JNPA_PORTDATA_API_URL=http://localhost:8500
export JNPA_PORTDATA_CLIENT_KEY=$(python "Data/15-API Access/keygen.py" sim@keltron.test | awk 'NR==3{print $3}')
export JNPA_API_MODE=SIM

# 3. trigger a sync and inspect
curl -X POST localhost:8000/api/integrations/jnpa/sync -d '{}' -H 'content-type: application/json'
curl localhost:8000/api/integrations/jnpa/runs | jq '.items[0]'
curl 'localhost:8000/api/integrations/jnpa/defects?format=md'
```

**Dual-source proof (screenshot-able):** run a dump CLI importer for a group,
then sync that group — the run shows `files_skipped_checksum ≈ 100%` and
`files_downloaded = 0`. Same bytes, one import.

## Live cutover (when JNPA opens the port)

1. Confirm `binsunnt@keltron.org` is registered in JNPA's allowlist (email
   `dtinfo@jnport.gov.in`).
2. Set `JNPA_PORTDATA_API_URL` (unset ⇒ official default), `JNPA_PORTDATA_CLIENT_KEY`
   (our derived key), `JNPA_API_MODE=LIVE`.
3. **Dry run first:** `POST /api/integrations/jnpa/sync {"group":"shipping-lines","dry_run":true}`
   — lists records, mutates nothing.
4. Review `GET /api/integrations/jnpa/defects?format=md` — the client
   auto-detects deviations beyond the 45 catalogued ones; each is a reportable
   observation (feeds the JNPA defect report).
5. Real sync of one small group; verify checksum-skips against dump-loaded
   data and target-table row counts.
6. Enable the scheduler (`JNPA_SYNC_ENABLED=true`) for periodic `sync_all`.
7. Regenerate evidence: `python scripts/jnpa_evidence.py --dsn "$POSTGRES_DSN"`.

## Troubleshooting

- **health `mode: DISABLED`** — no client key set. The sync loop is off by
  design; reads still work against whatever is already in the tables.
- **Connection timeout, host pings** — the known port filter (live endpoint).
  Use the sim, or the office/registered network.
- **`bad_cursor` mid-sync** — handled: the sync restarts the group once from
  the watermark; recordId dedup absorbs the re-read.
- **A group stuck UNROUTED** — expected for `edi-messages` (no consumer yet).
  `replay` it after wiring one; nothing is lost.
