-- 0026_driver_master.sql
-- UC-III Driver Master & Driver Intelligence — the licensed port-driver registry
-- from PDP Details.xlsx. PURELY ADDITIVE: new tables only. It does NOT touch the
-- login-critical jnpa.drivers / driver_enrollments / device_bindings / driver_faces
-- tables or the uq_drivers_vehicle_active invariant, so both driver login flows
-- (Vehicle-ID device-token and OTP/Firebase) are unaffected.
--
-- Two tables:
--   driver_master        <- PDP "Application Data" sheet (1 row per licensed driver)
--   driver_pdp_history   <- PDP "PDP Data" sheet (1 row per permit application)
--
-- driver_master is keyed by the normalised licence number (the natural business
-- key), links to Transport Master by resolved transporter_id, and optionally links
-- to an enrolled login identity in jnpa.drivers (nullable, set only when a registry
-- driver actually enrols for the PWA/biometrics).

SET search_path TO jnpa, public;

CREATE TABLE IF NOT EXISTS jnpa.driver_master (
    id                bigserial PRIMARY KEY,
    licence_no        text NOT NULL,
    licence_no_norm   text NOT NULL,                       -- UPPER, alnum only
    source_srno       bigint,                              -- provenance (Application Data.Srno)
    name              text NOT NULL,
    company_name      text,                                -- as in the source
    transporter_id    bigint REFERENCES jnpa.transporters(id) ON DELETE SET NULL,
    photo_file        text,                                -- source filename ({id}_Photo.jpg)
    photo_url         text,                                -- MinIO pointer (populated later)
    licence_type      text NOT NULL DEFAULT 'HMV',
    licence_valid_to  date,
    latest_pdp_number text,                                -- -> driver_pdp_history.pdp_number
    dob               date,
    enrolled_driver_id text,                               -- -> jnpa.drivers.driver_id (loose, optional)
    status            text NOT NULL DEFAULT 'ACTIVE'
                      CHECK (status IN ('ACTIVE','INACTIVE')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_driver_master_licence ON jnpa.driver_master (licence_no_norm);
CREATE INDEX IF NOT EXISTS idx_driver_master_name        ON jnpa.driver_master (lower(name));
CREATE INDEX IF NOT EXISTS idx_driver_master_company     ON jnpa.driver_master (lower(coalesce(company_name,'')));
CREATE INDEX IF NOT EXISTS idx_driver_master_transporter ON jnpa.driver_master (transporter_id);
CREATE INDEX IF NOT EXISTS idx_driver_master_pdp         ON jnpa.driver_master (latest_pdp_number);
CREATE INDEX IF NOT EXISTS idx_driver_master_valid       ON jnpa.driver_master (licence_valid_to);
CREATE INDEX IF NOT EXISTS idx_driver_master_enrolled    ON jnpa.driver_master (enrolled_driver_id);

CREATE TABLE IF NOT EXISTS jnpa.driver_pdp_history (
    pdp_id                bigint PRIMARY KEY,              -- source pdp_id
    acceptance_time_stamp timestamptz,
    active                boolean NOT NULL DEFAULT false,
    appl_number           text,                            -- application lineage (renewals)
    pdp_number            text,                            -- permit number
    validity              date,
    remarks               text,
    pdp_cancelled_by      text,
    cancellation_time     timestamptz,
    created_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pdp_history_pdpnum  ON jnpa.driver_pdp_history (pdp_number);
CREATE INDEX IF NOT EXISTS idx_pdp_history_appl    ON jnpa.driver_pdp_history (appl_number);
CREATE INDEX IF NOT EXISTS idx_pdp_history_active  ON jnpa.driver_pdp_history (active);
