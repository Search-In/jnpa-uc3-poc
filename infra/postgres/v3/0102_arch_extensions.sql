-- ============================================================================
-- 0102  Additive extensions to architecture tables.
-- Rule: NEVER alter existing columns/PKs/seeded values. Only ADD.
-- Rationale per column: preserve legacy API response fields (DTO freeze).
-- ============================================================================
BEGIN;

-- ---------------------------------------------------------------------------
-- core.transporter  <- jnpa.transporters
-- Legacy DTO exposes: id, code, name, gstin, contact, status, created_at,
-- updated_at (SELECT t.*). Arch table lacks all of these except name-parts.
-- id keeps legacy values via UNIQUE ext column with its own sequence.
-- ---------------------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS core.transporter_id_seq;
CREATE SEQUENCE IF NOT EXISTS core.transporter_company_id_seq;
ALTER TABLE core.transporter
    ADD COLUMN id         bigint DEFAULT nextval('core.transporter_id_seq') NOT NULL,
    ADD COLUMN code       text,
    ADD COLUMN gstin      text,
    ADD COLUMN contact    text,
    ADD COLUMN status     text DEFAULT 'ACTIVE' NOT NULL,
    ADD COLUMN created_at timestamptz DEFAULT now() NOT NULL,
    ADD COLUMN updated_at timestamptz DEFAULT now() NOT NULL;
ALTER TABLE core.transporter ADD CONSTRAINT uq_transporter_id UNIQUE (id);
CREATE UNIQUE INDEX uq_transporter_code ON core.transporter (code) WHERE code IS NOT NULL;
-- API-created rows need generated company_id (arch PK has no default)
ALTER TABLE core.transporter
    ALTER COLUMN company_id SET DEFAULT nextval('core.transporter_company_id_seq');
CREATE TRIGGER trg_transporter_updated_at BEFORE UPDATE ON core.transporter
    FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();

-- NOTE: FKs from ported transporter satellites to core.transporter(id) are
-- added at the END of 0202 (after legacy ids are backfilled).

-- ---------------------------------------------------------------------------
-- core.driver  <- jnpa.driver_master
-- Legacy DTO: id, licence_no, licence_no_norm, status, photo_url,
-- enrolled_driver_id, transporter_id, licence_valid_to, timestamps.
-- ---------------------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS core.driver_id_seq;
ALTER TABLE core.driver
    ADD COLUMN id                 bigint DEFAULT nextval('core.driver_id_seq'),
    ADD COLUMN licence_no_norm    text GENERATED ALWAYS AS
        (regexp_replace(upper(coalesce(licence_number,'')), '[^A-Z0-9]', '', 'g')) STORED,
    ADD COLUMN transporter_id     bigint REFERENCES core.transporter(id) ON DELETE SET NULL,
    ADD COLUMN status             text DEFAULT 'ACTIVE' NOT NULL,
    ADD COLUMN photo_url          text,
    ADD COLUMN enrolled_driver_id text,
    ADD COLUMN licence_valid_to   date,
    ADD COLUMN source_srno        bigint,
    ADD COLUMN created_at         timestamptz DEFAULT now() NOT NULL,
    ADD COLUMN updated_at         timestamptz DEFAULT now() NOT NULL;
ALTER TABLE core.driver ADD CONSTRAINT uq_driver_ext_id UNIQUE (id);
ALTER TABLE core.driver ADD CONSTRAINT driver_status_check
    CHECK (status IN ('ACTIVE','INACTIVE'));
CREATE INDEX idx_driver_licence_norm ON core.driver (licence_no_norm);
CREATE INDEX idx_driver_name_lower   ON core.driver (lower(driver_name));
CREATE INDEX idx_driver_company_lower ON core.driver (lower(coalesce(company_name,'')));
CREATE INDEX idx_driver_transporter  ON core.driver (transporter_id);
CREATE INDEX idx_driver_valid        ON core.driver (licence_valid_to);
CREATE INDEX idx_driver_enrolled     ON core.driver (enrolled_driver_id);
CREATE TRIGGER trg_driver_updated_at BEFORE UPDATE ON core.driver
    FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();

-- ---------------------------------------------------------------------------
-- core.pdp  <- jnpa.driver_pdp_history
-- pdp_id widened (source is bigint); cancellation kept at full precision.
-- ---------------------------------------------------------------------------
ALTER TABLE core.pdp ALTER COLUMN pdp_id TYPE bigint;
ALTER TABLE core.pdp
    ADD COLUMN cancellation_time timestamptz,
    ADD COLUMN created_at        timestamptz DEFAULT now();

-- ---------------------------------------------------------------------------
-- core.vehicle  <- jnpa.fleet_vehicles
-- Legacy DTO: id, vehicle_id, vehicle_number(->vehicle_no), vehicle_type,
-- chassis_number, rfid_fastag_id, status, created_by, timestamps.
-- ---------------------------------------------------------------------------
ALTER TABLE core.vehicle
    ADD COLUMN id             uuid DEFAULT gen_random_uuid(),
    ADD COLUMN vehicle_id     text,
    ADD COLUMN vehicle_type   text,
    ADD COLUMN chassis_number text,
    ADD COLUMN rfid_fastag_id text,
    ADD COLUMN status         text DEFAULT 'ACTIVE',
    ADD COLUMN created_by     text,
    ADD COLUMN created_at     timestamptz DEFAULT now(),
    ADD COLUMN updated_at     timestamptz DEFAULT now();
ALTER TABLE core.vehicle ADD CONSTRAINT uq_vehicle_ext_id UNIQUE (id);
CREATE UNIQUE INDEX uq_vehicle_vehicle_id ON core.vehicle (vehicle_id)
    WHERE vehicle_id IS NOT NULL;
CREATE TRIGGER trg_vehicle_updated_at BEFORE UPDATE ON core.vehicle
    FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();

-- ---------------------------------------------------------------------------
-- Customs family: message lineage + legacy row ids + ISO validity flag.
-- core.customs_message is the ported jnpa.customs_messages envelope table.
-- ---------------------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS core.igm_id_seq;
ALTER TABLE core.igm
    ADD COLUMN id         bigint DEFAULT nextval('core.igm_id_seq'),
    ADD COLUMN message_id bigint REFERENCES core.customs_message(id),
    ADD COLUMN created_at timestamptz DEFAULT now();
ALTER TABLE core.igm ADD CONSTRAINT uq_igm_ext_id UNIQUE (id);

CREATE SEQUENCE IF NOT EXISTS core.igm_line_id_seq;
ALTER TABLE core.igm_line
    ADD COLUMN id         bigint DEFAULT nextval('core.igm_line_id_seq'),
    ADD COLUMN created_at timestamptz DEFAULT now(),
    ADD COLUMN importer_state text,
    ADD COLUMN be_regularised text;
ALTER TABLE core.igm_line ADD CONSTRAINT uq_igm_line_ext_id UNIQUE (id);

CREATE SEQUENCE IF NOT EXISTS core.igm_line_container_id_seq;
ALTER TABLE core.igm_line_container
    ADD COLUMN id         bigint DEFAULT nextval('core.igm_line_container_id_seq'),
    ADD COLUMN iso_valid  boolean,
    ADD COLUMN created_at timestamptz DEFAULT now();
ALTER TABLE core.igm_line_container ADD CONSTRAINT uq_igm_line_container_ext_id UNIQUE (id);

CREATE SEQUENCE IF NOT EXISTS core.bill_of_entry_ooc_id_seq;
ALTER TABLE core.bill_of_entry_ooc
    ADD COLUMN id           bigint DEFAULT nextval('core.bill_of_entry_ooc_id_seq'),
    ADD COLUMN message_id   bigint REFERENCES core.customs_message(id),
    ADD COLUMN ooc_type     text,
    ADD COLUMN created_at   timestamptz DEFAULT now();
ALTER TABLE core.bill_of_entry_ooc ADD CONSTRAINT uq_ooc_ext_id UNIQUE (id);

CREATE SEQUENCE IF NOT EXISTS core.ooc_item_id_seq;
ALTER TABLE core.ooc_item
    ADD COLUMN id         bigint DEFAULT nextval('core.ooc_item_id_seq'),
    ADD COLUMN iso_valid  boolean,
    ADD COLUMN created_at timestamptz DEFAULT now();
ALTER TABLE core.ooc_item ADD CONSTRAINT uq_ooc_item_ext_id UNIQUE (id);

CREATE SEQUENCE IF NOT EXISTS core.smtp_permit_id_seq;
ALTER TABLE core.smtp_permit
    ADD COLUMN id         bigint DEFAULT nextval('core.smtp_permit_id_seq'),
    ADD COLUMN message_id bigint REFERENCES core.customs_message(id),
    ADD COLUMN created_at timestamptz DEFAULT now();
ALTER TABLE core.smtp_permit ADD CONSTRAINT uq_smtp_permit_ext_id UNIQUE (id);

CREATE SEQUENCE IF NOT EXISTS core.smtp_container_id_seq;
ALTER TABLE core.smtp_container
    ADD COLUMN id           bigint DEFAULT nextval('core.smtp_container_id_seq'),
    ADD COLUMN line_no      integer,
    ADD COLUMN subline_no   integer,
    ADD COLUMN iso_valid    boolean,
    ADD COLUMN created_at   timestamptz DEFAULT now();
ALTER TABLE core.smtp_container ADD CONSTRAINT uq_smtp_container_ext_id UNIQUE (id);

CREATE SEQUENCE IF NOT EXISTS core.leo_id_seq;
ALTER TABLE core.leo
    ADD COLUMN id         bigint DEFAULT nextval('core.leo_id_seq'),
    ADD COLUMN message_id bigint REFERENCES core.customs_message(id),
    ADD COLUMN action     text,
    ADD COLUMN created_at timestamptz DEFAULT now();
ALTER TABLE core.leo ADD CONSTRAINT uq_leo_ext_id UNIQUE (id);

CREATE SEQUENCE IF NOT EXISTS core.shipping_bill_id_seq;
ALTER TABLE core.shipping_bill
    ADD COLUMN id         bigint DEFAULT nextval('core.shipping_bill_id_seq'),
    ADD COLUMN message_id bigint REFERENCES core.customs_message(id),
    ADD COLUMN action     text,
    ADD COLUMN created_at timestamptz DEFAULT now();
ALTER TABLE core.shipping_bill ADD CONSTRAINT uq_shipping_bill_ext_id UNIQUE (id);

CREATE SEQUENCE IF NOT EXISTS core.rms_scan_report_id_ext_seq;
ALTER TABLE core.rms_scan_report
    ADD COLUMN id            bigint DEFAULT nextval('core.rms_scan_report_id_ext_seq'),
    ADD COLUMN message_id    bigint REFERENCES core.customs_message(id),
    ADD COLUMN customs_house text,
    ADD COLUMN igm_date      date,
    ADD COLUMN igm_date_raw  text,
    ADD COLUMN subject       text,
    ADD COLUMN any_selected  boolean,
    ADD COLUMN selected_count integer,
    ADD COLUMN created_at    timestamptz DEFAULT now();
ALTER TABLE core.rms_scan_report ADD CONSTRAINT uq_rms_scan_report_ext_id UNIQUE (id);

CREATE SEQUENCE IF NOT EXISTS core.rms_scan_container_id_seq;
ALTER TABLE core.rms_scan_container
    ADD COLUMN id         bigint DEFAULT nextval('core.rms_scan_container_id_seq'),
    ADD COLUMN igm_no     bigint,
    ADD COLUMN iso_valid  boolean,
    ADD COLUMN created_at timestamptz DEFAULT now();
ALTER TABLE core.rms_scan_container ADD CONSTRAINT uq_rms_scan_container_ext_id UNIQUE (id);

-- ---------------------------------------------------------------------------
-- Shipping lines: legacy ids, upload linkage (per-module trackers were ported
-- as core.sl_import_file / core.sl_import_error / core.sl_event), idempotency
-- key, and source columns the unified layout drops.
-- ---------------------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS core.advance_list_container_id_seq;
ALTER TABLE core.advance_list_container
    ADD COLUMN id                 bigint DEFAULT nextval('core.advance_list_container_id_seq'),
    ADD COLUMN import_file_id     bigint REFERENCES core.sl_import_file(id),
    ADD COLUMN row_sha256         text,
    ADD COLUMN container_valid_iso boolean,
    ADD COLUMN weight_source_uom  text,
    ADD COLUMN destination        text,
    ADD COLUMN voyage             text,
    ADD COLUMN reefer_status      text,
    ADD COLUMN created_at         timestamptz DEFAULT now();
ALTER TABLE core.advance_list_container ADD CONSTRAINT uq_alc_ext_id UNIQUE (id);
CREATE UNIQUE INDEX uq_alc_row_sha256 ON core.advance_list_container (row_sha256)
    WHERE row_sha256 IS NOT NULL;
CREATE INDEX idx_alc_import_file ON core.advance_list_container (import_file_id);

CREATE SEQUENCE IF NOT EXISTS core.delivery_order_id_seq;
ALTER TABLE core.delivery_order
    ADD COLUMN id              bigint DEFAULT nextval('core.delivery_order_id_seq'),
    ADD COLUMN import_file_id  bigint REFERENCES core.sl_import_file(id),
    ADD COLUMN message_type    text,
    ADD COLUMN sender_id       text,
    ADD COLUMN receiving_party text,
    ADD COLUMN call_sign       text,
    ADD COLUMN stuff_destuff_flag text,
    ADD COLUMN shipping_agent_code text,
    ADD COLUMN vessel_country  text,
    ADD COLUMN total_containers integer,
    ADD COLUMN raw_xml         text,
    ADD COLUMN created_at      timestamptz DEFAULT now();
ALTER TABLE core.delivery_order ADD CONSTRAINT uq_do_ext_id UNIQUE (id);

CREATE SEQUENCE IF NOT EXISTS core.delivery_order_line_id_seq;
ALTER TABLE core.delivery_order_line
    ADD COLUMN id               bigint DEFAULT nextval('core.delivery_order_line_id_seq'),
    ADD COLUMN equipment_status text,
    ADD COLUMN cargo_type       text,
    ADD COLUMN arrival_ts       timestamptz,
    ADD COLUMN receipt_date     date,
    ADD COLUMN delivery_mode    text,
    ADD COLUMN gate_pass_no     text,
    ADD COLUMN gate_pass_ts     timestamptz,
    ADD COLUMN vehicle_no       text,
    ADD COLUMN gate_number      text,
    ADD COLUMN ca_code          text,
    ADD COLUMN con_seal_status  text,
    ADD COLUMN issued_ts        timestamptz,
    ADD COLUMN created_at       timestamptz DEFAULT now();
ALTER TABLE core.delivery_order_line ADD CONSTRAINT uq_dol_ext_id UNIQUE (id);

ALTER TABLE core.ref_shipping_line
    ADD COLUMN source     text,
    ADD COLUMN first_seen timestamptz,
    ADD COLUMN last_seen  timestamptz;

COMMIT;
