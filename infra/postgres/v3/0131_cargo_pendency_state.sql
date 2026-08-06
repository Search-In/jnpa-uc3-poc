-- ============================================================================
-- 0131  PENDENCY — the missing cargo lifecycle state.
--
-- The audited lifecycle expected
--
--   Creation -> Movement -> Vessel Discharge -> PENDENCY -> Yard Assignment ->
--   Yard Position -> Reefer/Rail Planning -> Scan Queue -> Verification -> Release
--
-- and 10 of the 11 steps existed. "Pendency" existed only as a daily TEU
-- aggregate (core.perf_daily_terminal_status.icd_pendency_teus /
-- cfs_pendency_teus) and as a notification event name — never as a state a
-- CONTAINER could be in. A box discharged and waiting for evacuation was
-- indistinguishable from one just discharged.
--
-- PENDENCY is OPTIONAL, not a mandatory gate. Making it mandatory would
-- invalidate every container already recorded as VESSEL_DISCHARGED ->
-- YARD_ASSIGNED and break the existing flow, so VESSEL_DISCHARGED ->
-- YARD_ASSIGNED stays legal. The state machine ranks it at 15, between
-- VESSEL_DISCHARGED (10) and YARD_ASSIGNED (20) — see
-- services/cargo/service.py::_LIFECYCLE_RANK.
--
-- Additive: the lifecycle CHECK is WIDENED (every previously-legal value stays
-- legal); no row can be invalidated. Dropping and re-adding is the only way to
-- widen a CHECK in Postgres.
-- ============================================================================
BEGIN;

ALTER TABLE core.cargo DROP CONSTRAINT IF EXISTS cargo_lifecycle_status_check;
ALTER TABLE core.cargo ADD CONSTRAINT cargo_lifecycle_status_check
    CHECK (lifecycle_status IN (
        -- import leg (0023 / 0115, unchanged) + PENDENCY
        'CREATED','VESSEL_DISCHARGED','PENDENCY','YARD_ASSIGNED',
        'YARD_POSITION_ALLOCATED','REEFER_PLANNED','RAKE_ASSIGNED','SCAN_PENDING',
        'VERIFIED','RELEASED',
        -- export leg (0115, unchanged)
        'EXPORT_BOOKED','FORM13_ISSUED','EXPORT_GATE_IN','VGM_CAPTURED',
        'LEO_GRANTED','LOAD_LISTED','VESSEL_LOADED'
    ));

-- Pendency is queried as "everything discharged and not yet yarded", so give
-- that predicate an index rather than making callers scan.
CREATE INDEX IF NOT EXISTS idx_cargo_pendency
    ON core.cargo (lifecycle_status, created_at DESC)
    WHERE lifecycle_status IN ('VESSEL_DISCHARGED','PENDENCY');

COMMENT ON COLUMN core.cargo.lifecycle_status IS
    'Unified cargo lifecycle. Import: CREATED -> VESSEL_DISCHARGED -> [PENDENCY] '
    '-> YARD_ASSIGNED -> [position/reefer/rake] -> VERIFIED -> RELEASED. '
    'PENDENCY (0131) is optional — discharge may go straight to yard assignment.';

COMMIT;
