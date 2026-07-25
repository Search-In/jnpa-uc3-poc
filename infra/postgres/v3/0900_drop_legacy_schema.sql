-- ============================================================================
-- 0900  Post-verification cleanup: remove the COPIED legacy schema from the
--       target database (jnpa_schema_v3).
--
-- Run ONLY after:
--   * 0201/0202/0203 backfills completed with verified parity, and
--   * the deployed backend has passed production verification on the target.
--
-- The original legacy data in database jnpa3 is NOT touched by this script —
-- it remains intact as the rollback reference.
-- ============================================================================

DO $$
BEGIN
    IF current_database() <> 'jnpa_schema_v3' THEN
        RAISE EXCEPTION 'refusing: connected to %, expected jnpa_schema_v3',
            current_database();
    END IF;
END $$;

DROP SCHEMA IF EXISTS jnpa CASCADE;
