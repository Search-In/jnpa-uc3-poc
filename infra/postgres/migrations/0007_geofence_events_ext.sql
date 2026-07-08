-- ===========================================================================
-- Migration 0007 — extend jnpa.geofence_events for production enforcement.
-- Adds driver_id, event_type (ENTER/EXIT/DWELL/NO_PARKING_VIOLATION/RESTRICTED_ENTRY)
-- and dwell_seconds to the geofence_events table created in 0003. Additive +
-- idempotent (ADD COLUMN IF NOT EXISTS); never touches existing data. The gateway
-- geofence engine (gateway/geofence.py) applies the same ALTERs at runtime.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0007_geofence_events_ext.sql
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

ALTER TABLE jnpa.geofence_events ADD COLUMN IF NOT EXISTS driver_id     text;
ALTER TABLE jnpa.geofence_events ADD COLUMN IF NOT EXISTS event_type    text;
ALTER TABLE jnpa.geofence_events ADD COLUMN IF NOT EXISTS dwell_seconds integer;

CREATE INDEX IF NOT EXISTS idx_geofence_events_type ON jnpa.geofence_events (event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_geofence_events_driver ON jnpa.geofence_events (driver_id, created_at DESC);
