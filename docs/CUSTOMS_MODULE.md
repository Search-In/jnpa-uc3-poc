# Customs Module (Module 5) — IGM / OOC / SMTP / RMS / LEO / Shipping Bill

The real Indian-Customs / ICEGATE document layer for JNPT, built **entirely from
the official JNPA customer files** — nothing is synthesised. It ingests the six
customer modules, normalises them into a relational schema, exposes them over a REST
API, and binds them to the physical container lifecycle (`jnpa.cargo`).

> Source of truth: `$CUSTOMS_DATA_DIR` (default `~/Downloads/Digital Twin/data/5- Customs/`).
> The importer reads ONLY these files. There is no fake/generated customs data.

## Customer files → formats

| Module | Customer format | Files (sample) | Rows (sample) |
|---|---|---|---|
| **IGM** — Import General Manifest | `CHPOI03` **XML** (up to 5.5 MB) | 4 | 4 357 containers / 1 597 lines |
| **OOC** — Out-Of-Charge / Bill-of-Entry | `CHPOI10` **XML** | 4 | 4 BoE / 11 items |
| **SMTP** — Sub-Manifest Transhipment Permit | `CHPOI13` **XML** | 6 | 209 transhipment lines |
| **RMS** — Container Scanning selection | plain **`.txt`** | 4 | 98 selected containers |
| **LEO** — Let Export Order | **`.xlsx`** | 1 | 100 rows |
| **Shipping Bill** — export declaration | **`.xlsx`** | 1 | 100 rows → 15 distinct SBs |

## Architecture (reuses the existing repo conventions)

```
scripts/import_customs.py                     CLI importer (env CUSTOMS_DATA_DIR)
        │
services/customs/
  parsers/  chpoi03 chpoi10 chpoi13           pure XML/txt/xlsx → typed dict decoders
            rms_txt leo_xlsx sb_xlsx          (no I/O, no DB, deterministic, unit-tested)
  repository.py   CustomsRepository           only SQL speaker; bulk idempotent insert
  service.py      CustomsService              import orchestration, events, cargo binding
        │
gateway/routers/customs.py   /api/customs     thin REST router (DTOs, pagination, RBAC)
gateway/customs_ext.py       ensure_customs_schema()   boot-time idempotent DDL
infra/postgres/migrations/0031_customs.sql    normalized schema (15 tables + 1 view)
```

- **Layering** mirrors `services/cargo` and `services/cfs_ecy` (router → service → repository).
- **Schema bootstrap** mirrors `cfs_ecy_ext`/`uc3_ext`: `ensure_customs_schema()` runs at
  gateway boot (`main.py` lifespan) so a dev DB that never ran the migration still gets
  the objects. `tests/test_customs_schema.py` guards migration↔`_DDL` from drifting.
- **Soft-links to `jnpa.cargo` BY VALUE** (`container_no`), never by FK — the same
  cross-domain convention as CFS-ECY (migration 0027). Purely additive: it drops/alters
  nothing existing.

## Database (migration 0031)

Normalised, fully constrained (PK / FK / UNIQUE / CHECK / indexes). Key tables:

- `customs_messages` — import ledger + **idempotency anchor** (`source_sha256` UNIQUE).
- IGM: `customs_igm_vessel` → `customs_igm_cargo_line` → `customs_igm_container`.
- OOC: `customs_ooc` → `customs_ooc_container` → `customs_ooc_item`.
- SMTP: `customs_smtp` → `customs_smtp_line`.
- RMS: `customs_rms_scanlist` → `customs_rms_container`.
- `customs_shipping_bill`, `customs_leo`, `customs_events`, `customs_import_errors`.
- View `v_customs_container_status` — per-container flags (`declared_igm`, `rms_selected`,
  `ooc_cleared`, `smtp_bonded`) — the single by-value join binding customs to `jnpa.cargo`.

## Import (idempotent, atomic, bulk)

```bash
# dry-run: parse + validate every file, no DB writes
.venv/bin/python scripts/import_customs.py --dry-run

# live import (ensures schema first)
POSTGRES_DSN='postgresql+asyncpg://postgres:...@localhost:5433/postgres' \
    .venv/bin/python scripts/import_customs.py

# import + bind to cargo lifecycle
.venv/bin/python scripts/import_customs.py --reconcile
```

Guarantees:
- **Idempotent** — content-hash dedup (`source_sha256`): re-importing unchanged bytes is a
  no-op (`SKIPPED_DUPLICATE`); every child row also upserts on its natural key.
- **Atomic per file** — the whole message (envelope + all child rows) commits in one
  transaction; any error rolls the entire file back and records a `FAILED` ledger row.
- **Bulk** — `executemany` + parent-id maps, so a 2 794-container IGM is a handful of
  statements. **Honest accounting** — the Shipping Bill sheet's 100 rows / 15 distinct SBs
  import as `record=100, imported=15` (duplicates collapse, never double-counted).

REST equivalent: `POST /api/customs/import` (idempotent), `POST /api/customs/reconcile`.

## API (`/api/customs`)

`GET /summary` · `GET /messages[/{id}]` · `GET /igm[/{igm_no}/containers]` ·
`GET /ooc | /smtp | /rms | /leo | /shipping-bills` · `GET /containers/{container_no}`
(full customs view + derived workflow stage) · `GET /events` ·
`POST /import` · `POST /reconcile`.

All list endpoints paginate (`limit`/`offset`) and set `X-Total-Count`. Filters are
whitelisted equality columns (injection-safe). **RBAC**: `/api/customs` is restricted to
control-room + `CUSTOMS` in `gateway/auth.py._POLICY` — the customs clearance audience.

## Workflow (customs → cargo binding)

`POST /api/customs/reconcile` (or `import_customs.py --reconcile`) drives
`jnpa.cargo.customs_status` from customs facts, using ONLY the existing enum values:

- **Out-Of-Charge issued → `CLEARED`** (import customs release) + `customs.cargo_cleared` event.
- **RMS-selected (not yet cleared) → `UNDER_INSPECTION`** (scanning hold) +
  `customs.cargo_scan_hold` event + a `CUSTOMS_SCAN_REQUIRED` notification on the **existing**
  `jnpa.cargo_notifications` feed (no new notification system).

Only containers already present in `jnpa.cargo` are touched; reconciliation is idempotent.

Tracks (per the tender): **Import** IGM → RMS → OOC → Release · **Export** Shipping Bill →
LEO (SB-keyed) · **Transhipment** SMTP → Bond → Destination. The per-container view derives
the import/transhipment stage (`MANIFESTED` → `SCAN_SELECTED` → `OUT_OF_CHARGE`; `BONDED`).

## Events

Emitted ONLY from real processing, into the append-only `jnpa.customs_events` (same pattern
as `jnpa.cargo_events`): `customs.igm_filed`, `customs.ooc_issued`, `customs.smtp_issued`,
`customs.rms_selected`, `customs.leo_granted`, `customs.shipping_bill_filed` (one per imported
file) and `customs.cargo_cleared` / `customs.cargo_scan_hold` (on reconciliation). Poll via
`GET /api/customs/events?since=<id>`.

## Tests

- `test_customs_parsers.py` — every parser vs the real files (exact counts).
- `test_customs_repository.py` — real-DB import (idempotency, honest counts, rollback, reconcile).
- `test_customs_service.py` — format detection, workflow derivation, discovery.
- `test_customs_api.py` — router logic (pagination, X-Total-Count, filters, 404s) via fake repo.
- `test_customs_schema.py` — migration ↔ boot-DDL drift guard.

DB/data-dependent tests skip automatically when Postgres (5433) or the customer files are absent.
