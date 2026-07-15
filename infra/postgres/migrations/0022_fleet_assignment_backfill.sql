-- 0022_fleet_assignment_backfill.sql
-- Deployment blocker fix: reconcile jnpa.fleet_vehicles with EXISTING driver
-- assignments.
--
-- Canonical relationship: jnpa.drivers.vehicle_no_norm  ==  jnpa.fleet_vehicles.vehicle_id
-- (the same key the PWA login gate matches on). The Vehicle Master was seeded
-- ONLY from truck-sim, so assignments that came from elsewhere (admin-created
-- plates, non-sim TRK ids) had no fleet row -> the LEFT JOIN audit returned
-- orphaned ACTIVE drivers.
--
-- This backfills a fleet row for every assigned Vehicle ID that is missing, so no
-- ACTIVE driver is left dangling. It NEVER touches jnpa.drivers — assignments,
-- PWA login and JWTs are unchanged; it only ADDS the missing vehicles the
-- assignments already point at. Idempotent (ON CONFLICT DO NOTHING) and mirrored
-- at runtime by gateway/fleet.py::sync_from_assignments (boot migration).

CREATE SCHEMA IF NOT EXISTS jnpa;

-- Backfill: one fleet vehicle per distinct assigned Vehicle ID not already present.
--  * TRK-shaped id -> use the driver's original vehicle_no as the plate when it is
--    itself a plate (not another TRK id), else leave the number null.
--  * plate-shaped id (the Vehicle ID *is* a plate) -> store it as the number too.
INSERT INTO jnpa.fleet_vehicles
    (vehicle_id, vehicle_number, vehicle_type, status, created_by)
SELECT DISTINCT ON (d.vehicle_no_norm)
    d.vehicle_no_norm,
    CASE
        WHEN d.vehicle_no_norm ~ '^TRK-[0-9]{6}$'
            THEN CASE WHEN UPPER(TRIM(d.vehicle_no)) ~ '^TRK-[0-9]{6}$'
                      THEN NULL ELSE NULLIF(TRIM(d.vehicle_no), '') END
        ELSE d.vehicle_no_norm
    END,
    'Container Truck',
    'ACTIVE',
    'system:assignment-backfill'
FROM jnpa.drivers d
LEFT JOIN jnpa.fleet_vehicles f ON f.vehicle_id = d.vehicle_no_norm
WHERE d.vehicle_no_norm IS NOT NULL
  AND TRIM(d.vehicle_no_norm) <> ''
  AND f.vehicle_id IS NULL
-- DISTINCT ON needs the distinct key to lead ORDER BY; prefer an ACTIVE driver's
-- row and a real (non-TRK) plate so the derived vehicle_number is the best available.
ORDER BY d.vehicle_no_norm,
         (d.status = 'ACTIVE') DESC,
         (UPPER(TRIM(d.vehicle_no)) ~ '^TRK-[0-9]{6}$') ASC
ON CONFLICT (vehicle_id) DO NOTHING;

-- Post-condition assertion: the verification query must now return ZERO rows.
DO $$
DECLARE
    n int;
BEGIN
    SELECT count(*) INTO n
    FROM jnpa.drivers d
    LEFT JOIN jnpa.fleet_vehicles f ON d.vehicle_no_norm = f.vehicle_id
    WHERE d.status = 'ACTIVE'
      AND f.vehicle_id IS NULL
      AND d.vehicle_no_norm IS NOT NULL
      AND TRIM(d.vehicle_no_norm) <> '';
    IF n > 0 THEN
        RAISE WARNING '0022 fleet backfill: % ACTIVE driver(s) still without a fleet vehicle', n;
    ELSE
        RAISE NOTICE '0022 fleet backfill: all ACTIVE drivers resolve to a fleet vehicle';
    END IF;
END $$;
