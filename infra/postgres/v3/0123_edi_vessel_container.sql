-- 0123  COARRI / COPRAR vessel-side container moves (edi-messages group)
--
-- The JNPA Port-Data API's edi-messages group delivers, besides CODECO gate
-- moves (already consumed), the vessel-side documents:
--   COARRI  <ContLoadingNDischargeOder>  container loading/discharge REPORT
--   COPRAR  <AdvContainerList>           advance container list (load/discharge ORDER)
-- Until now both landed raw and stayed UNROUTED. This migration adds the
-- additive consumer tables, mirroring the 0119 rail pattern: one import
-- ledger + one domain table, ON CONFLICT-deduped on a natural key,
-- provenance-tagged (data_origin, per 0120/0121 conventions).
--
-- Purely additive — touches no existing table.

BEGIN;

CREATE TABLE IF NOT EXISTS core.edi_import_file (
    id              bigserial PRIMARY KEY,
    feed            text NOT NULL,
    physical_format text NOT NULL DEFAULT 'XML',
    source_file     text,
    source_sha256   text,
    file_size_bytes bigint,
    record_count    integer NOT NULL DEFAULT 0,
    imported_count  integer NOT NULL DEFAULT 0,
    error_count     integer NOT NULL DEFAULT 0,
    duplicate_count integer NOT NULL DEFAULT 0,
    import_status   text NOT NULL DEFAULT 'PENDING',
    error_detail    text,
    uploaded_by     text,
    source          text NOT NULL DEFAULT 'API',
    data_origin     text NOT NULL DEFAULT 'MANUAL',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT edi_import_file_feed_check
        CHECK (feed = ANY (ARRAY['COARRI'::text, 'COPRAR'::text])),
    CONSTRAINT edi_import_file_status_check
        CHECK (import_status = ANY (ARRAY['PENDING'::text, 'SUCCESS'::text,
               'PARTIAL'::text, 'FAILED'::text, 'SKIPPED_DUPLICATE'::text,
               'REJECTED'::text])),
    CONSTRAINT ck_edi_import_file_data_origin
        CHECK (data_origin IN ('API', 'MANUAL')));

-- Identical bytes are kept once PER ORIGIN (LIVE sync vs manual import), the
-- 0120 convention.
CREATE UNIQUE INDEX IF NOT EXISTS uq_edi_import_file_sha_origin
    ON core.edi_import_file (source_sha256, data_origin);
CREATE INDEX IF NOT EXISTS idx_edi_import_file_feed
    ON core.edi_import_file (feed, id DESC);

CREATE TABLE IF NOT EXISTS core.edi_vessel_container (
    id              bigserial PRIMARY KEY,
    import_file_id  bigint REFERENCES core.edi_import_file(id),
    doc_type        text NOT NULL,           -- COARRI | COPRAR
    direction       text,                    -- LOAD | DISCHARGE (from filename)
    document_number text,
    common_ref      text,
    sender_id       text,
    vcn             text,                    -- vessel call number
    terminal_code   text,                    -- TOCode / TOOrDockCode
    line_code       text,                    -- ContLineCode / CACode
    agent_code      text,                    -- VesselAgentCode / SACode
    container_no    text NOT NULL,
    iso_code        text,
    iso_valid       boolean,
    equipment_status text,                   -- FCL/MTY (COARRI) or code (COPRAR)
    container_status text,
    seal_no         text,                    -- customs seal
    shipper_seal_no text,
    gross_weight    numeric,
    tare_weight     numeric,
    pol             text,
    pod             text,
    final_pod       text,
    igm_line        integer,
    igm_subline     integer,
    cargo_type      text,
    imo_class       text,
    icd_indicator   boolean,
    damage_indicator boolean,
    damage_desc     text,
    shipping_ts     timestamptz,             -- CShippingDateTime (IST source)
    landing_ts      timestamptz,             -- CLandDateTime
    berthing_ts     timestamptz,             -- BerthingDateTime
    rotation_no     text,
    rotation_date   date,
    source_file     text,
    extra           jsonb NOT NULL DEFAULT '{}'::jsonb,
    data_origin     text NOT NULL DEFAULT 'MANUAL',
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT edi_vessel_container_doc_check
        CHECK (doc_type = ANY (ARRAY['COARRI'::text, 'COPRAR'::text])),
    CONSTRAINT ck_edi_vessel_container_data_origin
        CHECK (data_origin IN ('API', 'MANUAL')));

-- One row per container per document (re-imports collide here, never overwrite).
CREATE UNIQUE INDEX IF NOT EXISTS uq_edi_vessel_container
    ON core.edi_vessel_container
       (doc_type, COALESCE(document_number, ''), container_no);
CREATE INDEX IF NOT EXISTS idx_edi_vessel_container_no
    ON core.edi_vessel_container (container_no);
CREATE INDEX IF NOT EXISTS idx_edi_vessel_container_vcn
    ON core.edi_vessel_container (vcn);
CREATE INDEX IF NOT EXISTS idx_edi_vessel_container_origin
    ON core.edi_vessel_container (data_origin);

COMMIT;
