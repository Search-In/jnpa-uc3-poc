-- 0032_shipping_lines.sql
-- Shipping Lines module (UC-II) — the real Import/Export Advance List (IAL/EAL)
-- and Electronic Delivery Order (EDO/CODECO) layer for JNPT, sourced ONLY from
-- the official customer files under
--   $SHIPPING_LINES_DATA_DIR (default ~/Downloads/Digital Twin/Data/4-Shipping Lines/).
--
-- Three business documents, exported per-terminal (APMT/BMCT/GTI/NSFT/NSICT/NSIGT),
-- in heterogeneous physical formats (flat CSV, record-labelled CSV, .xls, .xlsx,
-- and CODECO-XML embedded in an .xlsx cell). The importer normalises them into a
-- canonical container line-item plus a shipping-line master.
--
--   IAL  Import Advance List  -> sl_advance_containers (list_type='IAL')
--   EAL  Export Advance List  -> sl_advance_containers (list_type='EAL')
--   EDO  Electronic Delivery Order (CODECO XML) -> sl_delivery_orders
--
-- ADDITIVE & idempotent: every object is CREATE ... IF NOT EXISTS / CREATE OR
-- REPLACE VIEW, so re-running is a no-op. It DROPS/ALTERS nothing and touches no
-- existing table — shipping-line rows soft-link to jnpa.cargo BY VALUE
-- (container_no), never by FK. This file is the source of truth; the identical
-- DDL is embedded in gateway/shipping_lines_ext.py (asserted in lock-step by
-- tests/test_shipping_lines_schema.py) so a DB that never ran this migration
-- still gets the objects at gateway boot / importer run.

CREATE SCHEMA IF NOT EXISTS jnpa;

-- ---------------------------------------------------- shipping-line master registry
CREATE TABLE IF NOT EXISTS jnpa.shipping_lines (
    line_code   text PRIMARY KEY,
    line_name   text,
    source      text NOT NULL DEFAULT 'ADVANCE_LIST',
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now());

-- ---------------------------------------------------- import ledger / file envelope
CREATE TABLE IF NOT EXISTS jnpa.sl_import_files (
    id               bigserial PRIMARY KEY,
    list_type        text NOT NULL CHECK (list_type IN ('IAL','EAL','EDO')),
    terminal         text NOT NULL
                     CHECK (terminal IN ('APMT','BMCT','GTI','NSFT','NSICT','NSIGT','OTHER')),
    physical_format  text NOT NULL
                     CHECK (physical_format IN ('CSV','XLS','XLSX','CODECO_XML')),
    source_file      text NOT NULL,
    source_sha256    text NOT NULL,
    file_size_bytes  bigint,
    vessel_visit     text,
    voyage           text,
    line_code        text,
    direction        text,
    record_count     integer NOT NULL DEFAULT 0,
    imported_count   integer NOT NULL DEFAULT 0,
    error_count      integer NOT NULL DEFAULT 0,
    import_status    text NOT NULL DEFAULT 'PENDING'
                     CHECK (import_status IN ('PENDING','SUCCESS','PARTIAL','FAILED','SKIPPED_DUPLICATE')),
    error_detail     text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_sl_import_file_sha UNIQUE (source_sha256));
CREATE INDEX IF NOT EXISTS idx_sl_file_list ON jnpa.sl_import_files (list_type, id DESC);
CREATE INDEX IF NOT EXISTS idx_sl_file_term ON jnpa.sl_import_files (terminal, id DESC);
CREATE INDEX IF NOT EXISTS idx_sl_file_stat ON jnpa.sl_import_files (import_status, id DESC);

-- ---------------------------------------------------- import row-level errors
CREATE TABLE IF NOT EXISTS jnpa.sl_import_errors (
    id             bigserial PRIMARY KEY,
    import_file_id bigint NOT NULL REFERENCES jnpa.sl_import_files(id) ON DELETE CASCADE,
    record_ref     text,
    error_code     text NOT NULL,
    error_detail   text,
    created_at     timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS idx_sl_err_file ON jnpa.sl_import_errors (import_file_id, id);

-- ---------------------------------------------------- IAL/EAL canonical line items
CREATE TABLE IF NOT EXISTS jnpa.sl_advance_containers (
    id                  bigserial PRIMARY KEY,
    import_file_id      bigint NOT NULL REFERENCES jnpa.sl_import_files(id) ON DELETE CASCADE,
    list_type           text NOT NULL CHECK (list_type IN ('IAL','EAL')),
    terminal            text NOT NULL,
    container_no        text NOT NULL,
    iso_code            text,
    container_valid_iso boolean NOT NULL DEFAULT false,
    freight_kind        text NOT NULL DEFAULT 'UNKNOWN'
                        CHECK (freight_kind IN ('FULL','EMPTY','UNKNOWN')),
    category            text NOT NULL DEFAULT 'OTHER'
                        CHECK (category IN ('IMPORT','EXPORT','TRANSHIP','OTHER')),
    gross_weight_kg     numeric,
    weight_source_uom   text CHECK (weight_source_uom IN ('KG','MT')),
    pol                 text,
    pod                 text,
    destination         text,
    shipping_line_code  text REFERENCES jnpa.shipping_lines(line_code),
    vessel_visit        text,
    voyage              text,
    bill_of_lading      text,
    seal_no             text,
    reefer_status       text,
    reefer_temp         numeric,
    reefer_uom          text,
    imdg_code           text,
    un_number           text,
    group_code          text,
    client_code         text,
    departure_mode      text,
    nominated_cfs       text,
    iec_code            text,
    gst_no              text,
    commodity_code      text,
    raw                 jsonb NOT NULL DEFAULT '{}'::jsonb,
    row_sha256          text NOT NULL DEFAULT '',
    created_at          timestamptz NOT NULL DEFAULT now());
-- Content-hash uniqueness: a byte-identical source row collapses on re-import
-- (idempotent / duplicate-safe), but any row that differs in ANY source field
-- persists as its own record — so normalization never drops a distinct source row
-- (e.g. one container listed under two operator codes in the same advance list).
CREATE UNIQUE INDEX IF NOT EXISTS uq_sl_adv_container ON jnpa.sl_advance_containers
    (import_file_id, row_sha256);
CREATE INDEX IF NOT EXISTS idx_sl_adv_container_no ON jnpa.sl_advance_containers (container_no);
CREATE INDEX IF NOT EXISTS idx_sl_adv_bl   ON jnpa.sl_advance_containers (bill_of_lading);
CREATE INDEX IF NOT EXISTS idx_sl_adv_line ON jnpa.sl_advance_containers (shipping_line_code);
CREATE INDEX IF NOT EXISTS idx_sl_adv_term ON jnpa.sl_advance_containers (terminal, list_type);

-- ---------------------------------------------------- EDO / CODECO delivery orders
CREATE TABLE IF NOT EXISTS jnpa.sl_delivery_orders (
    id                  bigserial PRIMARY KEY,
    import_file_id      bigint NOT NULL REFERENCES jnpa.sl_import_files(id) ON DELETE CASCADE,
    document_number     text,
    common_ref_number   text,
    message_type        text,
    sender_id           text,
    receiving_party     text,
    vcn                 text,
    imo_number          text,
    call_sign           text,
    stuff_destuff_flag  text,
    shipping_agent_code text,
    vessel_country      text,
    total_containers    integer,
    container_no        text NOT NULL,
    iso_code            text,
    container_valid_iso boolean NOT NULL DEFAULT false,
    equipment_status    text,
    cargo_type          text,
    loading_port        text,
    dest_port           text,
    final_pod           text,
    arrival_ts          timestamptz,
    receipt_date        date,
    delivery_mode       text,
    gate_pass_no        text,
    gate_pass_ts        timestamptz,
    vehicle_no          text,
    gate_number         text,
    ca_code             text,
    con_seal_status     text,
    issued_ts           timestamptz,
    raw_xml             text,
    created_at          timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX IF NOT EXISTS uq_sl_delivery_order ON jnpa.sl_delivery_orders
    (COALESCE(common_ref_number, ''), container_no, COALESCE(gate_pass_no, ''));
CREATE INDEX IF NOT EXISTS idx_sl_do_container ON jnpa.sl_delivery_orders (container_no);
CREATE INDEX IF NOT EXISTS idx_sl_do_gatepass  ON jnpa.sl_delivery_orders (gate_pass_no);
CREATE INDEX IF NOT EXISTS idx_sl_do_vehicle   ON jnpa.sl_delivery_orders (vehicle_no);

-- ---------------------------------------------------- append-only event log
CREATE TABLE IF NOT EXISTS jnpa.sl_events (
    id           bigserial PRIMARY KEY,
    event        text NOT NULL,
    module       text,
    reference    text,
    container_no text,
    payload      jsonb,
    created_at   timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS idx_sl_events_mod  ON jnpa.sl_events (module, id DESC);
CREATE INDEX IF NOT EXISTS idx_sl_events_cont ON jnpa.sl_events (container_no);

-- ---------------------------------------------------- per-container rollup view
CREATE OR REPLACE VIEW jnpa.v_shipping_line_container AS
    WITH ac AS (
        SELECT DISTINCT ON (container_no)
            container_no, list_type, terminal, shipping_line_code, category,
            freight_kind, gross_weight_kg, weight_source_uom, pol, pod, destination,
            bill_of_lading, vessel_visit, voyage, iso_code, seal_no, reefer_status,
            reefer_temp, id
        FROM jnpa.sl_advance_containers
        ORDER BY container_no, id DESC
    ),
    edo AS (
        SELECT DISTINCT ON (container_no)
            container_no, gate_pass_no, gate_pass_ts, vehicle_no, delivery_mode,
            shipping_agent_code, equipment_status, loading_port, dest_port, final_pod, id
        FROM jnpa.sl_delivery_orders
        ORDER BY container_no, id DESC
    )
    SELECT
        COALESCE(ac.container_no, edo.container_no)      AS container_no,
        ac.list_type, ac.terminal, ac.shipping_line_code, ac.category,
        ac.freight_kind, ac.gross_weight_kg, ac.weight_source_uom, ac.pol, ac.pod,
        ac.destination, ac.bill_of_lading, ac.vessel_visit, ac.voyage, ac.iso_code,
        ac.seal_no, ac.reefer_status, ac.reefer_temp,
        edo.gate_pass_no, edo.gate_pass_ts, edo.vehicle_no, edo.delivery_mode,
        edo.shipping_agent_code, edo.equipment_status,
        (ac.container_no IS NOT NULL) AS in_advance_list,
        (edo.container_no IS NOT NULL) AS has_delivery_order
    FROM ac
    FULL OUTER JOIN edo ON edo.container_no = ac.container_no;
