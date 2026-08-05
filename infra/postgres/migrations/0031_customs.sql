-- 0031_customs.sql
-- Customs module (UC-II/UC-III) — the real Indian-Customs / ICEGATE document
-- layer for JNPT, sourced ONLY from official customer files under
--   $CUSTOMS_DATA_DIR (default ~/Downloads/Digital Twin/data/5- Customs/).
--
-- Six customer modules, three physical formats:
--   IGM           CHPOI03  XML   Import General Manifest  (vessel -> cargo lines -> containers)
--   OOC           CHPOI10  XML   Out-Of-Charge / Bill-of-Entry (boe -> containers -> invoice items)
--   SMTP          CHPOI13  XML   Sub-Manifest Transhipment Permit (bond movement, flat lines)
--   RMS           .txt           Container Scanning Division selection list (risk scanning)
--   Shipping Bill .xlsx          Export declaration
--   LEO           .xlsx          Let Export Order (export clearance)
--
-- PURELY ADDITIVE. Every statement is CREATE ... IF NOT EXISTS. It DROPS or ALTERS
-- nothing existing. It does NOT touch cargo / gate_captures / leo_reconciliation /
-- empty_container / vehicle / driver / transporter / auth tables. All customs rows
-- soft-link to jnpa.cargo BY VALUE (container_no, ISO-6346), never by FK — the same
-- cross-domain convention as migration 0027 (cfs_ecy) — so a container that has not
-- yet been created in jnpa.cargo never blocks a customs import.
--
-- Idempotency is enforced at TWO levels:
--   * message level  — jnpa.customs_messages.source_sha256 UNIQUE: re-importing a
--                      file whose bytes are unchanged is a no-op (SKIPPED_DUPLICATE).
--   * record level   — every child table carries a natural UNIQUE key, so a partial
--                      re-import upserts via ON CONFLICT instead of duplicating.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0031_customs.sql
-- Runtime-applied at gateway boot by gateway/customs_ext.ensure_customs_schema().

CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

-- ===========================================================================
-- Import ledger / EDI message envelope. One row per imported customer file.
-- This is the single source of truth for "what was imported, from where, when,
-- and did it succeed" — and the idempotency anchor (source_sha256 UNIQUE).
-- ===========================================================================
CREATE TABLE IF NOT EXISTS jnpa.customs_messages (
    id               bigserial PRIMARY KEY,
    message_type     text NOT NULL
                     CHECK (message_type IN ('CHPOI03','CHPOI10','CHPOI13','RMS','LEO','SHIPPING_BILL')),
    module           text NOT NULL
                     CHECK (module IN ('IGM','OOC','SMTP','RMS','LEO','SHIPPING_BILL')),
    control_number   text,                  -- EDI DocumentHeader.ControlNumber (XML only)
    sender_id        text,                  -- DocumentHeader.SenderId
    receiver_id      text,                  -- DocumentHeader.ReceiverId
    message_id_code  text,                  -- DocumentHeader.MessageId (e.g. CHPOI03C)
    sent_ts          timestamptz,           -- SentDate+SentTime (IST-parsed), XML only
    primary_ref      text,                  -- natural doc id: IGM_NO / BillOfEntryNo / SMTPNo / SB Number
    source_file      text NOT NULL,         -- basename of the source file
    source_sha256    text NOT NULL,         -- content hash: the idempotency key
    file_size_bytes  bigint,
    record_count     integer NOT NULL DEFAULT 0,   -- rows parsed from the file
    imported_count   integer NOT NULL DEFAULT 0,   -- rows actually persisted
    error_count      integer NOT NULL DEFAULT 0,
    import_status    text NOT NULL DEFAULT 'PENDING'
                     CHECK (import_status IN ('PENDING','SUCCESS','PARTIAL','FAILED','SKIPPED_DUPLICATE')),
    error_detail     text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_customs_message_sha UNIQUE (source_sha256)
);
CREATE INDEX IF NOT EXISTS idx_customs_msg_module    ON jnpa.customs_messages (module, id DESC);
CREATE INDEX IF NOT EXISTS idx_customs_msg_type      ON jnpa.customs_messages (message_type, id DESC);
CREATE INDEX IF NOT EXISTS idx_customs_msg_ref       ON jnpa.customs_messages (primary_ref);
CREATE INDEX IF NOT EXISTS idx_customs_msg_status    ON jnpa.customs_messages (import_status, id DESC);

-- Row-level import errors (a PARTIAL import records the rows it could not persist).
CREATE TABLE IF NOT EXISTS jnpa.customs_import_errors (
    id           bigserial PRIMARY KEY,
    message_id   bigint NOT NULL REFERENCES jnpa.customs_messages(id) ON DELETE CASCADE,
    record_ref   text,                    -- e.g. "line 313 / container EOLU8617280"
    error_code   text NOT NULL,
    error_detail text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_customs_import_err_msg ON jnpa.customs_import_errors (message_id, id);

-- ===========================================================================
-- IGM  (CHPOI03)  — vessel header -> cargo lines (1:N) -> containers (1:N)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS jnpa.customs_igm_vessel (
    id                     bigserial PRIMARY KEY,
    message_id             bigint NOT NULL REFERENCES jnpa.customs_messages(id) ON DELETE CASCADE,
    customs_house_code     text,
    igm_no                 text NOT NULL,
    igm_date               date,
    imo_code               text,
    vessel_code            text,
    voyage_no              text,
    shipping_line_code     text,
    shipping_agent_code    text,
    master_name            text,
    port_of_arrival        text,
    vessel_type            text,
    total_no_of_lines      integer,
    brief_cargo_desc       text,
    expected_arrival       timestamptz,
    entry_inward           timestamptz,
    terminal_operator_code text,
    created_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_igm_vessel UNIQUE (igm_no, igm_date)
);
CREATE INDEX IF NOT EXISTS idx_igm_vessel_msg  ON jnpa.customs_igm_vessel (message_id);
CREATE INDEX IF NOT EXISTS idx_igm_vessel_igm  ON jnpa.customs_igm_vessel (igm_no);

CREATE TABLE IF NOT EXISTS jnpa.customs_igm_cargo_line (
    id                  bigserial PRIMARY KEY,
    vessel_id           bigint NOT NULL REFERENCES jnpa.customs_igm_vessel(id) ON DELETE CASCADE,
    igm_no              text NOT NULL,      -- denormalised for cross-document join by value
    igm_date            date,
    line_no             integer NOT NULL,
    subline_no          integer NOT NULL DEFAULT 0,
    bl_no               text,
    bl_date             date,
    house_bl_no         text,
    house_bl_date       date,
    port_of_loading     text,
    port_of_destination text,
    port_of_discharge   text,
    importer_name       text,
    importer_address    text,
    importer_state      text,
    notified_party      text,
    nature_of_cargo     text,
    item_type           text,
    cargo_movement      text,
    no_of_packages      integer,
    type_of_packages    text,
    gross_weight        numeric,
    unit_of_weight      text,
    goods_description   text,
    mlo_code            text,
    be_regularised      text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_igm_line UNIQUE (vessel_id, line_no, subline_no)
);
CREATE INDEX IF NOT EXISTS idx_igm_line_vessel ON jnpa.customs_igm_cargo_line (vessel_id);
CREATE INDEX IF NOT EXISTS idx_igm_line_igm    ON jnpa.customs_igm_cargo_line (igm_no, line_no, subline_no);
CREATE INDEX IF NOT EXISTS idx_igm_line_bl     ON jnpa.customs_igm_cargo_line (bl_no);

CREATE TABLE IF NOT EXISTS jnpa.customs_igm_container (
    id                   bigserial PRIMARY KEY,
    cargo_line_id        bigint NOT NULL REFERENCES jnpa.customs_igm_cargo_line(id) ON DELETE CASCADE,
    igm_no               text NOT NULL,
    line_no              integer NOT NULL,
    subline_no           integer NOT NULL DEFAULT 0,
    container_no         text NOT NULL,     -- ISO-6346; soft-links to jnpa.cargo BY VALUE
    iso_valid            boolean NOT NULL DEFAULT true,
    seal_no              text,
    container_agent_code text,
    container_status     text,              -- FCL / LCL
    no_of_packages       integer,
    container_weight     numeric,
    iso_size_type        text,              -- ISOCode (e.g. 2200)
    soc_flag             text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_igm_container UNIQUE (cargo_line_id, container_no)
);
CREATE INDEX IF NOT EXISTS idx_igm_cont_line   ON jnpa.customs_igm_container (cargo_line_id);
CREATE INDEX IF NOT EXISTS idx_igm_cont_no     ON jnpa.customs_igm_container (container_no);
CREATE INDEX IF NOT EXISTS idx_igm_cont_igm    ON jnpa.customs_igm_container (igm_no, line_no);

-- ===========================================================================
-- OOC  (CHPOI10)  — Out-Of-Charge / Bill-of-Entry -> containers (1:N) -> items (1:N)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS jnpa.customs_ooc (
    id                      bigserial PRIMARY KEY,
    message_id              bigint NOT NULL REFERENCES jnpa.customs_messages(id) ON DELETE CASCADE,
    customs_house_code      text,
    igm_no                  text,
    igm_date                date,
    line_no                 integer,
    subline_no              integer NOT NULL DEFAULT 0,
    bill_of_entry_no        text NOT NULL,
    bill_of_entry_date      date,
    document_type           text,
    ie_code                 text,
    importer_name           text,
    importer_address        text,
    importer_city           text,
    pin_code                text,
    cha_code                text,
    out_of_charge_no        text,
    out_of_charge_date      date,
    out_of_charge_type      text,
    nature_of_cargo         text,
    quantity_out_of_charged numeric,
    unit_of_quantity        text,
    no_of_packages          integer,
    country_of_origin       text,
    assessable_value        numeric,
    cif_value               numeric,
    total_customs_duty      numeric,
    created_at              timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_ooc_boe UNIQUE (bill_of_entry_no, line_no, subline_no)
);
CREATE INDEX IF NOT EXISTS idx_ooc_msg  ON jnpa.customs_ooc (message_id);
CREATE INDEX IF NOT EXISTS idx_ooc_igm  ON jnpa.customs_ooc (igm_no, line_no);
CREATE INDEX IF NOT EXISTS idx_ooc_boe  ON jnpa.customs_ooc (bill_of_entry_no);
CREATE INDEX IF NOT EXISTS idx_ooc_ooc  ON jnpa.customs_ooc (out_of_charge_no);

CREATE TABLE IF NOT EXISTS jnpa.customs_ooc_container (
    id               bigserial PRIMARY KEY,
    ooc_id           bigint NOT NULL REFERENCES jnpa.customs_ooc(id) ON DELETE CASCADE,
    bill_of_entry_no text NOT NULL,
    container_no     text NOT NULL,         -- soft-links to jnpa.cargo BY VALUE
    iso_valid        boolean NOT NULL DEFAULT true,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_ooc_container UNIQUE (ooc_id, container_no)
);
CREATE INDEX IF NOT EXISTS idx_ooc_cont_ooc ON jnpa.customs_ooc_container (ooc_id);
CREATE INDEX IF NOT EXISTS idx_ooc_cont_no  ON jnpa.customs_ooc_container (container_no);

CREATE TABLE IF NOT EXISTS jnpa.customs_ooc_item (
    id               bigserial PRIMARY KEY,
    ooc_container_id bigint NOT NULL REFERENCES jnpa.customs_ooc_container(id) ON DELETE CASCADE,
    invoice_number   text,
    item_sr_no       integer,
    item_description  text,
    hs_classification text,
    cif_value        numeric,
    assessable_value numeric,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_ooc_item UNIQUE (ooc_container_id, invoice_number, item_sr_no)
);
CREATE INDEX IF NOT EXISTS idx_ooc_item_cont ON jnpa.customs_ooc_item (ooc_container_id);
CREATE INDEX IF NOT EXISTS idx_ooc_item_hs   ON jnpa.customs_ooc_item (hs_classification);

-- ===========================================================================
-- SMTP  (CHPOI13)  — Sub-Manifest Transhipment Permit: one header per SMTP No,
-- flat transhipment lines (bond movement). One destination/bond per permit.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS jnpa.customs_smtp (
    id                     bigserial PRIMARY KEY,
    message_id             bigint NOT NULL REFERENCES jnpa.customs_messages(id) ON DELETE CASCADE,
    customs_house_code     text,
    smtp_no                text NOT NULL,
    smtp_date              date,
    igm_no                 text,
    igm_date               date,
    destination_code       text,
    carrier_code           text,
    bond_no                text,
    terminal_operator_code text,
    created_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_smtp UNIQUE (smtp_no)
);
CREATE INDEX IF NOT EXISTS idx_smtp_msg  ON jnpa.customs_smtp (message_id);
CREATE INDEX IF NOT EXISTS idx_smtp_igm  ON jnpa.customs_smtp (igm_no);
CREATE INDEX IF NOT EXISTS idx_smtp_bond ON jnpa.customs_smtp (bond_no);

CREATE TABLE IF NOT EXISTS jnpa.customs_smtp_line (
    id               bigserial PRIMARY KEY,
    smtp_id          bigint NOT NULL REFERENCES jnpa.customs_smtp(id) ON DELETE CASCADE,
    smtp_no          text NOT NULL,
    line_no          integer NOT NULL,
    subline_no       integer NOT NULL DEFAULT 0,
    consignee_name   text,
    cargo_desc       text,
    container_no     text NOT NULL,         -- soft-links to jnpa.cargo BY VALUE
    iso_valid        boolean NOT NULL DEFAULT true,
    container_type   text,
    seal_no          text,
    no_of_packages   integer,
    unit_of_packages text,
    gross_qty        numeric,
    unit_of_qty      text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_smtp_line UNIQUE (smtp_id, line_no, container_no)
);
CREATE INDEX IF NOT EXISTS idx_smtp_line_smtp ON jnpa.customs_smtp_line (smtp_id);
CREATE INDEX IF NOT EXISTS idx_smtp_line_cont ON jnpa.customs_smtp_line (container_no);

-- ===========================================================================
-- RMS  (.txt)  — Container Scanning Division selection list, keyed by IGM No.
-- The risk-management scanning selection: which containers customs picked to scan.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS jnpa.customs_rms_scanlist (
    id                  bigserial PRIMARY KEY,
    message_id          bigint NOT NULL REFERENCES jnpa.customs_messages(id) ON DELETE CASCADE,
    customs_house       text,
    shipping_line       text,
    shipping_agent      text,
    igm_no              text NOT NULL,
    igm_date            date,               -- best-effort parse of the odd raw format
    igm_date_raw        text,               -- raw as-printed (e.g. "02/2026/5 00:05:00")
    processing_end_date date,
    vessel_name         text,
    subject             text,
    any_selected        boolean NOT NULL DEFAULT false,
    selected_count      integer NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_rms_scanlist UNIQUE (igm_no)
);
CREATE INDEX IF NOT EXISTS idx_rms_scan_msg ON jnpa.customs_rms_scanlist (message_id);

CREATE TABLE IF NOT EXISTS jnpa.customs_rms_container (
    id            bigserial PRIMARY KEY,
    scanlist_id   bigint NOT NULL REFERENCES jnpa.customs_rms_scanlist(id) ON DELETE CASCADE,
    igm_no        text NOT NULL,
    sl_no         integer,
    container_no  text NOT NULL,            -- soft-links to jnpa.cargo BY VALUE
    iso_valid     boolean NOT NULL DEFAULT true,
    scan_machine  text,                     -- M/F/D (mobile/fixed/drive-through), parsed from (D-...)
    scan_location text,                     -- e.g. INNSA1RSDT02
    cfs_name      text,
    goods_desc    text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_rms_container UNIQUE (scanlist_id, container_no)
);
CREATE INDEX IF NOT EXISTS idx_rms_cont_scan ON jnpa.customs_rms_container (scanlist_id);
CREATE INDEX IF NOT EXISTS idx_rms_cont_no   ON jnpa.customs_rms_container (container_no);

-- ===========================================================================
-- Shipping Bill  (.xlsx)  — export declaration.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS jnpa.customs_shipping_bill (
    id         bigserial PRIMARY KEY,
    message_id bigint NOT NULL REFERENCES jnpa.customs_messages(id) ON DELETE CASCADE,
    sb_no      text NOT NULL,
    sb_date    date,
    site_id    text,
    action     text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_shipping_bill UNIQUE (sb_no)
);
CREATE INDEX IF NOT EXISTS idx_sb_msg  ON jnpa.customs_shipping_bill (message_id);
CREATE INDEX IF NOT EXISTS idx_sb_date ON jnpa.customs_shipping_bill (sb_date);

-- ===========================================================================
-- LEO  (.xlsx)  — Let Export Order (export clearance), keyed by SB + LEO date.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS jnpa.customs_leo (
    id          bigserial PRIMARY KEY,
    message_id  bigint NOT NULL REFERENCES jnpa.customs_messages(id) ON DELETE CASCADE,
    sb_no       text NOT NULL,
    sb_date     date,
    site_id     text,
    rotation_no text,                       -- export rotation / IGM reference
    leo_date    date,
    action      text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_leo UNIQUE (sb_no, leo_date)
);
CREATE INDEX IF NOT EXISTS idx_leo_msg ON jnpa.customs_leo (message_id);
CREATE INDEX IF NOT EXISTS idx_leo_sb  ON jnpa.customs_leo (sb_no);

-- ===========================================================================
-- Customs event log — reuses the append-only "events" pattern (as jnpa.cargo_events
-- does), but customs events are not always single-container (a whole IGM is filed,
-- a Bill-of-Entry is cleared), so container_no is NULLable. Generated ONLY from
-- actual customs processing (import / workflow) — never synthetically.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS jnpa.customs_events (
    id           bigserial PRIMARY KEY,
    event        text NOT NULL,             -- customs.igm_filed / customs.rms_selected / customs.ooc_issued / ...
    module       text,
    reference    text,                      -- igm_no / bill_of_entry_no / smtp_no / sb_no
    container_no text,                      -- nullable
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_customs_events_id     ON jnpa.customs_events (id DESC);
CREATE INDEX IF NOT EXISTS idx_customs_events_cont   ON jnpa.customs_events (container_no, id DESC);
CREATE INDEX IF NOT EXISTS idx_customs_events_event  ON jnpa.customs_events (event, id DESC);
CREATE INDEX IF NOT EXISTS idx_customs_events_module ON jnpa.customs_events (module, id DESC);

-- ===========================================================================
-- Derived per-container customs status — the single join that binds the customs
-- document layer to jnpa.cargo BY VALUE (container_no). Powers the workflow /
-- "customs status of this box" view without duplicating any data.
--   declared_igm   : the box appears on an import manifest (CHPOI03)
--   rms_selected   : customs selected it for scanning (risk)
--   ooc_cleared    : an Out-Of-Charge was issued for its Bill-of-Entry (import release-ready)
--   smtp_bonded    : it is on a transhipment permit (bond movement)
-- ===========================================================================
CREATE OR REPLACE VIEW jnpa.v_customs_container_status AS
WITH cont AS (
    SELECT container_no, igm_no FROM jnpa.customs_igm_container
    UNION SELECT container_no, igm_no FROM jnpa.customs_ooc_container oc
          JOIN jnpa.customs_ooc o ON o.id = oc.ooc_id
    UNION SELECT container_no, igm_no FROM jnpa.customs_smtp_line sl
          JOIN jnpa.customs_smtp s ON s.id = sl.smtp_id
    UNION SELECT container_no, igm_no FROM jnpa.customs_rms_container
)
SELECT
    c.container_no,
    max(c.igm_no) AS igm_no,
    EXISTS (SELECT 1 FROM jnpa.customs_igm_container   ic WHERE ic.container_no = c.container_no) AS declared_igm,
    EXISTS (SELECT 1 FROM jnpa.customs_rms_container   rc WHERE rc.container_no = c.container_no) AS rms_selected,
    EXISTS (SELECT 1 FROM jnpa.customs_ooc_container   oc WHERE oc.container_no = c.container_no) AS ooc_cleared,
    EXISTS (SELECT 1 FROM jnpa.customs_smtp_line       sl WHERE sl.container_no = c.container_no) AS smtp_bonded
FROM cont c
GROUP BY c.container_no;
