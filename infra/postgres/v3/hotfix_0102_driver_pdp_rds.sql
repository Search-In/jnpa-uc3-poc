-- ============================================================================
-- hotfix_0102_driver_pdp_rds.sql — remaining 0102 drift on RDS jnpa_schema_v3
-- Found 2026-07-31: core.driver has MOST 0102 columns (id, licence_no_norm,
-- transporter_id, status, photo_url, licence_valid_to) but lacks
-- enrolled_driver_id / source_srno / created_at / updated_at, all six 0102
-- indexes, the uq_driver_ext_id constraint and the updated_at trigger.
-- core.pdp lacks cancellation_time / created_at and pdp_id is integer
-- (0102: bigint).
--
-- MIGRATION PLAN
--   1. Additive driver columns: DEFAULT now() is STABLE -> metadata-only, no
--      rewrite, instant on 31,846 rows.
--   2. Indexes: CREATE INDEX IF NOT EXISTS (plain; table is small — no need
--      for CONCURRENTLY at this size).
--   3. core.pdp: ADD COLUMNs (instant) + ALTER pdp_id TYPE bigint — LOSSLESS
--      widen but takes ACCESS EXCLUSIVE and rewrites ~367k rows (seconds).
--      Run when the Driver Master screen is not being demoed.
--   All in ONE transaction: any failure rolls back everything.
--
-- ROLLBACK PLAN
--   Transaction failure auto-rolls-back. To manually revert after COMMIT:
--     ALTER TABLE core.driver DROP COLUMN IF EXISTS enrolled_driver_id,
--       DROP COLUMN IF EXISTS source_srno, DROP COLUMN IF EXISTS created_at,
--       DROP COLUMN IF EXISTS updated_at;
--     DROP INDEX IF EXISTS core.idx_driver_licence_norm, core.idx_driver_name_lower,
--       core.idx_driver_company_lower, core.idx_driver_transporter,
--       core.idx_driver_valid, core.idx_driver_enrolled;
--     ALTER TABLE core.pdp ALTER COLUMN pdp_id TYPE integer;  -- safe: values fit
--     ALTER TABLE core.pdp DROP COLUMN IF EXISTS cancellation_time,
--       DROP COLUMN IF EXISTS created_at;
--
-- VERIFICATION QUERIES (run after)
--   SELECT count(*) FROM core.driver;                       -- must equal pre-count (31846)
--   SELECT count(*) FROM core.pdp;                          -- must equal pre-count (367078)
--   SELECT data_type FROM information_schema.columns
--    WHERE table_schema='core' AND table_name='pdp' AND column_name='pdp_id';  -- bigint
--   SELECT count(*) FROM pg_indexes WHERE schemaname='core'
--     AND tablename='driver' AND indexname LIKE 'idx_driver_%';                -- 6
--   curl localhost:8000/api/drivers/master?limit=1          -- still 200 with rows
-- ============================================================================
BEGIN;

-- ------------------------------------------------------------------- driver
ALTER TABLE core.driver
    ADD COLUMN IF NOT EXISTS enrolled_driver_id text,
    ADD COLUMN IF NOT EXISTS source_srno        bigint,
    ADD COLUMN IF NOT EXISTS created_at         timestamptz DEFAULT now() NOT NULL,
    ADD COLUMN IF NOT EXISTS updated_at         timestamptz DEFAULT now() NOT NULL;

DO $$ BEGIN
    ALTER TABLE core.driver ADD CONSTRAINT uq_driver_ext_id UNIQUE (id);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    ALTER TABLE core.driver ADD CONSTRAINT driver_status_check
        CHECK (status IN ('ACTIVE','INACTIVE'));
EXCEPTION WHEN duplicate_object THEN NULL;
          WHEN check_violation THEN RAISE; END $$;

CREATE INDEX IF NOT EXISTS idx_driver_licence_norm  ON core.driver (licence_no_norm);
CREATE INDEX IF NOT EXISTS idx_driver_name_lower    ON core.driver (lower(driver_name));
CREATE INDEX IF NOT EXISTS idx_driver_company_lower ON core.driver (lower(coalesce(company_name,'')));
CREATE INDEX IF NOT EXISTS idx_driver_transporter   ON core.driver (transporter_id);
CREATE INDEX IF NOT EXISTS idx_driver_valid         ON core.driver (licence_valid_to);
CREATE INDEX IF NOT EXISTS idx_driver_enrolled      ON core.driver (enrolled_driver_id);

DO $$ BEGIN
    CREATE TRIGGER trg_driver_updated_at BEFORE UPDATE ON core.driver
        FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- --------------------------------------------------------------------- pdp
ALTER TABLE core.pdp
    ADD COLUMN IF NOT EXISTS cancellation_time timestamptz,
    ADD COLUMN IF NOT EXISTS created_at        timestamptz DEFAULT now();

-- Lossless widen; rewrites ~367k rows under ACCESS EXCLUSIVE (seconds).
ALTER TABLE core.pdp ALTER COLUMN pdp_id TYPE bigint;

COMMIT;
