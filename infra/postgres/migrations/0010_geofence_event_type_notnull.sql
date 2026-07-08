-- ===========================================================================
-- Migration 0010 — enforce a mandatory jnpa.geofence_events.event_type.
--
-- P0 production-readiness fix. Historically some writers (the append-only audit
-- helper gateway/audit.py::record_geofence_event) inserted rows WITHOUT an
-- event_type, so GET /api/geo/events surfaced blank types. This migration:
--
--   1. Backfills every NULL/'' event_type by deriving it from the row
--      (violation_type / exit_time / dwell_seconds / entry_time) into the
--      canonical set {ENTER, EXIT, DWELL, NO_PARKING_VIOLATION, RESTRICTED_ENTRY}.
--   2. Installs a BEFORE INSERT/UPDATE trigger that fills event_type the same way
--      whenever a writer omits it — so ALL future rows are valid WITHOUT editing
--      the audit-framework code (the trigger runs regardless of the caller).
--   3. Sets the column NOT NULL (safe: the BEFORE trigger fires first, so an
--      omitted event_type is filled before the NOT NULL check).
--
-- Additive + idempotent. Does NOT modify the audit framework tables/logic.
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0010_geofence_event_type_notnull.sql
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

-- 1. Backfill existing NULL / empty event_type rows -------------------------
UPDATE jnpa.geofence_events
SET event_type = CASE
    WHEN violation_type IS NOT NULL AND violation_type <> '' THEN violation_type
    WHEN exit_time IS NOT NULL                                THEN 'EXIT'
    WHEN COALESCE(dwell_seconds, 0) > 0                       THEN 'DWELL'
    WHEN entry_time IS NOT NULL                               THEN 'ENTER'
    ELSE 'ENTER'
END
WHERE event_type IS NULL OR event_type = '';

-- 2. Derivation trigger — guarantees a non-null event_type for every writer --
CREATE OR REPLACE FUNCTION jnpa.geofence_events_default_event_type()
RETURNS trigger AS $$
BEGIN
    IF NEW.event_type IS NULL OR NEW.event_type = '' THEN
        NEW.event_type := CASE
            WHEN NEW.violation_type IS NOT NULL AND NEW.violation_type <> '' THEN NEW.violation_type
            WHEN NEW.exit_time IS NOT NULL                                   THEN 'EXIT'
            WHEN COALESCE(NEW.dwell_seconds, 0) > 0                          THEN 'DWELL'
            WHEN NEW.entry_time IS NOT NULL                                  THEN 'ENTER'
            ELSE 'ENTER'
        END;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_geofence_events_event_type ON jnpa.geofence_events;
CREATE TRIGGER trg_geofence_events_event_type
    BEFORE INSERT OR UPDATE ON jnpa.geofence_events
    FOR EACH ROW EXECUTE FUNCTION jnpa.geofence_events_default_event_type();

-- 3. Enforce mandatory presence at the schema level -------------------------
ALTER TABLE jnpa.geofence_events ALTER COLUMN event_type SET NOT NULL;
