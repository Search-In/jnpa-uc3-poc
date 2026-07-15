-- Pre-deployment gate: every ACTIVE driver must resolve to an existing fleet
-- vehicle. This is the exact verification query from the deployment audit — it
-- MUST return ZERO rows before deploy.
--
-- Run:  psql "$POSTGRES_DSN" -f scripts/verify_fleet_assignments.sql
--
-- If it returns rows, run the backfill (idempotent):
--   psql "$POSTGRES_DSN" -f infra/postgres/migrations/0022_fleet_assignment_backfill.sql
-- (the gateway also backfills on boot — gateway/fleet.py::sync_from_assignments).

SELECT
    d.driver_id,
    d.name,
    d.vehicle_no_norm
FROM jnpa.drivers d
LEFT JOIN jnpa.fleet_vehicles f
    ON d.vehicle_no_norm = f.vehicle_id
WHERE f.vehicle_id IS NULL;
