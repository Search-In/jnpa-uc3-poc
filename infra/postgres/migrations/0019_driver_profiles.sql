-- 0019_driver_profiles.sql
-- Admin-created driver profiles + vehicle-assignment integrity for PWA login.
--
-- Background: driver identities are created two ways today — the Driver PWA
-- self-submits an enrollment (jnpa.driver_enrollments, source PWA) which an admin
-- approves and promotes into jnpa.drivers. This migration lets a Control-Room
-- admin ALSO create a driver profile directly from the Driver Enrollment page and
-- assign it a Vehicle ID, then run it through the SAME approval workflow. No new
-- driver table is introduced — the existing enrollment/driver tables are reused;
-- this migration only adds provenance columns + the integrity constraint that the
-- PWA login gate depends on.
--
-- Additive only (ALTER ... ADD COLUMN IF NOT EXISTS). The gateway also
-- self-provisions the provenance columns at runtime (enrollment.py::_DDL /
-- ensure_profile_columns), mirroring the otp.py / push.py pattern, so they are
-- present even against an already-initialised volume where migrations are not
-- re-run.

CREATE SCHEMA IF NOT EXISTS jnpa;

-- --------------------------------------------------------------------------
-- Provenance: who created the record and where it came from.
--   source     = 'PWA'   -> driver self-submitted from the mobile app (default,
--                            preserves existing rows)
--              = 'ADMIN' -> created by a Control-Room admin on the enrollment page
--   created_by = actor id of the admin who created it ("<role>:<sub>")
-- --------------------------------------------------------------------------
ALTER TABLE jnpa.driver_enrollments
    ADD COLUMN IF NOT EXISTS created_by text;
ALTER TABLE jnpa.driver_enrollments
    ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'PWA';

ALTER TABLE jnpa.drivers
    ADD COLUMN IF NOT EXISTS created_by text;
-- Normalised (UPPER, trimmed) assigned Vehicle ID. This is the column the PWA
-- login gate matches the entered Vehicle ID against, and the column the
-- one-active-driver-per-vehicle constraint is enforced on.
ALTER TABLE jnpa.drivers
    ADD COLUMN IF NOT EXISTS vehicle_no_norm text;

-- Backfill the normalised column for any pre-existing driver rows.
UPDATE jnpa.drivers
    SET vehicle_no_norm = UPPER(TRIM(vehicle_no))
    WHERE vehicle_no_norm IS NULL AND vehicle_no IS NOT NULL AND TRIM(vehicle_no) <> '';

-- --------------------------------------------------------------------------
-- Fast lookup for the PWA login gate: entered Vehicle ID -> active driver.
-- --------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_drivers_vehicle_no
    ON jnpa.drivers (vehicle_no);
CREATE INDEX IF NOT EXISTS idx_drivers_vehicle_no_norm
    ON jnpa.drivers (vehicle_no_norm);

-- --------------------------------------------------------------------------
-- Integrity: at most ONE ACTIVE driver per vehicle. This is what makes an
-- assigned Vehicle ID an authoritative, unambiguous login credential — two
-- ACTIVE drivers can never claim the same vehicle. Partial unique index so
-- SUSPENDED / historical rows do not block reassigning the vehicle.
-- --------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_drivers_vehicle_active
    ON jnpa.drivers (vehicle_no_norm)
    WHERE status = 'ACTIVE' AND vehicle_no_norm IS NOT NULL;
