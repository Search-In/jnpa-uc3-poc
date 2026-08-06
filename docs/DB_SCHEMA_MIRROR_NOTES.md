# DB Schema Mirror Notes — JNPA Port-Data API integration (migrations 0117–0119)

These are the schema deltas the API integration adds to the operational RDS
(`jnpa_schema_v3`, applied via `jnpa-uc3-poc/infra/postgres/v3/`). They are
recorded here so the **canonical schema document** at
`Desktop/JNPT_docs/Digital Twin/DB_Schema/schema.sql` can be hand-updated to
match — that file is the design-of-record and lives outside the repo, so it is
NOT edited automatically. Apply these notes to it when convenient.

All additions are **additive only** — no existing table, column or constraint
is altered. Every object is `IF NOT EXISTS`. The migrations are also embedded
as idempotent boot DDL in `services/jnpa_sync/repository.py`
(`ensure_api_ingest_schema`) and `services/rail/repository.py`
(`ensure_rail_schema`), the coexistence pattern the other modules use.

---

## 0117 — API ingest machinery (`infra/postgres/v3/0117_jnpa_api_ingest.sql`)

Five new `core.*` tables. This is the sync layer's own bookkeeping — the
counterpart of the per-service upload ledgers, for the API channel.

| Table | Purpose | Key columns / constraints |
|---|---|---|
| `core.api_sync_state` | Incremental-read watermark per group | PK `group_slug`; `watermark_ts` (max publishedAt fully processed), `last_cursor`, `last_status` |
| `core.api_ingest_run` | One row per sync run — the audit + evidence trail | `id bigserial`; `trigger`, `group_slug`, `status`, `api_mode` (LIVE\|SIM), counters (`records_listed/new/duplicate`, `files_downloaded/304/skipped_checksum`, `bytes_downloaded`, `request_count`, `rate_limit_remaining_min`), `error`, `detail jsonb` |
| `core.api_record` | Every API record ever seen | `id bigserial`; **`UNIQUE (record_id)`** (the boundary-tie dedup target); `group_slug`, `message_type`, `published_at`, `vessel_call`, `file_ref`, `checksum_sha256` (= file ETag; joins the upload ledgers' `file_hash`/`source_sha256`), `stored_path`, `source_channel`, `routed_service`, `routed_status`, `routed_file_id`, `payload jsonb`. Indexes on `(group_slug, published_at DESC)`, `(checksum_sha256)`, `(routed_status, group_slug)` |
| `core.api_report_snapshot` | Report-group JSON, landed raw before mapping | `id bigserial`; **`UNIQUE (group_slug, COALESCE(report_date,'epoch'), COALESCE(terminal,''), payload_sha256)`** (natural-key idempotency — reports carry no file/checksum); `payload jsonb`, `item_count`, `mapped_status` (RAW_ONLY\|MAPPED\|MAP_FAILED), `mapped_detail jsonb` |
| `core.api_defect_log` | Runtime deviations from the published interface | `id bigserial`; `defect_code`, `endpoint`, `severity`, `description`, `request_summary`/`response_summary jsonb`, `observed_at`, `ingest_run_id`. Index on `(defect_code, observed_at DESC)` |

**Rationale for the canonical doc:** the pre-existing schema was single-shot
file-dump-shaped — it had file-level provenance (`core.ingest_file.path`) but
no API-record table, no fileRef/checksum column, no `published_at`, no
watermark/cursor, and no ingest-run audit. 0117 supplies exactly those, and
`checksum_sha256` is the join that makes dump-loaded and API-loaded copies of
one file reconcile.

## 0118 — report-group natural keys (`infra/postgres/v3/0118_jnpa_natural_keys.sql`)

**Deliberate NO-OP — the migration number is reserved, no schema object is
created.** Nothing to mirror into the canonical doc. Rationale (recorded in the
migration header so the design decision survives):

- **Raw landing** is already keyed by 0117's `uq_api_report_snapshot`
  `(group_slug, COALESCE(report_date,'epoch'), COALESCE(terminal,''), payload_sha256)`
  — re-polling unchanged report content is an ON CONFLICT DO NOTHING no-op.
- **Mapped persistence performs no direct upsert of its own.** Mapped report
  rows are rendered to the existing CSV upload templates and fed through the
  same validated upload services the manual dump import uses
  (`berthing-reports` → `BerthingUploadService.import_file`; `daily-reports` →
  performance `UploadService.import_file("daily_status", …)`), which own their
  unique keys and file-hash ledgers already.

So report ingestion introduces no new key; the number is burned to keep the
sequence gap-free and record the decision.

## 0119 — rail tables (`infra/postgres/v3/0119_rail_tables.sql`)

Five new `core.*` tables — the consumer for the previously-UNROUTED
`rail-fois` / `rail-form11-icd` groups, on the CFS-ECY ledger mould.

| Table | Purpose | Key columns / constraints |
|---|---|---|
| `core.rail_import_file` | One row per rail file imported (FOIS CSV / Form 11 XLSX / CTO TXT) | `id bigserial`; **`UNIQUE (source_sha256)`** (dump/API dedup → SKIPPED_DUPLICATE); `feed` (FOIS\|FORM11\|CTO) + status CHECKs (adds REJECTED for unsupported PDFs); counters; `physical_format`, `source`, `uploaded_by` |
| `core.rail_import_error` | Per-row/per-file validation errors | `id`; `import_file_id` FK, `record_ref`, `error_code`, `error_detail` |
| `core.fois_train_intimation` | NLDS/FOIS Train Intimation rows (one scheduled rake arrival) | `rake_id`, `rake_name`, `units`, station/zone chain, `loaded_empty_flag`, `eda`/`edd`/`last_status_time`, `extra jsonb`. **`UNIQUE (rake_id, COALESCE(eda,'epoch'))`** — same schedule across daily snapshots is a no-op |
| `core.form11_entry` | Form 11 pre-advice manifest (one export container per terminal manifest) | `terminal` (from filename), `container_no`, `iso_code`, `box_size` (text — cells like "40 FT"), `booking_number`, `gross_weight`, `pod`, `line_code`, `extra jsonb`. **`UNIQUE (terminal, container_no, COALESCE(booking_number,''))`** |
| `core.cto_manifest_entry` | CTO rail manifest (one wagon/container line) | `cto_code` (from filename), `rake_no`/`rake_id`, `wagon_no`, `container_no` (NULL for empty wagon), `is_empty`, `load_empty`, weights/ports, `event_ts`, `extra jsonb`. **`UNIQUE (cto_code, wagon_no, COALESCE(container_no,''))`** |

ICD daily-report PDFs are out of scope — the consumer records them as a
REJECTED ledger row (reason `UNSUPPORTED_FORMAT`), never parsed. All unique
indexes are COALESCE-guarded expression indexes with matching
`ON CONFLICT … DO NOTHING` (the `api_report_snapshot` idiom). Boot DDL is
mirrored byte-identically in `services/rail/repository.py::ensure_rail_schema`.

---

## Also worth applying to the canonical doc (recommended, not forced)

The canonical schema has **no dedup key** on several fact tables that a
re-ingest (dump + API) could double-load — most consequentially
`core.container_event` (drives ECY TRT / trailer TAT marts). The API path
avoids this because it routes through the upload services' own ledgers, but
the underlying tables remain vulnerable to any *other* double-load. Adding
null-safe unique keys there (e.g. `container_event` on
`(container_no, event_ts, event_type, source_table)`) is a separate,
recommended hardening — noted here so it isn't forgotten, but out of scope for
this integration.
