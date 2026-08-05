# ICEGATE Customs Adapter (UC-3 Customs · Phase 4)

Connects the existing `/gate-customs` dashboard to the **real** customs data (module 5)
for the **ICEGATE** source, via the **Adapter pattern**, behind a feature flag — with the
UI, `/api/gate-data` contract, and DTOs **unchanged**. e-Seal / Form-13 / Weighbridge remain
SIM (their real data is JNPA module 8, out of scope).

Milestone tag: **`UC3-CUSTOMS-PHASE4-PRODUCTION`**.

## Architecture

```
GateCustoms.tsx  (UNCHANGED)
      │  api.gateProviders()  ·  api.gateCaptures('ICEGATE')
      ▼
/api/gate-data/*   (UNCHANGED contract — gateway proxy, gateway/routers/gate_data.py)
      ▼
gate-data service (port 8350)
      │
      ├── ESEAL        → seed provider    (SIM)   ── untouched
      ├── FORM13       → seed provider    (SIM)   ── untouched
      ├── WEIGHBRIDGE  → seed provider    (SIM)   ── untouched
      └── ICEGATE      → CustomsAdapter   (LIVE)  ── flag-gated
                              │  atomic INSERT…SELECT (idempotent)
                              ▼
                        jnpa.gate_captures  ◄── /captures reads this (UNCHANGED GateCapture DTO)
                              ▲
                              │ transform
      jnpa.customs_igm_container / customs_ooc / v_customs_container_status  (module 5)
```

The adapter writes real ICEGATE rows into the **same** `jnpa.gate_captures` table the UI already
reads — so the read path, endpoints, and DTOs never change.

## Adapter flow (gate-data boot, `gate-data/app.py::_persist_dataset_once`)

```
boot
 └─ adapter enabled? (GATE_ICEGATE_ADAPTER=customs)
      ├─ YES → log "icegate_adapter_enabled"
      │        seed ESEAL/FORM13/WEIGHBRIDGE (SIM), SKIP synthetic ICEGATE
      │        customs_adapter.sync_icegate_captures(dsn)   ← atomic transform
      └─ NO  → log "icegate_adapter_disabled"
               customs_adapter.purge_live_icegate(dsn)      ← symmetric rollback
               seed ESEAL/FORM13/WEIGHBRIDGE + synthetic ICEGATE (SIM)
```

## Feature flag

| Setting | ICEGATE source | Provider badge |
|---|---|---|
| `GATE_ICEGATE_ADAPTER=` (unset — default) | seed simulator | **SIM** |
| `GATE_ICEGATE_ADAPTER=customs` | real customs tables | **LIVE** |

Accepted truthy values: `customs, 1, true, yes, on, live`. Plumbed in `docker-compose.yml`
(gate-data service) as `GATE_ICEGATE_ADAPTER: ${GATE_ICEGATE_ADAPTER:-}`.

## Rollback process (symmetric)

1. Clear the flag: `GATE_ICEGATE_ADAPTER=` (or remove it).
2. Restart **only** the gate-data service: `docker compose up -d --force-recreate gate-data`.
3. On boot, `purge_live_icegate()` deletes every `capture_type='ICEGATE' AND source_mode='live'`
   row (logged `icegate_rollback_executed`), then the seed restores synthetic ICEGATE (SIM).
4. Result: provider ICEGATE = SIM, **no stale LIVE rows**, no duplicates, synthetic rows restored.

No other service (gateway, frontend, DB) needs restarting.

## Database flow

```
IGM (CHPOI03)  ─┐
                ├─ customs_igm_vessel → customs_igm_cargo_line → customs_igm_container ─┐
OOC (CHPOI10)  ─┤     (LEFT JOIN v_customs_container_status → ooc_cleared / rms_selected)│
RMS (.txt)     ─┘     (LEFT JOIN LATERAL customs_ooc_container → bill_of_entry_no)       │
                                                                                        ▼
                                          jnpa.gate_captures (capture_type='ICEGATE', source_mode='live')
```

Transform (customs → GateCapture):

| GateCapture field | Source |
|---|---|
| `container_no` | `customs_igm_container.container_no` |
| `status` / `payload.leo_status` / `payload.leo_granted` | OOC exists → `GRANTED` else `PENDING` (`v_customs_container_status.ooc_cleared`) |
| `payload.assessment` | RMS selected → `ASSESSED` else `FACILITATED` (`rms_selected`) |
| `payload.shipping_bill_no` | OOC Bill-of-Entry number (when present) |
| `payload.igm_no` | `customs_igm_container.igm_no` |
| `captured_at` | `COALESCE(entry_inward, expected_arrival, sent_ts, created_at)` (never NULL) |

Duplicate-proofing: `UNIQUE(container_no, capture_type, captured_at)` on `jnpa.gate_captures`.

## API compatibility

| Endpoint | Compatible | Breaking | Notes |
|---|---|---|---|
| `GET /providers` | ✅ | None | LIVE adds only additive `adapter:'customs'` on ICEGATE; `mode` flips to `live`. Other sources identical. |
| `GET /captures` | ✅ | None | Same code path both modes; `GateCapture` DTO identical (10 keys, types, nullable, `count/captures` envelope, `created_at DESC`, `limit`). Only ICEGATE row content changes. |
| `GET /reconciliations`, `/customs/history`, `/leo/*`, `/records/{cn}` | ✅ | None | Untouched. |

**No breaking changes — additive only.**

## Production deployment steps

1. Apply migration once (or rely on boot bootstrap): `psql "$DSN" -f infra/postgres/migrations/0031_customs.sql`.
2. Import the official customer files: `python scripts/import_customs.py` (idempotent). Optionally `--reconcile`.
3. Enable the adapter: set `GATE_ICEGATE_ADAPTER=customs` in the environment for the gate-data service.
4. Recreate gate-data: `docker compose up -d --force-recreate gate-data`.
5. Verify: `GET /api/gate-data/providers` → ICEGATE `mode:live`; `GET /api/gate-data/captures?type=ICEGATE&limit=20` → real records.
6. Open `/gate-customs` — ICEGATE badge LIVE, ICEGATE table shows real containers.

## Operational runbook

- **Refresh ICEGATE after a new customs import:** re-run `scripts/import_customs.py`, then restart gate-data (boot re-syncs) — or call `sync_icegate_captures(dsn)`. Idempotent.
- **Check provider modes:** `curl -s localhost:8350/providers` (or via gateway `/api/gate-data/providers`).
- **Row counts:** `SELECT capture_type, source_mode, count(*) FROM jnpa.gate_captures GROUP BY 1,2;`
- **Key log events (gate-data):** `icegate_adapter_enabled` / `icegate_adapter_disabled`,
  `customs_adapter_synced{rows_synchronized,rows_skipped,duration_ms}`,
  `customs_adapter_rollback{rows_removed}`, `icegate_rollback_executed`,
  `customs_adapter_sync_failed`.
- **Performance envelope:** fresh sync ≈ 0.6–2.6 s for ~4,200 rows (background, non-blocking);
  idempotent re-sync < 0.5 s; read `/captures?type=ICEGATE` ≈ 5 ms (index scan).

## Troubleshooting guide

| Symptom | Likely cause | Action |
|---|---|---|
| ICEGATE badge shows SIM | Flag not set / not truthy | Set `GATE_ICEGATE_ADAPTER=customs`, recreate gate-data |
| ICEGATE table empty | Customs data not imported | Run `scripts/import_customs.py`; confirm `SELECT count(*) FROM jnpa.customs_igm_container` |
| ICEGATE shows old data | Sync not re-run after import | Restart gate-data (boot re-syncs) |
| `customs_adapter_sync_failed` in logs | DB unreachable / schema missing | Check DSN; `ensure_customs_schema` runs at gateway boot; transaction already rolled back (ICEGATE keeps prior rows) |
| Stale LIVE rows after disabling | gate-data not restarted after clearing flag | Restart gate-data — `purge_live_icegate` runs on boot |
| Duplicate ICEGATE rows | Should be impossible | `UNIQUE(container_no,capture_type,captured_at)` enforces it; verify constraint present |

## Limitations (this phase)

- e-Seal / Form-13 / Weighbridge remain SIM (real data = JNPA **module 8**, out of scope).
- ICEGATE captures cover **IGM-declared containers**; OOC/RMS enrich only where the sample
  files' IGM numbers overlap (honest reflection of the provided data).
- Sync is a boot-time / on-demand batch (not streaming); re-run after new imports.

See also: [CUSTOMS_MODULE.md](CUSTOMS_MODULE.md) for the full module (parsers, importer, API, workflow).
