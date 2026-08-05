-- ============================================================================
-- 0114  core.ecy_cfs_chain — the F-Y1 empty-repositioning chain (UC-III).
--
-- The audit found the ECY->CFS->gate-out chain was never materialised: the 1,928
-- CODECO rows sit flat in core.cfs_ecy_movement and mart.v_cfs_ecy_dwell groups
-- BY facility, so an ECY leg and a CFS leg can never combine. This table
-- materialises one row per container chain with its legs, durations and anomaly
-- flags, rebuilt idempotently from the movement rows.
--
-- Why a TABLE and not a view: the anomaly flags (duplicate IN, multi-OUT,
-- OUT-before-IN, orphan legs) are evaluated at rebuild time and must be
-- inspectable/queryable; a view would recompute the window functions on every
-- read of a 1,928-row (and growing) event table.
--
-- Additive: it reads core.cfs_ecy_movement and writes only itself.
-- ============================================================================
BEGIN;

CREATE TABLE IF NOT EXISTS core.ecy_cfs_chain (
    id                bigserial PRIMARY KEY,
    container_number  text NOT NULL,

    -- the three legs of the canonical chain
    ecy_out_ts        timestamptz,      -- leg 1: ECY gate-OUT (empty released)
    cfs_in_ts         timestamptz,      -- leg 2: CFS gate-IN  (after road shuttle)
    cfs_out_ts        timestamptz,      -- leg 3: CFS gate-OUT (to terminal for export)
    ecy_in_ts         timestamptz,      -- the return leg (empty back at ECY)

    -- derived durations (hours)
    transit_hours     numeric,          -- ECY-out -> CFS-in   (the road leg)
    dwell_hours       numeric,          -- CFS-in  -> CFS-out  (stuffing/dwell)
    cycle_hours       numeric,          -- ECY-out -> CFS-out  (full repositioning)

    -- completeness + provenance
    chain_status      text NOT NULL DEFAULT 'PARTIAL'
                      CHECK (chain_status IN ('COMPLETE','PARTIAL','ORPHAN')),
    legs_present      integer NOT NULL DEFAULT 0,
    event_count       integer NOT NULL DEFAULT 0,

    -- anomaly flags (the planted COSU4663595 case and its siblings)
    has_anomaly       boolean NOT NULL DEFAULT false,
    anomaly_codes     text[] NOT NULL DEFAULT '{}',
    anomaly_detail    jsonb  NOT NULL DEFAULT '{}'::jsonb,

    first_event_ts    timestamptz,
    last_event_ts     timestamptz,
    rebuilt_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_ecy_cfs_chain_container UNIQUE (container_number)
);
CREATE INDEX IF NOT EXISTS idx_chain_status ON core.ecy_cfs_chain (chain_status);
CREATE INDEX IF NOT EXISTS idx_chain_anomaly ON core.ecy_cfs_chain (has_anomaly)
    WHERE has_anomaly IS TRUE;
CREATE INDEX IF NOT EXISTS idx_chain_cfs_out ON core.ecy_cfs_chain (cfs_out_ts DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_chain_cycle ON core.ecy_cfs_chain (cycle_hours DESC NULLS LAST);

COMMIT;
