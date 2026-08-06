-- ============================================================
-- 0124  JNPA Simulated Port-Data API — ingest machinery
-- (renumbered from a duplicate 0117 — 0117_gate_capture_evidence.sql keeps
--  the slot; the version ledger is keyed on the number, so duplicates abort
--  every migrate run with a checksum-drift error. Content unchanged.)
-- Additive only. Naming: singular, per architecture conventions.
--
-- Five tables for the live-API sync layer (services/jnpa_sync +
-- integrations/jnpa_portdata), the counterpart of the file-dump import
-- ledgers. The API (Reference v2.0, 31-Jul-2026) serves 13 data groups as
-- indexed records carrying a fileRef + sha256 checksum; files are fetched
-- separately. Sync is incremental (?since=<watermark>&order=asc + cursor
-- paging). The same logical bytes may also arrive via the manual file-dump
-- import path — dedup across BOTH channels rides on sha256.
--
--   core.api_sync_state      one row per data group: the incremental-read
--                            watermark (max publishedAt fully processed),
--                            the verbatim nextCursor for mid-page resume,
--                            and the last run outcome.
--   core.api_ingest_run      one row per sync run (scheduled / manual /
--                            backfill), success or failure — the audit
--                            trail behind /api/integrations/jnpa/runs and
--                            the D1-3 API-management evidence export.
--   core.api_record          one row per API record ever seen. record_id
--                            is UNIQUE: the ON CONFLICT DO NOTHING target
--                            that absorbs boundary re-reads (the API's
--                            exclusive-`since` over a NON-unique sort key
--                            can replay records at batch boundaries).
--                            checksum_sha256 joins the per-service upload
--                            ledgers' file_hash — a dump-loaded file is
--                            recognised BEFORE download and never fetched
--                            twice.
--   core.api_report_snapshot report groups (berthing-reports /
--                            daily-reports) return JSON with NO file and NO
--                            checksum — idempotency needs a natural key:
--                            (group, date, terminal, payload sha).
--                            Land-raw-then-map: the payload is stored
--                            verbatim BEFORE any mapping, because the item
--                            schema is undocumented upstream.
--   core.api_defect_log      runtime observations of API behaviour
--                            deviating from the published documents.
--                            JNPA's notice of 31-Jul-2026 REQUIRES observed
--                            defects to be reported — this table feeds the
--                            defect-report export.
--
-- No token/credential table: the 1-hour bearer lives in-process only
-- (integrations/jnpa_portdata caches and refreshes it); persisting it would
-- widen the secret surface for nothing.
-- ============================================================
BEGIN;

CREATE TABLE IF NOT EXISTS core.api_sync_state (
    group_slug      text PRIMARY KEY,           -- e.g. 'customs'
    watermark_ts    timestamptz,                -- max publishedAt fully processed
    last_cursor     text,                       -- verbatim nextCursor (opaque)
    last_run_id     bigint,
    last_status     text,                       -- OK | PARTIAL | ERROR | SKIPPED_STATIC
    updated_at      timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS core.api_ingest_run (
    id                       bigserial PRIMARY KEY,
    started_at               timestamptz NOT NULL DEFAULT now(),
    finished_at              timestamptz,
    trigger                  text NOT NULL,     -- SCHEDULED | MANUAL | BACKFILL | TEST
    group_slug               text,              -- NULL for a sync_all parent run
    status                   text NOT NULL DEFAULT 'RUNNING',
                                                -- RUNNING | OK | PARTIAL | ERROR | DRY_RUN
    api_mode                 text NOT NULL DEFAULT 'LIVE',  -- LIVE | SIM
    records_listed           integer NOT NULL DEFAULT 0,
    records_new              integer NOT NULL DEFAULT 0,
    records_duplicate        integer NOT NULL DEFAULT 0,    -- record_id conflicts
    files_downloaded         integer NOT NULL DEFAULT 0,
    files_304                integer NOT NULL DEFAULT 0,
    files_skipped_checksum   integer NOT NULL DEFAULT 0,    -- dump/API dedup hits
    bytes_downloaded         bigint  NOT NULL DEFAULT 0,
    request_count            integer NOT NULL DEFAULT 0,
    rate_limit_remaining_min integer,
    error                    text,              -- typed client error (redacted)
    detail                   jsonb NOT NULL DEFAULT '{}'::jsonb);

CREATE INDEX IF NOT EXISTS idx_api_ingest_run_started
    ON core.api_ingest_run (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_ingest_run_group
    ON core.api_ingest_run (group_slug, started_at DESC);

CREATE TABLE IF NOT EXISTS core.api_record (
    id              bigserial PRIMARY KEY,
    record_id       text NOT NULL,              -- API recordId (rec_...)
    group_slug      text NOT NULL,
    message_type    text,                       -- e.g. CHPOI03, gate-open-report
    message_name    text,
    published_at    timestamptz,                -- the watermark key (+05:30)
    container_count integer,
    vessel_call     text,                       -- VCN-shaped; validate before
                                                -- joining (known fill-forward
                                                -- artefact upstream)
    summary         text,
    file_ref        text,                       -- ref_... (NULL: no file)
    media_type      text,
    size_bytes      bigint,
    checksum_sha256 text,                       -- == file ETag (unquoted)
    stored_path     text,                       -- raw-store copy of the bytes
    source_channel  text NOT NULL DEFAULT 'API',
    routed_service  text,                       -- consumer that imported it
    routed_status   text,                       -- SUCCESS | PARTIAL |
                                                -- SKIPPED_DUPLICATE | REJECTED |
                                                -- UNROUTED | STATIC_SKIP
    routed_file_id  bigint,                     -- consumer's own ledger row
    ingest_run_id   bigint REFERENCES core.api_ingest_run(id),
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,  -- record as received
    fetched_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_api_record UNIQUE (record_id));

CREATE INDEX IF NOT EXISTS idx_api_record_group_pub
    ON core.api_record (group_slug, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_record_sha
    ON core.api_record (checksum_sha256);
CREATE INDEX IF NOT EXISTS idx_api_record_routed
    ON core.api_record (routed_status, group_slug);

CREATE TABLE IF NOT EXISTS core.api_report_snapshot (
    id             bigserial PRIMARY KEY,
    group_slug     text NOT NULL,               -- berthing-reports | daily-reports
    report_date    date,
    terminal       text,
    payload        jsonb NOT NULL,              -- envelope as received (raw)
    item_count     integer NOT NULL DEFAULT 0,
    payload_sha256 text NOT NULL,               -- canonical-JSON hash (dedup)
    mapped_status  text NOT NULL DEFAULT 'RAW_ONLY',
                                                -- RAW_ONLY | MAPPED | MAP_FAILED
    mapped_detail  jsonb NOT NULL DEFAULT '{}'::jsonb,
    ingest_run_id  bigint REFERENCES core.api_ingest_run(id),
    fetched_at     timestamptz NOT NULL DEFAULT now());

-- Null-safe natural key — re-polling the same (group, date, terminal) with
-- unchanged content is a no-op; changed content lands as a NEW snapshot row.
CREATE UNIQUE INDEX IF NOT EXISTS uq_api_report_snapshot
    ON core.api_report_snapshot (group_slug,
                                 COALESCE(report_date, 'epoch'::date),
                                 COALESCE(terminal, ''),
                                 payload_sha256);
CREATE INDEX IF NOT EXISTS idx_api_report_snapshot_date
    ON core.api_report_snapshot (group_slug, report_date DESC);

CREATE TABLE IF NOT EXISTS core.api_defect_log (
    id               bigserial PRIMARY KEY,
    defect_code      text NOT NULL,             -- D-codes from the static
                                                -- catalogue + RUNTIME_* codes
    endpoint         text,
    severity         text NOT NULL DEFAULT 'INFO',  -- INFO | WARN | ERROR
    description      text,
    request_summary  jsonb NOT NULL DEFAULT '{}'::jsonb,
    response_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_at      timestamptz NOT NULL DEFAULT now(),
    ingest_run_id    bigint REFERENCES core.api_ingest_run(id));

CREATE INDEX IF NOT EXISTS idx_api_defect_code
    ON core.api_defect_log (defect_code, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_defect_observed
    ON core.api_defect_log (observed_at DESC);

COMMIT;
