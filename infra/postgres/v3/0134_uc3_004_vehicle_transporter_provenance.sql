-- 0134 — UC3-004: provenance for the vehicle -> transporter registry.
--
-- Gap G6: neither TransporterDetails.xlsx nor PDP Details.xlsx carries a vehicle
-- number, so the vehicle->transporter relationship cannot be loaded from the
-- customer's master data. Only the gate-document corpus evidences it, and only
-- for the plates whose slip actually prints a transporter name — 3 of them in
-- the current drop. Everything else has to be generated, and a generated
-- ownership claim must never be indistinguishable from a real one.
--
-- This migration therefore makes provenance EXPLICIT and mandatory-by-default on
-- every row of core.transporter_vehicle:
--
--   DOCUMENT_EVIDENCED — the mapping is printed on a real gate document
--                        (core.gate_document.data_origin = 'REAL'); source_ref
--                        records which document variant it came from.
--   SYNTHETIC          — generated to fill the G6 gap. assumption_ref is
--                        REQUIRED (A-G6) so the UI can label it and the reader
--                        can find the assumption that justifies it.
--
-- Fully ADDITIVE and idempotent: no column dropped, no row rewritten, no
-- existing constraint changed. Safe to re-run.
BEGIN;

-- ------------------------------------------------------------- 1. provenance
ALTER TABLE core.transporter_vehicle
    ADD COLUMN IF NOT EXISTS provenance    text,
    ADD COLUMN IF NOT EXISTS assumption_ref text,
    ADD COLUMN IF NOT EXISTS source_ref     text;

-- Vocabulary is closed. NULL stays legal so a pre-existing row is not
-- retroactively reinterpreted as real; the seeder stamps every row it writes.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'transporter_vehicle_provenance_check') THEN
        ALTER TABLE core.transporter_vehicle
            ADD CONSTRAINT transporter_vehicle_provenance_check
            CHECK (provenance IS NULL
                   OR provenance IN ('DOCUMENT_EVIDENCED', 'SYNTHETIC'));
    END IF;
END $$;

-- A SYNTHETIC row without an assumption reference is exactly the failure mode
-- this ticket exists to prevent, so the database refuses it outright.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'transporter_vehicle_synthetic_needs_assumption') THEN
        ALTER TABLE core.transporter_vehicle
            ADD CONSTRAINT transporter_vehicle_synthetic_needs_assumption
            CHECK (provenance IS DISTINCT FROM 'SYNTHETIC'
                   OR assumption_ref IS NOT NULL);
    END IF;
END $$;

COMMENT ON COLUMN core.transporter_vehicle.provenance IS
    'DOCUMENT_EVIDENCED = the vehicle->transporter link is printed on a REAL '
    'gate document; SYNTHETIC = generated to fill gap G6. NULL on rows written '
    'before this migration.';
COMMENT ON COLUMN core.transporter_vehicle.assumption_ref IS
    'Assumptions-register key justifying a generated mapping (A-G6). Required '
    'whenever provenance = SYNTHETIC; enforced by CHECK.';
COMMENT ON COLUMN core.transporter_vehicle.source_ref IS
    'For DOCUMENT_EVIDENCED rows, the gate_document.doc_variant the mapping was '
    'read from, so the claim can be traced back to the physical slip.';

-- ------------------------------------------------------------- 2. query paths
CREATE INDEX IF NOT EXISTS idx_transporter_veh_provenance
    ON core.transporter_vehicle (provenance);

COMMIT;
