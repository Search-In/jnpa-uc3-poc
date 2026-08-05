# Shipping Lines Module (UC-II, module 4)

Real Import/Export Advance List (IAL/EAL) and Electronic Delivery Order (EDO/CODECO)
layer for JNPT, sourced ONLY from the official customer files under
`$SHIPPING_LINES_DATA_DIR` (default `~/Downloads/Digital Twin/Data/4-Shipping Lines/`).
Additive; touches no existing table. Mirrors the Customs module architecture.

## Source data (what was inspected)

Three business documents, exported per-terminal, in heterogeneous physical formats:

| List | Meaning | Terminals / files | Physical shape |
|---|---|---|---|
| **IAL** | Import Advance List | APMT, BMCT (CSV); NSFT (record-labelled CSV); NSICT, NSIGT (XLSX) | flat + record-labelled |
| **EAL** | Export Advance List | BMCT (CSV); GTI, NSFT (record-labelled CSV); NSICT, NSIGT (XLS) | flat + record-labelled |
| **EDO** | Electronic Delivery Order | EDO.xlsx | CODECO XML embedded in a cell |

Dry-run corpus size: **8,883 records** (EAL 5,743 · IAL 3,135 · EDO 5).

Three physical shapes handled:
1. **Flat tabular** — row 0 is the real header (EAL_BMCT, IAL APMT/BMCT, all .xls/.xlsx).
2. **Record-labelled "flat-file EDI"** — `HDRADVANCE` / `RecordLabel` / `CTR` rows in
   column 0; the real container headers live in the `RecordLabel` row (GTI, NSFT feeds).
3. **CODECO XML** — one `<CODECODetails>` document per cell in EDO.xlsx.

### Normalisation decisions (data-quality)
- **Weight unit is inferred by magnitude, not column name.** Several terminals
  mislabel a KG value as `GrossWeightInMT` (e.g. BMCT `19880`), while APMT's
  `GrossWeightInMT` is genuine tonnes (`20.95`). A laden box is ~2–40 t, so a value
  `< 200` is treated as MT (`×1000`), otherwise KG. `weight_source_uom` records the
  inferred unit; the raw value is preserved in `raw`.
- Heterogeneous headers resolved via an alias map (`parsers/column_maps.ALIASES`).
- `freight_kind` F/E → FULL/EMPTY; `category` I/E/T → IMPORT/EXPORT/TRANSHIP, defaulting
  from the list direction (EAL→EXPORT, IAL→IMPORT) when the row omits it.
- ISO / UN codes de-floated (`2210.0` → `2210`); `NIL`/`NA` sentinels → NULL.
- **Bill of Lading is sparse in the source** — present only in IAL NSICT (~22%) / NSIGT;
  BL lookup is therefore partial by nature of the data.

## Schema (migration `0032_shipping_lines.sql`)

Normalised, mirroring Customs quality — not one giant table:

- `jnpa.shipping_lines` — line-code master (upserted from distinct codes; never overwrites a name).
- `jnpa.sl_import_files` — import ledger / file envelope (sha256 dedup, per-file counts, status).
- `jnpa.sl_import_errors` — row-level errors.
- `jnpa.sl_advance_containers` — canonical IAL/EAL line items (FK → `shipping_lines`, `raw` jsonb).
- `jnpa.sl_delivery_orders` — EDO/CODECO delivery orders.
- `jnpa.sl_events` — append-only event log.
- `jnpa.v_shipping_line_container` — per-container rollup (soft, by-value join to `jnpa.cargo`).

The identical DDL is embedded in `gateway/shipping_lines_ext.py` (bootstrapped at gateway
boot) and kept in lock-step by `tests/test_shipping_lines_schema.py`.

## Importer (`scripts/import_shipping_lines.py`)

Idempotent, transaction-safe, re-import-safe, duplicate-safe — the Customs importer contract:
- Content dedup via `sl_import_files.source_sha256` UNIQUE → `SKIPPED_DUPLICATE`.
- Container rows de-dup on a **per-row content hash** (`sl_advance_containers.row_sha256` =
  SHA-256 of the full normalized source row): a byte-identical row collapses via
  `ON CONFLICT (import_file_id, row_sha256) DO NOTHING` (idempotent), but a row that
  differs in **any** source field persists as its own record — so normalization never
  drops a distinct source row (e.g. one container listed under two operator codes in the
  same list). **Never overwrites.**
- One file = one transaction (full rollback on error; a `FAILED` ledger row + `sl_import_errors`
  are recorded separately so failures are still audited).
- `--dry-run` parses/validates with no DB (and needs no DSN).

### Database target — RDS only
Reuses the project's existing `POSTGRES_DSN` (the same connection Cargo/Customs/CFS-ECY/
Performance use) — **no new DB config**. If `POSTGRES_DSN` is unset it is read from the
project's active `.env.local`. Before any write it runs a **preflight**: it prints
`host/port/db` (never the password) and **stops** unless the target is an
`*.rds.amazonaws.com` endpoint (localhost / `127.0.0.1` / a Docker `postgres` service /
port 5433 abort with a clear message; `--allow-non-rds` overrides). After import it runs
built-in validation (files, rows, duplicates, failures, line count, container count,
terminal-wise counts).

```bash
# dry-run (no DB)
.venv/bin/python scripts/import_shipping_lines.py --dry-run

# live import into RDS (POSTGRES_DSN from env or .env.local)
.venv/bin/python scripts/import_shipping_lines.py
```

## API (`/api/shipping-lines`, RBAC: CONTROL_ROOM + CUSTOMS)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/shipping-lines/summary` | dashboard counts (by list/terminal/line, totals) |
| GET | `/api/shipping-lines` | advance-list line items (filters: list_type, terminal, category, freight_kind, shipping_line, container, bl, q; paginated) |
| GET | `/api/shipping-lines/lines` | shipping-line master registry |
| GET | `/api/shipping-lines/delivery-orders` | EDO / CODECO delivery orders |
| GET | `/api/shipping-lines/messages[/{id}]` | import ledger (+ row errors) |
| GET | `/api/shipping-lines/events` | shipping-line event poll |
| GET | `/api/shipping-lines/container/{container_number}` | full view of one box (advance lists + delivery orders) |
| GET | `/api/shipping-lines/bl/{bill_of_lading}` | line items by Bill of Lading |
| GET | `/api/shipping-lines/{shipping_line}` | all shipments for one line code |
| POST | `/api/shipping-lines/import` | import `$SHIPPING_LINES_DATA_DIR` (idempotent) |

## Cargo integration (read-only enrichment)

Additive only — `jnpa.cargo` and the `CargoOut` DTO are unchanged. A new sub-resource
`GET /api/cargo/{container_number}/shipping-line` surfaces a container's IAL/EAL + EDO
facts (joined by value on `container_no` via `v_shipping_line_container`). 404 only if the
cargo record itself is unknown; if the box has no shipping-line document the `shipping_line`
block is `null` (an enrichment, never an error). No optional `0033` migration was added —
the `jnpa.cargo` table is not modified.

## Tests
- `tests/test_shipping_lines_schema.py` — migration ↔ ext DDL parity.
- `tests/test_shipping_lines_parsers.py` — normalisers + real-file end-to-end (skips if data dir absent).
- `tests/test_shipping_lines_api.py` — route surface, catch-all ordering, format detection, RBAC.
