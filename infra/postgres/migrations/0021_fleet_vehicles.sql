-- 0021_fleet_vehicles.sql
-- Vehicle Master (fleet registry) — the authoritative list of vehicles a driver
-- may be assigned to.
--
-- Naming note: jnpa.vehicle_master already exists as the Vahan RC-lookup cache
-- (keyed by plate); it is unrelated and untouched. This new registry is keyed by
-- the fleet Vehicle ID (TRK-000123) and drives the "assign vehicle" dropdown +
-- the PWA-login vehicle-existence guarantee.
--
-- Additive only. The gateway also self-provisions this table at runtime
-- (gateway/fleet.py::_DDL) and migrates the truck-sim fleet into it on boot
-- (gateway/fleet.py::sync_from_fleet), mirroring the enrollment.py pattern, so it
-- is present + populated even against an already-initialised volume.

CREATE SCHEMA IF NOT EXISTS jnpa;
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

CREATE TABLE IF NOT EXISTS jnpa.fleet_vehicles (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id      text NOT NULL UNIQUE,
    vehicle_number  text,
    vehicle_type    text,
    chassis_number  text,
    rfid_fastag_id  text,
    status          text NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE', 'INACTIVE', 'MAINTENANCE')),
    created_by      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_fleet_vehicles_vehicle_id ON jnpa.fleet_vehicles (vehicle_id);
CREATE INDEX IF NOT EXISTS idx_fleet_vehicles_number ON jnpa.fleet_vehicles (vehicle_number);
CREATE INDEX IF NOT EXISTS idx_fleet_vehicles_status ON jnpa.fleet_vehicles (status);
