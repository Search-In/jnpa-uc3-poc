-- =====================================================================
-- 0029_perf_vessel_unique_via.sql  —  UC-III Module 12 hardening
-- =====================================================================
-- ADDITIVE, non-destructive upgrade of the jnpa.perf_daily_vessels UNIQUE key.
--
-- Migration 0028 shipped:
--     CONSTRAINT uq_perf_daily_vessel UNIQUE (report_date, terminal_code, berth_no)
-- That key silently collapses a same-day BERTH TURNOVER — when two distinct
-- vessels call the same berth on one report date (each with its own via_no /
-- voyage id), the second row is dropped on import. The correct key adds via_no:
--     UNIQUE (report_date, terminal_code, berth_no, via_no)
--
-- CREATE TABLE IF NOT EXISTS in 0028 does NOT alter an already-existing table, so
-- databases that already ran 0028 keep the old 3-column constraint. This migration
-- upgrades those databases in place. It:
--   1. Inspects the CURRENT column set of the uq_perf_daily_vessel constraint
--      (by conname on jnpa.perf_daily_vessels) — never guesses.
--   2. Drops it ONLY if it is exactly the old 3-column key.
--   3. (Re)creates the correct 4-column key only when it is not already present.
--   4. Preserves all existing rows (the new key is a strict superset of the old,
--      so no row can violate it — the ADD never fails on existing data).
--   5. Is safe to run multiple times (fully idempotent — no-op once upgraded).
--   6. Is a no-op on a FRESH database where gateway/performance_ext.py already
--      created the 4-column constraint.
--
-- Touches nothing else — no other table, module, auth/JWT/RBAC, or data.
--
-- Apply:
--   psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0029_perf_vessel_unique_via.sql
-- (also re-applied idempotently at gateway boot via
--  gateway/performance_ext.ensure_performance_schema, which upgrades the same way)
-- =====================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

DO $$
DECLARE
    v_cols text;
BEGIN
    -- Nothing to do if the table itself does not exist yet (0028 not applied):
    -- 0028 runs first in the numbered sequence and will create it.
    IF to_regclass('jnpa.perf_daily_vessels') IS NULL THEN
        RAISE NOTICE '0029: jnpa.perf_daily_vessels absent — skipping (0028 will create it)';
        RETURN;
    END IF;

    -- Ordered column list of the current uq_perf_daily_vessel constraint (if any).
    SELECT string_agg(a.attname, ',' ORDER BY k.ord)
      INTO v_cols
    FROM pg_constraint c
    JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
    WHERE c.conname  = 'uq_perf_daily_vessel'
      AND c.conrelid = 'jnpa.perf_daily_vessels'::regclass
      AND c.contype  = 'u';

    IF v_cols = 'report_date,terminal_code,berth_no,via_no' THEN
        RAISE NOTICE '0029: uq_perf_daily_vessel already includes via_no — no change';

    ELSIF v_cols = 'report_date,terminal_code,berth_no' THEN
        RAISE NOTICE '0029: upgrading uq_perf_daily_vessel to include via_no';
        ALTER TABLE jnpa.perf_daily_vessels DROP CONSTRAINT uq_perf_daily_vessel;
        ALTER TABLE jnpa.perf_daily_vessels
            ADD CONSTRAINT uq_perf_daily_vessel
            UNIQUE (report_date, terminal_code, berth_no, via_no);

    ELSIF v_cols IS NULL THEN
        -- Constraint missing entirely (e.g. table created without it) — add it.
        RAISE NOTICE '0029: uq_perf_daily_vessel absent — creating with via_no';
        ALTER TABLE jnpa.perf_daily_vessels
            ADD CONSTRAINT uq_perf_daily_vessel
            UNIQUE (report_date, terminal_code, berth_no, via_no);

    ELSE
        -- Some other unexpected column set — leave it alone rather than guess.
        RAISE NOTICE '0029: uq_perf_daily_vessel has unexpected columns (%) — left unchanged', v_cols;
    END IF;
END $$;
