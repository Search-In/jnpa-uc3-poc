-- ===========================================================================
-- Vehicle Master (fleet registry) seed — jnpa.fleet_vehicles.
--
-- Backfills the Vehicle Master so the enterprise flow works even when the
-- truck-sim is not running (the gateway also migrates the sim fleet in on boot,
-- idempotently — this script is the static equivalent for DB-only setups).
--
-- Guarantees existing assigned drivers keep working: TRK-000001 / TRK-000002 are
-- the vehicles seeded by scripts/seed_production_drivers.sql, so seeding them here
-- as ACTIVE means their already-active drivers still pass the PWA-login gate.
--
-- IDEMPOTENT: ON CONFLICT (vehicle_id) DO NOTHING never clobbers an operator edit.
-- Run:  psql "$POSTGRES_DSN" -f scripts/seed_fleet_vehicles.sql
-- ===========================================================================

CREATE SCHEMA IF NOT EXISTS jnpa;

-- Known plates from the production driver seed (device -> plate binding).
INSERT INTO jnpa.fleet_vehicles (vehicle_id, vehicle_number, vehicle_type, status, created_by)
VALUES
    ('TRK-000001', 'MH04KN3106', 'Container Truck', 'ACTIVE', 'system:seed'),
    ('TRK-000002', 'MH43SV7025', 'Container Truck', 'ACTIVE', 'system:seed')
ON CONFLICT (vehicle_id) DO NOTHING;

-- A block of unassigned ACTIVE vehicles so the "assign vehicle" dropdown has
-- real options (TRK-000003 .. TRK-000030). Plates are synthetic/deterministic.
INSERT INTO jnpa.fleet_vehicles (vehicle_id, vehicle_number, vehicle_type, status, created_by)
SELECT
    'TRK-' || lpad(g::text, 6, '0'),
    'MH04' || chr(65 + (g % 26)) || chr(65 + ((g * 7) % 26)) || lpad(((g * 137) % 10000)::text, 4, '0'),
    'Container Truck',
    'ACTIVE',
    'system:seed'
FROM generate_series(3, 30) AS g
ON CONFLICT (vehicle_id) DO NOTHING;
