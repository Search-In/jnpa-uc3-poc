-- ===========================================================================
-- Migration 0014 — Vehicle-Intelligence read-path indexes.
--
-- The /api/vahan/vehicle-intel/{plate} aggregate (gateway/vehicle_intel.py) fans
-- out per-plate lookups across several tables. Two of them had no supporting
-- index and fell back to sequential scans as the tables grew:
--
--   * jnpa.challans WHERE vehicle_number ORDER BY issued_at DESC   (only case_id was indexed)
--   * jnpa.alerts   WHERE plate          ORDER BY ts DESC          (only ts was indexed)
--
-- These two composite indexes make both lookups index-only range scans, matching
-- the pattern already used for violation_cases (vehicle_number, first_detected_at)
-- and truck_telemetry (plate, ts). Additive + idempotent. No data change.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0014_vehicle_intel_indexes.sql
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

CREATE INDEX IF NOT EXISTS idx_challans_vehicle
    ON jnpa.challans (vehicle_number, issued_at DESC);

CREATE INDEX IF NOT EXISTS idx_alerts_plate
    ON jnpa.alerts (plate, ts DESC);
