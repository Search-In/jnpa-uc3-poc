-- ============================================================================
-- 0111  UC-3 lifecycle foundation fixes (all additive / idempotent).
--
-- core.pdp (367k rows) shipped in the v3 copy with NO indexes — the legacy
-- 0026 indexes (pdp_number / appl_number / active) were never ported, so every
-- /api/drivers/master/{licence}/pdp-history call and every latest-permit
-- lateral join is a sequential scan. Recreate them here (RDS-safe: plain
-- CREATE INDEX IF NOT EXISTS, small table, no lock concerns at this size).
-- ============================================================================
BEGIN;

-- Latest-permit lookup: WHERE pdp_number = :n ORDER BY accepted_at DESC LIMIT 1
CREATE INDEX IF NOT EXISTS idx_pdp_number_accepted
    ON core.pdp (pdp_number, accepted_at DESC);

-- Permit lineage: WHERE appl_number = :a (11x renewal fan-out grouping)
CREATE INDEX IF NOT EXISTS idx_pdp_appl
    ON core.pdp (appl_number);

-- Active-permit scans / KPI counts
CREATE INDEX IF NOT EXISTS idx_pdp_active
    ON core.pdp (active) WHERE active IS TRUE;

COMMIT;
