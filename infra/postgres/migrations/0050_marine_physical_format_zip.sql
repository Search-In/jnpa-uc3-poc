-- 0050_marine_physical_format_zip.sql — UC-I Marine: allow ZIP/SHP uploads in the ledger.
-- Additive + idempotent.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0050_marine_physical_format_zip.sql
--
-- The sea-channel upload is a zipped ESRI shapefile (JNPA_Sea_Channels.zip); the ledger
-- stores its actual container format, 'ZIP' (a rare bare .shp stores 'SHP'). The
-- physical_format CHECK on core.marine_import_files did not include these, so the ledger
-- insert failed. This widens the CHECK to add 'ZIP' and 'SHP'. Parser detection/routing
-- ('SHP') is unchanged — this is purely the ledger's stored metadata.
--
-- STRICTLY ADDITIVE. Only widens one CHECK on core.marine_import_files; existing rows
-- (CSV/XML/XLSX/PDF/LOG) remain valid, and VESPRO/CALINF/Pilot/Port-Craft uploads are
-- unaffected. Touches NOTHING in jnpa. Idempotent: the guarded DO block re-adds the
-- constraint only if it does not already permit 'ZIP', so a re-run is a no-op.
--
-- ROLLBACK: restore the previous CHECK (CSV/XLS/XLSX/PDF/XML/LOG) after ensuring no
--           ZIP/SHP rows exist.

CREATE SCHEMA IF NOT EXISTS core;

DO $$
DECLARE v_conname text;
BEGIN
    SELECT c.conname INTO v_conname
    FROM pg_constraint c
    WHERE c.conrelid = 'core.marine_import_files'::regclass
      AND c.contype = 'c'
      AND pg_get_constraintdef(c.oid) ILIKE '%physical_format%'
      AND pg_get_constraintdef(c.oid) NOT ILIKE '%ZIP%';
    IF v_conname IS NOT NULL THEN
        EXECUTE format('ALTER TABLE core.marine_import_files DROP CONSTRAINT %I', v_conname);
        ALTER TABLE core.marine_import_files
            ADD CONSTRAINT marine_import_files_physical_format_check
            CHECK (physical_format IN ('CSV','XLS','XLSX','PDF','XML','LOG','ZIP','SHP'));
    END IF;
END $$;
