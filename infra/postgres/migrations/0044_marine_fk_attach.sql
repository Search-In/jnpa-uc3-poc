-- 0044_marine_fk_attach.sql — UC-I Marine: re-attach the FKs migration 0038 deferred.
-- Additive + idempotent.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0044_marine_fk_attach.sql
--
-- Migration 0038 created core.vessel_call / core.vessel_call_event with five FK columns
-- as plain columns, because their parents did not exist yet (see 0038 ledger [D2]). Now
-- that 0039 (ingest_file), 0040 (ref_terminal, ref_berth) and 0041 (vessel) exist, this
-- migration attaches the five constraints:
--   core.vessel_call.imo_no            -> core.vessel      (imo_no)        [0041]
--   core.vessel_call.terminal_id       -> core.ref_terminal(terminal_id)  [0040]
--   core.vessel_call.berth_id          -> core.ref_berth   (berth_id)     [0040]
--   core.vessel_call_event.berth_id    -> core.ref_berth   (berth_id)     [0040]
--   core.vessel_call_event.source_file -> core.ingest_file (file_id)      [0039]
--
-- This is the ONLY migration that ALTERs a table it does not create. It alters ONLY the
-- two core.vessel_call* tables (owned by 0038) and touches NOTHING in jnpa.
--
-- IDEMPOTENT: ADD CONSTRAINT has no IF NOT EXISTS, so each is wrapped in a DO block that
-- (a) skips if the parent table is absent (to_regclass guard — the ensure_marine_schema
-- boot path is order-safe, but this keeps a partial DB from erroring), and (b) skips if
-- the constraint already exists (pg_constraint lookup). Re-running is a no-op. This is
-- the same DO-block guard idiom used in gateway/performance_ext.py.
--
-- NON-BREAKING: each FK is attached NOT VALID — it enforces referential integrity on all
-- NEW writes immediately, but does NOT scan existing rows at attach time, so attaching
-- can never fail on legacy data. core.vessel_call* are empty today, so this is currently
-- a formality; it becomes load-bearing once marine ingestion starts. A follow-up may run
-- `ALTER TABLE ... VALIDATE CONSTRAINT ...` after ingestion resolves every FK value to a
-- seeded dimension key (that VALIDATE is intentionally NOT run here — validation belongs
-- after the reference dimensions are fully populated).
--
-- ROLLBACK:
--   ALTER TABLE core.vessel_call       DROP CONSTRAINT IF EXISTS fk_vessel_call_imo;
--   ALTER TABLE core.vessel_call       DROP CONSTRAINT IF EXISTS fk_vessel_call_terminal;
--   ALTER TABLE core.vessel_call       DROP CONSTRAINT IF EXISTS fk_vessel_call_berth;
--   ALTER TABLE core.vessel_call_event DROP CONSTRAINT IF EXISTS fk_vessel_call_event_berth;
--   ALTER TABLE core.vessel_call_event DROP CONSTRAINT IF EXISTS fk_vessel_call_event_source_file;

DO $$
BEGIN
    IF to_regclass('core.vessel') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint
                       WHERE conname = 'fk_vessel_call_imo'
                         AND conrelid = 'core.vessel_call'::regclass) THEN
        ALTER TABLE core.vessel_call
            ADD CONSTRAINT fk_vessel_call_imo
            FOREIGN KEY (imo_no) REFERENCES core.vessel (imo_no) NOT VALID;
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('core.ref_terminal') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint
                       WHERE conname = 'fk_vessel_call_terminal'
                         AND conrelid = 'core.vessel_call'::regclass) THEN
        ALTER TABLE core.vessel_call
            ADD CONSTRAINT fk_vessel_call_terminal
            FOREIGN KEY (terminal_id) REFERENCES core.ref_terminal (terminal_id) NOT VALID;
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('core.ref_berth') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint
                       WHERE conname = 'fk_vessel_call_berth'
                         AND conrelid = 'core.vessel_call'::regclass) THEN
        ALTER TABLE core.vessel_call
            ADD CONSTRAINT fk_vessel_call_berth
            FOREIGN KEY (berth_id) REFERENCES core.ref_berth (berth_id) NOT VALID;
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('core.ref_berth') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint
                       WHERE conname = 'fk_vessel_call_event_berth'
                         AND conrelid = 'core.vessel_call_event'::regclass) THEN
        ALTER TABLE core.vessel_call_event
            ADD CONSTRAINT fk_vessel_call_event_berth
            FOREIGN KEY (berth_id) REFERENCES core.ref_berth (berth_id) NOT VALID;
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('core.ingest_file') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint
                       WHERE conname = 'fk_vessel_call_event_source_file'
                         AND conrelid = 'core.vessel_call_event'::regclass) THEN
        ALTER TABLE core.vessel_call_event
            ADD CONSTRAINT fk_vessel_call_event_source_file
            FOREIGN KEY (source_file) REFERENCES core.ingest_file (file_id) NOT VALID;
    END IF;
END $$;
