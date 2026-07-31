-- ============================================================================
-- 0113  Container Job Assignment (UC-III backbone) + gate/yard/scanner events.
--
-- The audit's #1 structural gap: there was NO truck<->container job entity —
-- only a free-text core.cargo.vehicle_number with no driver, no validation and
-- no uniqueness. This migration adds the assignment spine and the movement
-- events the lifecycle needs, all additive.
-- ============================================================================
BEGIN;

-- --------------------------------------------------------------- assignment
CREATE TABLE IF NOT EXISTS core.container_job_assignment (
    id                 bigserial PRIMARY KEY,
    container_number   text,                        -- NULL for empty-by-group jobs
    group_code         text,
    transporter_id     bigint REFERENCES core.transporter(id) ON DELETE SET NULL,
    vehicle_id         text NOT NULL,               -- core.vehicle.vehicle_id (TRK-000123)
    vehicle_no         text,                        -- human plate at assignment time
    driver_id          text,                        -- core.driver_identity.driver_id
    driver_licence     text,                        -- core.driver.licence_number
    move_type          text NOT NULL
                       CHECK (move_type IN ('IMPORT_PICK','EXPORT_DROP','EMPTY_PICK','EMPTY_DROP')),
    document_type      text CHECK (document_type IS NULL OR
                                   document_type IN ('EIR','PIN','FORM13','GATEPASS')),
    document_reference text,
    terminal           text,
    gate               text,
    status             text NOT NULL DEFAULT 'ASSIGNED'
                       CHECK (status IN ('ASSIGNED','ACCEPTED','AT_GATE','IN_YARD',
                                         'PICKED_UP','DROPPED','COMPLETED','CANCELLED')),
    assigned_by        text,
    assigned_at        timestamptz NOT NULL DEFAULT now(),
    accepted_at        timestamptz,
    completed_at       timestamptz,
    cancelled_reason   text,
    notes              text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- A vehicle may hold at most ONE open job at a time (the double-assignment guard
-- the audit found missing). Terminal states are excluded from the constraint.
CREATE UNIQUE INDEX IF NOT EXISTS uq_job_open_vehicle
    ON core.container_job_assignment (vehicle_id)
    WHERE status NOT IN ('COMPLETED','CANCELLED');
-- A container may likewise have at most one open job.
CREATE UNIQUE INDEX IF NOT EXISTS uq_job_open_container
    ON core.container_job_assignment (container_number)
    WHERE container_number IS NOT NULL AND status NOT IN ('COMPLETED','CANCELLED');
CREATE INDEX IF NOT EXISTS idx_job_container ON core.container_job_assignment (container_number);
CREATE INDEX IF NOT EXISTS idx_job_vehicle ON core.container_job_assignment (vehicle_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_job_driver ON core.container_job_assignment (driver_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_job_status ON core.container_job_assignment (status, id DESC);
CREATE INDEX IF NOT EXISTS idx_job_transporter ON core.container_job_assignment (transporter_id);

-- Append-only status history (every transition, who + why).
CREATE TABLE IF NOT EXISTS core.container_job_event (
    id           bigserial PRIMARY KEY,
    job_id       bigint NOT NULL REFERENCES core.container_job_assignment(id) ON DELETE CASCADE,
    event        text NOT NULL,
    old_status   text,
    new_status   text,
    actor        text,
    actor_role   text,
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_job_event_job ON core.container_job_event (job_id, id);
CREATE INDEX IF NOT EXISTS idx_job_event_id ON core.container_job_event (id DESC);

-- ------------------------------------------------- gate events (real ingest)
-- core.gate_event exists but is simulator-only and carries no container, lane or
-- document reference. Extend it additively so a REAL gate crossing can be
-- recorded through the API without breaking the simulator's writes.
ALTER TABLE core.gate_event ADD COLUMN IF NOT EXISTS container_number text;
ALTER TABLE core.gate_event ADD COLUMN IF NOT EXISTS bat_lane text;
ALTER TABLE core.gate_event ADD COLUMN IF NOT EXISTS document_type text;
ALTER TABLE core.gate_event ADD COLUMN IF NOT EXISTS document_reference text;
ALTER TABLE core.gate_event ADD COLUMN IF NOT EXISTS job_id bigint;
ALTER TABLE core.gate_event ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE core.gate_event ADD COLUMN IF NOT EXISTS driver_id text;
CREATE INDEX IF NOT EXISTS idx_gate_event_container ON core.gate_event (container_number);
CREATE INDEX IF NOT EXISTS idx_gate_event_job ON core.gate_event (job_id);
-- device_id/trip_id are NOT NULL in the base table; API-recorded crossings
-- supply the plate as the device id and the job reference as the trip id.

-- --------------------------------------------------------- yard movements
CREATE TABLE IF NOT EXISTS core.cargo_movement_event (
    id               bigserial PRIMARY KEY,
    job_id           bigint REFERENCES core.container_job_assignment(id) ON DELETE SET NULL,
    container_number text,
    movement_type    text NOT NULL
                     CHECK (movement_type IN ('YARD_PICKUP','YARD_DROP','YARD_MOVE')),
    vehicle_id       text,
    vehicle_no       text,
    driver_id        text,
    yard_location    text,                          -- free format (e.g. 2P08D.1)
    from_location    text,
    terminal         text,
    occurred_at      timestamptz NOT NULL DEFAULT now(),
    actor            text,
    detail           jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_movement_container ON core.cargo_movement_event (container_number, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_movement_job ON core.cargo_movement_event (job_id, id);
CREATE INDEX IF NOT EXISTS idx_movement_vehicle ON core.cargo_movement_event (vehicle_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_movement_type ON core.cargo_movement_event (movement_type, occurred_at DESC);

-- ----------------------------------------------------------- scanner master
CREATE TABLE IF NOT EXISTS core.scanner_machine (
    id            bigserial PRIMARY KEY,
    machine_code  text NOT NULL UNIQUE,             -- e.g. D-INNSA1RSDT01
    machine_class text NOT NULL
                  CHECK (machine_class IN ('DRIVE_THROUGH','MOBILE','FIXED')),
    machine_type  text,                             -- the raw D / M / F prefix letter
    location_code text,                             -- e.g. INNSA1RSDT01
    customs_house text,
    terminal      text,
    lane          text,
    active        boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_scanner_class ON core.scanner_machine (machine_class);
CREATE INDEX IF NOT EXISTS idx_scanner_location ON core.scanner_machine (location_code);

-- Seed the four machines named in the client's RMS selection lists.
INSERT INTO core.scanner_machine (machine_code, machine_class, machine_type, location_code, customs_house)
VALUES
    ('D-INNSA1RSDT01','DRIVE_THROUGH','D','INNSA1RSDT01','INNSA1'),
    ('D-INNSA1RSDT02','DRIVE_THROUGH','D','INNSA1RSDT02','INNSA1'),
    ('M-INNSA1SDMB01','MOBILE','M','INNSA1SDMB01','INNSA1'),
    ('M-INNSA1SDMB02','MOBILE','M','INNSA1SDMB02','INNSA1')
ON CONFLICT (machine_code) DO NOTHING;

-- ------------------------------------------------------------- scan events
CREATE TABLE IF NOT EXISTS core.scan_event (
    id               bigserial PRIMARY KEY,
    container_number text NOT NULL,
    job_id           bigint REFERENCES core.container_job_assignment(id) ON DELETE SET NULL,
    vehicle_id       text,
    vehicle_no       text,
    machine_code     text REFERENCES core.scanner_machine(machine_code) ON DELETE SET NULL,
    igm_no           bigint,
    result           text NOT NULL
                     CHECK (result IN ('SCAN_PENDING','SCANNED_CLEAN','SCAN_HOLD','SCAN_SKIPPED')),
    remarks          text,
    scanned_at       timestamptz NOT NULL DEFAULT now(),
    actor            text,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_scan_container ON core.scan_event (container_number, scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_scan_job ON core.scan_event (job_id);
CREATE INDEX IF NOT EXISTS idx_scan_result ON core.scan_event (result, scanned_at DESC);

COMMIT;
