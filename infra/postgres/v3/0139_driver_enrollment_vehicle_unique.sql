-- 0139 — one open enrollment per vehicle (the missing half of "one vehicle, one driver").
--
-- core.driver_identity has enforced uq_drivers_vehicle_active since day one, so
-- two ACTIVE drivers can never hold the same truck. core.driver_enrollment had
-- no equivalent: POST /api/identity/drivers checked the conflict with a SELECT
-- and then INSERTed, so two admins (or an admin and a PWA self-enrolment) could
-- both pass the check and both land a PENDING enrollment on the same vehicle.
-- Nothing failed until approval time, by which point both operators believed
-- their profile was created and the loser got a 409 they could not act on.
--
-- The predicate mirrors gateway/enrollment.py exactly:
--   * open states = PENDING / REENROLL (_OPEN_ENROL_STATES)
--   * key        = UPPER(TRIM(vehicle_no)) (normalize_vehicle_no)
-- so the index and the application check can never disagree.
CREATE UNIQUE INDEX IF NOT EXISTS uq_driver_enrol_vehicle_open
    ON core.driver_enrollment (UPPER(TRIM(vehicle_no)))
    WHERE status IN ('PENDING', 'REENROLL')
      AND vehicle_no IS NOT NULL
      AND TRIM(vehicle_no) <> '';
