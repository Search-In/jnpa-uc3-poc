-- ===========================================================================
-- Migration 0012 — align jnpa.gates display coordinates to the JNPA satellite
-- reference.
--
-- init.sql (commit f4cedb7) moved the 4 gate markers onto the developed berth
-- centroids (methodology + values from jnpa_poc_2 config/terminals.json). But
-- init.sql only runs on a FRESH Postgres volume, so any already-provisioned
-- database still carries the pre-fix coordinates (gates ~400 m off the berth).
-- This migration re-points the display coordinates on an existing database.
--
-- Display coordinates ONLY: gate throughput / utilisation join on
-- jnpa.cameras.gate_id (not coordinates), the truck simulator's routing coords
-- live in trucking_app/gates.py, and gate cameras are not rendered as map
-- markers — so none of those are touched here.
--
-- Idempotent (UPDATE by id; re-running is a no-op once values match) and scoped
-- to the 4 known gate ids. Never inserts, never deletes.
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0012_gate_coords.sql
-- ===========================================================================
SET search_path TO jnpa, public;

UPDATE jnpa.gates SET lat = 18.9527, lon = 72.9505 WHERE id = 'G-NSICT';
UPDATE jnpa.gates SET lat = 18.9497, lon = 72.9479 WHERE id = 'G-JNPCT';
UPDATE jnpa.gates SET lat = 18.9550, lon = 72.9525 WHERE id = 'G-NSIGT';
UPDATE jnpa.gates SET lat = 18.9386, lon = 72.9383 WHERE id = 'G-BMCT';
