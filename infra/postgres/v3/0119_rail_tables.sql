-- ============================================================
-- 0119  Rail consumers — FOIS + Form 11 / CTO ingest machinery
-- Additive only. Naming: singular, per architecture conventions.
--
-- The file-backed rail groups of the JNPA Port-Data API (rail-fois and
-- rail-form11-icd) had no consumer, so the sync engine landed their records
-- as routed_status='UNROUTED'. This migration adds the consumer's tables —
-- the counterpart of the CFS-ECY upload ledger (migration 0101 /
-- core.cfs_ecy_import_file) — so services/rail can import the bytes and the
-- sync engine's replay_unrouted flips those records to SUCCESS.
--
--   core.rail_import_file      one row per rail file ever imported (FOIS CSV,
--                              Form 11 XLSX, CTO TXT). source_sha256 is
--                              UNIQUE — re-delivery of identical bytes (dump
--                              OR API) is a no-op (SKIPPED_DUPLICATE), the
--                              same idempotency the other ledgers give.
--                              `feed` distinguishes FOIS | FORM11 | CTO.
--   core.rail_import_error     per-row / per-file validation errors, FK to
--                              the ledger row (mirror of cfs_ecy_import_error).
--   core.fois_train_intimation NLDS/FOIS Train Intimation rows: one scheduled
--                              rake arrival (RakeId + ETA/ETD + reporting
--                              chain). Null-safe natural key
--                              (rake_id, ETA) → re-polling the same schedule
--                              snapshot is a no-op.
--   core.form11_entry          Form 11 pre-advice manifest: one export
--                              container per terminal manifest (BMCT / NSICT /
--                              NSIGT — the terminal is recovered from the
--                              filename). Two header variants collapse to one
--                              alias-mapped row; the rest is kept verbatim in
--                              `extra`.
--   core.cto_manifest_entry    CTO (Container Train Operator) rail manifest:
--                              one wagon/container line. cto_code is recovered
--                              from the filename (R261076 HTPL.txt → R261076);
--                              two positional layouts (dated-first vs
--                              rake-first) are both handled.
--
-- ICD daily-report PDFs are OUT OF SCOPE: the consumer records them as a
-- REJECTED ledger row (reason UNSUPPORTED_FORMAT) — never parsed, never
-- crashed on.
-- ============================================================
BEGIN;

CREATE TABLE IF NOT EXISTS core.rail_import_file (
    id              bigserial PRIMARY KEY,
    feed            text NOT NULL,              -- FOIS | FORM11 | CTO
    physical_format text NOT NULL,              -- CSV | TXT | XLS | XLSX | XLSM | PDF | OTHER
    source_file     text,
    source_sha256   text,
    file_size_bytes bigint,
    record_count    integer NOT NULL DEFAULT 0,
    imported_count  integer NOT NULL DEFAULT 0,
    error_count     integer NOT NULL DEFAULT 0,
    duplicate_count integer NOT NULL DEFAULT 0,
    import_status   text NOT NULL DEFAULT 'PENDING',
                                                -- PENDING | SUCCESS | PARTIAL |
                                                -- FAILED | SKIPPED_DUPLICATE | REJECTED
    error_detail    text,
    uploaded_by     text,
    source          text NOT NULL DEFAULT 'API',   -- API | DIRECTORY | UPLOAD
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT rail_import_file_feed_check
        CHECK (feed = ANY (ARRAY['FOIS'::text, 'FORM11'::text, 'CTO'::text])),
    CONSTRAINT rail_import_file_status_check
        CHECK (import_status = ANY (ARRAY['PENDING'::text, 'SUCCESS'::text,
               'PARTIAL'::text, 'FAILED'::text, 'SKIPPED_DUPLICATE'::text,
               'REJECTED'::text])),
    CONSTRAINT uq_rail_import_file_sha UNIQUE (source_sha256));

CREATE INDEX IF NOT EXISTS idx_rail_import_file_feed
    ON core.rail_import_file (feed, id DESC);
CREATE INDEX IF NOT EXISTS idx_rail_import_file_status
    ON core.rail_import_file (import_status, id DESC);

CREATE TABLE IF NOT EXISTS core.rail_import_error (
    id             bigserial PRIMARY KEY,
    import_file_id bigint NOT NULL REFERENCES core.rail_import_file(id),
    record_ref     text,
    error_code     text NOT NULL,
    error_detail   text,
    created_at     timestamptz NOT NULL DEFAULT now());

CREATE INDEX IF NOT EXISTS idx_rail_import_error_file
    ON core.rail_import_error (import_file_id, id);

CREATE TABLE IF NOT EXISTS core.fois_train_intimation (
    id                      bigserial PRIMARY KEY,
    import_file_id          bigint REFERENCES core.rail_import_file(id),
    rake_id                 text NOT NULL,        -- RakeId (train identity)
    rake_name               text,
    units                   integer,              -- wagon/unit count
    station_from            text,
    station_to              text,
    zone_from               text,
    zone_to                 text,
    last_reporting_station  text,
    last_reporting_division text,
    last_reporting_zone     text,
    loaded_empty_flag       text,                 -- L | E
    eda                     timestamptz,          -- estimated date of arrival
    edd                     timestamptz,          -- estimated date of departure
    last_status_time        timestamptz,
    source_file             text,
    extra                   jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at              timestamptz NOT NULL DEFAULT now());

-- Null-safe natural key: the same scheduled rake (RakeId, ETA) re-appearing
-- across consecutive daily snapshots is a no-op (ON CONFLICT DO NOTHING).
CREATE UNIQUE INDEX IF NOT EXISTS uq_fois_train_intimation
    ON core.fois_train_intimation
       (rake_id, COALESCE(eda, 'epoch'::timestamptz));
CREATE INDEX IF NOT EXISTS idx_fois_train_intimation_eda
    ON core.fois_train_intimation (eda DESC);

CREATE TABLE IF NOT EXISTS core.form11_entry (
    id             bigserial PRIMARY KEY,
    import_file_id bigint REFERENCES core.rail_import_file(id),
    terminal       text NOT NULL,                 -- BMCT | NSICT | NSIGT (from filename)
    container_no   text NOT NULL,
    iso_code       text,
    box_size       text,
    booking_number text,                          -- liner booking number
    gross_weight   numeric,
    pod            text,                          -- port of discharge
    line_code      text,                          -- box operator / line
    icd_location   text,
    via            text,
    status         text,
    iso_valid      boolean,
    source_file    text,
    extra          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now());

CREATE UNIQUE INDEX IF NOT EXISTS uq_form11_entry
    ON core.form11_entry
       (terminal, container_no, COALESCE(booking_number, ''));
CREATE INDEX IF NOT EXISTS idx_form11_entry_container
    ON core.form11_entry (container_no);

CREATE TABLE IF NOT EXISTS core.cto_manifest_entry (
    id             bigserial PRIMARY KEY,
    import_file_id bigint REFERENCES core.rail_import_file(id),
    cto_code       text NOT NULL,                 -- R261076 (from filename)
    rake_no        text,                          -- in-content rake indicator
    rake_id        text,                          -- explicit rake id (variant B)
    seq            integer,
    wagon_no       text NOT NULL,
    container_no   text,                          -- NULL for an empty wagon
    is_empty       boolean NOT NULL DEFAULT false,
    box_size       text,
    load_empty     text,                          -- L | E
    line_code      text,
    weight         numeric,
    pol            text,
    pod            text,
    from_station   text,
    terminal       text,
    booking_ref    text,
    event_ts       timestamptz,
    iso_valid      boolean,
    source_file    text,
    extra          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now());

CREATE UNIQUE INDEX IF NOT EXISTS uq_cto_manifest_entry
    ON core.cto_manifest_entry
       (cto_code, wagon_no, COALESCE(container_no, ''));
CREATE INDEX IF NOT EXISTS idx_cto_manifest_entry_container
    ON core.cto_manifest_entry (container_no);

COMMIT;
