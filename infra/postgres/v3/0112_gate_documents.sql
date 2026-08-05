-- ============================================================================
-- 0112  Gate Document module (UC-III): EIR / PIN ticket / Form-13 entities +
--        import ledger. Additive only — no existing table is touched.
--
-- These are the three client gate documents the corpus anchors the truck
-- lifecycle on (eir_parsed/*, PIN tickets, form13_parsed/*). Container number
-- is NULLABLE by design: real documents exist with no container number
-- (e.g. truck MH46AF4375's EIR) — such rows stay truck-keyed.
-- EIR TruckIn/TruckOut yields the document-derived TAT (82/165-min ground
-- truth) via a generated column.
-- ============================================================================
BEGIN;

-- ---------------------------------------------------------------- ledger
CREATE TABLE IF NOT EXISTS core.gate_doc_import_file (
    id               bigserial PRIMARY KEY,
    doc_type         text NOT NULL CHECK (doc_type IN ('EIR','PIN','FORM13')),
    physical_format  text NOT NULL CHECK (physical_format IN ('CSV','XLS','XLSX')),
    source_file      text NOT NULL,
    source_sha256    text NOT NULL,
    file_size_bytes  bigint,
    record_count     integer NOT NULL DEFAULT 0,
    imported_count   integer NOT NULL DEFAULT 0,
    error_count      integer NOT NULL DEFAULT 0,
    duplicate_count  integer NOT NULL DEFAULT 0,
    import_status    text NOT NULL DEFAULT 'PENDING'
                     CHECK (import_status IN ('PENDING','SUCCESS','PARTIAL','FAILED','SKIPPED_DUPLICATE')),
    error_detail     text,
    uploaded_by      text,
    source           text NOT NULL DEFAULT 'UPLOAD' CHECK (source IN ('DIRECTORY','UPLOAD')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_gate_doc_import_sha UNIQUE (source_sha256)
);
CREATE INDEX IF NOT EXISTS idx_gate_doc_file_type ON core.gate_doc_import_file (doc_type, id DESC);
CREATE INDEX IF NOT EXISTS idx_gate_doc_file_status ON core.gate_doc_import_file (import_status, id DESC);

CREATE TABLE IF NOT EXISTS core.gate_doc_import_error (
    id              bigserial PRIMARY KEY,
    import_file_id  bigint NOT NULL REFERENCES core.gate_doc_import_file(id) ON DELETE CASCADE,
    record_ref      text,
    error_code      text NOT NULL,
    error_detail    text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gate_doc_err_file ON core.gate_doc_import_error (import_file_id, id);

-- ------------------------------------------------------------------- EIR
CREATE TABLE IF NOT EXISTS core.eir (
    id               bigserial PRIMARY KEY,
    eir_no           text,
    eir_type         text,
    terminal         text,
    container_number text,
    iso_valid        boolean,
    vessel           text,
    via_no           text,
    seal_number      text,
    bat_lane         text,
    truck_no         text NOT NULL,
    driver_name      text,
    driver_licence   text,
    truck_in_time    timestamptz,
    truck_out_time   timestamptz,
    tat_minutes      numeric GENERATED ALWAYS AS (
                         round(extract(epoch FROM (truck_out_time - truck_in_time)) / 60.0)
                     ) STORED,
    gross_weight_mt  numeric,
    company          text,
    cfs_from         text,
    cfs_to           text,
    group_code       text,
    scanner_stamp    text,
    remarks          text,
    row_sha256       text,
    source_file      text,
    import_file_id   bigint REFERENCES core.gate_doc_import_file(id),
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_eir_row_sha ON core.eir (row_sha256)
    WHERE row_sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_eir_container ON core.eir (container_number);
CREATE INDEX IF NOT EXISTS idx_eir_truck ON core.eir (truck_no);
CREATE INDEX IF NOT EXISTS idx_eir_truck_in ON core.eir (truck_in_time DESC NULLS LAST);

-- ------------------------------------------------------------------- PIN
-- One row per MOVE LEG: a dual-move ticket (export drop + import pick, one
-- trip) is two rows sharing pin_number with leg_seq 1/2.
CREATE TABLE IF NOT EXISTS core.pin_ticket (
    id               bigserial PRIMARY KEY,
    pin_number       text NOT NULL,
    ticket_type      text,
    terminal         text,
    truck_no         text NOT NULL,
    company          text,
    container_number text,
    iso_valid        boolean,
    group_code       text,
    yard_location    text,
    gate             text,
    move_type        text CHECK (move_type IS NULL OR
                                 move_type IN ('IMPORT_PICK','EXPORT_DROP','EMPTY_PICK','EMPTY_DROP')),
    leg_seq          integer NOT NULL DEFAULT 1,
    issued_at        timestamptz,
    remarks          text,
    row_sha256       text,
    source_file      text,
    import_file_id   bigint REFERENCES core.gate_doc_import_file(id),
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_pin_row_sha ON core.pin_ticket (row_sha256)
    WHERE row_sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_pin_number ON core.pin_ticket (pin_number);
CREATE INDEX IF NOT EXISTS idx_pin_container ON core.pin_ticket (container_number);
CREATE INDEX IF NOT EXISTS idx_pin_truck ON core.pin_ticket (truck_no);

-- ---------------------------------------------------------------- FORM-13
-- NO new table. Form-13 REUSES the existing core.gate_capture store, which
-- already declares capture_type 'FORM13' in its CHECK constraint, keeps
-- container_no NULLABLE (so a containerless Form-13 is storable), and carries
-- vehicle_plate + a jsonb payload for the document-specific fields (VisitID,
-- in/out gate codes, transporter, direction, BAT). Real uploads are written with
-- source_mode='live', which distinguishes them from the pre-existing
-- deterministic 'sim' seed rows without touching those rows.
--   payload keys: form13_no, visit_id, terminal, transporter_name, driver_name,
--                 driver_licence, in_gate, out_gate, direction, bat_lane,
--                 shipping_bill_no, gross_wt_kg, remarks, row_sha256,
--                 source_file, import_file_id
-- Idempotency comes from the table's existing
-- UNIQUE (container_no, capture_type, captured_at).
-- Two supporting indexes for the new query paths (additive):
CREATE INDEX IF NOT EXISTS idx_gate_capture_form13_visit
    ON core.gate_capture ((payload->>'visit_id')) WHERE capture_type = 'FORM13';
CREATE INDEX IF NOT EXISTS idx_gate_capture_form13_sha
    ON core.gate_capture ((payload->>'row_sha256')) WHERE capture_type = 'FORM13';

COMMIT;
