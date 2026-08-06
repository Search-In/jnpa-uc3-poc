-- ============================================================================
-- 0128  core.cargo.evacuation_mode — per-container RAIL / ROAD attribution.
--
-- JNPA What-If Notice (05 Aug 2026) scenario II-A asks what happens when "twenty
-- per cent of containers currently evacuated by rail are moved to road". The
-- audit proved that question was unanswerable: an individual container could not
-- be labelled RAIL or ROAD anywhere in the schema.
--
--   * core.perf_ldb_route_movement.transport_mode is a MONTHLY percentage share,
--     not a per-box fact.
--   * core.cargo_rake_plan.containers is a jsonb array with no FK, so the rail
--     side could not be joined to core.cargo at all.
--
-- This migration adds the attribute and backfills it from the evidence already
-- in the database. `evacuation_mode_source` records WHERE each value came from,
-- so the simulation layer can declare a derived value as an assumption instead of
-- presenting it as a measured one (Notice §1.c).
--
-- Additive: the column is NULLable with no default, so every existing row stays
-- valid and every existing query is unaffected.
-- ============================================================================
BEGIN;

ALTER TABLE core.cargo ADD COLUMN IF NOT EXISTS evacuation_mode text;
ALTER TABLE core.cargo ADD COLUMN IF NOT EXISTS evacuation_mode_source text;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'cargo_evacuation_mode_check') THEN
        ALTER TABLE core.cargo ADD CONSTRAINT cargo_evacuation_mode_check
            CHECK (evacuation_mode IS NULL OR
                   evacuation_mode IN ('RAIL','ROAD','UNKNOWN'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'cargo_evacuation_mode_source_check') THEN
        ALTER TABLE core.cargo ADD CONSTRAINT cargo_evacuation_mode_source_check
            CHECK (evacuation_mode_source IS NULL OR
                   evacuation_mode_source IN ('RAKE_PLAN','JOB_ASSIGNMENT',
                                              'LDB_MOVEMENT','DECLARED','ASSUMED'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_cargo_evacuation_mode
    ON core.cargo (evacuation_mode, lifecycle_status)
    WHERE evacuation_mode IS NOT NULL;

-- ------------------------------------------------------------------ backfill
-- Ordered weakest-evidence-first so the strongest source wins the final UPDATE.
--
-- 3. LDB movement mode (weakest: a corridor observation, not an evacuation plan)
UPDATE core.cargo c
   SET evacuation_mode = CASE WHEN upper(m.mode) IN ('RAIL','TRAIN') THEN 'RAIL'
                              WHEN upper(m.mode) IN ('ROAD','TRUCK') THEN 'ROAD' END,
       evacuation_mode_source = 'LDB_MOVEMENT'
  FROM (SELECT DISTINCT ON (container_number) container_number, mode
          FROM core.ldb_movement
         WHERE mode IS NOT NULL
         ORDER BY container_number, ts DESC) m
 WHERE m.container_number = c.container_number
   AND upper(m.mode) IN ('RAIL','TRAIN','ROAD','TRUCK')
   AND c.evacuation_mode IS NULL;

-- 2. A truck job assignment is direct evidence the box leaves by road.
UPDATE core.cargo c
   SET evacuation_mode = 'ROAD',
       evacuation_mode_source = 'JOB_ASSIGNMENT'
 WHERE EXISTS (SELECT 1 FROM core.container_job_assignment j
                WHERE j.container_number = c.container_number
                  AND j.move_type IN ('IMPORT_PICK','EXPORT_DROP')
                  AND j.status <> 'CANCELLED');

-- 1. A rake plan is direct evidence the box leaves by rail (strongest signal).
UPDATE core.cargo c
   SET evacuation_mode = 'RAIL',
       evacuation_mode_source = 'RAKE_PLAN'
 WHERE EXISTS (SELECT 1 FROM core.cargo_rake_plan p
                WHERE p.containers @> to_jsonb(c.container_number));

-- Everything still unlabelled is honestly UNKNOWN — NOT silently defaulted to
-- ROAD. The simulation reports the unknown share rather than assuming it away.
UPDATE core.cargo
   SET evacuation_mode = 'UNKNOWN', evacuation_mode_source = 'ASSUMED'
 WHERE evacuation_mode IS NULL;

COMMENT ON COLUMN core.cargo.evacuation_mode IS
    'How the container leaves the port: RAIL | ROAD | UNKNOWN. Backfilled by '
    'migration 0128 from rake plans, job assignments and LDB movements.';
COMMENT ON COLUMN core.cargo.evacuation_mode_source IS
    'Provenance of evacuation_mode. Anything other than DECLARED is DERIVED and '
    'must be surfaced as an assumption by the what-if layer (JNPA Notice 1.c).';

COMMIT;
