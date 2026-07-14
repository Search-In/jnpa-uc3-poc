-- seed_demo_parking_rich.sql — richer, reproducible parking demo data.
--
-- Idempotent (guarded by detail->>'source' = 'DEMO_RICH' and slot vehicle tags),
-- so it is safe to run repeatedly. Populates every Parking tab with realistic,
-- RDS-backed rows:
--   * Facilities  — spreads OCCUPIED slots across all facilities (not just one)
--   * Vehicles    — ACTIVE parking_transactions for the occupying trucks
--   * History     — a few COMPLETED transactions (entry + exit)
--   * Violations  — ILLEGAL_PARKING / NO_PARKING_VIOLATION / OVERFLOW events that
--                   carry an explicit severity + duration in the detail jsonb, so
--                   the Violations tab shows Duration + Severity columns.
--
-- Run:  psql "$LOCAL_DSN" -f scripts/seed_demo_parking_rich.sql
--       (LOCAL_DSN e.g. postgresql://postgres:TempPass123!@localhost:5434/postgres)

BEGIN;

-- 1) Spread occupancy: OCCUPY ~8% of each facility's slots, tagging the vehicle
--    so the seed is idempotent and the Vehicles tab has rows in every lot.
WITH picks AS (
  SELECT s.id,
         f.id AS facility_id,
         'MH04PK' || lpad((row_number() OVER (PARTITION BY f.id ORDER BY s.id))::text, 4, '0') AS vehicle_id,
         row_number() OVER (PARTITION BY f.id ORDER BY s.id) AS rn,
         greatest(1, (f.capacity * 8) / 100) AS want
  FROM jnpa.parking_facilities f
  JOIN jnpa.parking_slots s ON s.facility_id = f.id
  WHERE s.availability_status = 'AVAILABLE'
)
UPDATE jnpa.parking_slots s
SET availability_status = 'OCCUPIED',
    vehicle_id = p.vehicle_id,
    updated_at = now()
FROM picks p
WHERE s.id = p.id
  AND p.rn <= p.want
  -- only top up facilities that are currently under-occupied (idempotent)
  AND (SELECT count(*) FROM jnpa.parking_slots x
       WHERE x.facility_id = p.facility_id AND x.availability_status = 'OCCUPIED') < p.want;

-- 2) ACTIVE transactions for the freshly-occupied slots (Vehicles tab).
INSERT INTO jnpa.parking_transactions (vehicle_id, driver_id, facility_id, slot_id, entry_time, status)
SELECT s.vehicle_id,
       NULL,
       s.facility_id,
       s.id,
       now() - (make_interval(mins => (15 + (s.id % 90))::int)),
       'ACTIVE'
FROM jnpa.parking_slots s
WHERE s.availability_status = 'OCCUPIED'
  AND s.vehicle_id LIKE 'MH04PK%'
  AND NOT EXISTS (
    SELECT 1 FROM jnpa.parking_transactions t
    WHERE t.slot_id = s.id AND t.status = 'ACTIVE'
  );

-- 3) A few COMPLETED transactions for the History tab (entry + exit + duration).
INSERT INTO jnpa.parking_transactions (vehicle_id, driver_id, facility_id, slot_id, entry_time, exit_time, duration, status)
SELECT v.vehicle_id, NULL, v.facility_id, NULL,
       now() - v.ago, now() - v.ago + v.dur, v.dur, 'COMPLETED'
FROM (VALUES
  ('MH04HX2201', 'PK-NSICT',  interval '6 hour',  interval '52 minute'),
  ('MH04HX2202', 'PK-JNPCT',  interval '5 hour',  interval '38 minute'),
  ('MH04HX2203', 'PK-BMCT',   interval '4 hour',  interval '71 minute'),
  ('MH04HX2204', 'PK-CPP',    interval '3 hour',  interval '25 minute')
) AS v(vehicle_id, facility_id, ago, dur)
WHERE NOT EXISTS (
  SELECT 1 FROM jnpa.parking_transactions t
  WHERE t.vehicle_id = v.vehicle_id AND t.status = 'COMPLETED'
);

-- 4) Violations with explicit severity + duration in detail (Violations tab).
INSERT INTO jnpa.parking_events (event_type, vehicle_id, driver_id, facility_id, slot_id, detail, created_at)
SELECT e.event_type, e.vehicle_id, NULL, e.facility_id, NULL,
       jsonb_build_object(
         'source', 'DEMO_RICH',
         'severity', e.severity,
         'duration_min', e.duration_min,
         'note', e.note
       ),
       now() - e.ago
FROM (VALUES
  -- the task's worked example
  ('ILLEGAL_PARKING',      'TRK-000086', 'PK-CPP',    'High',   45, 'Parked in no-parking zone at Common Parking', interval '2 hour'),
  ('ILLEGAL_PARKING',      'MH04DM5521', 'PK-NSICT',  'High',   62, 'Blocking gate lane at NSICT',                 interval '3 hour'),
  ('NO_PARKING_VIOLATION', 'MH04DM5522', 'PK-BMCT',   'High',   30, 'Stopped in restricted BMCT bay',             interval '5 hour'),
  ('OVERFLOW',             'MH04DM5523', 'PK-HOLDING','Medium', 18, 'Holding yard overflow spill',                interval '90 minute'),
  ('OVERFLOW',             'MH04DM5524', 'PK-JNPCT',  'Medium', 22, 'JNPCT lot at capacity',                      interval '40 minute'),
  ('ILLEGAL_PARKING',      'MH04DM5525', 'PK-NSIGT',  'Low',    12, 'Brief unauthorised stop at NSIGT',           interval '20 minute')
) AS e(event_type, vehicle_id, facility_id, severity, duration_min, note, ago)
WHERE NOT EXISTS (
  SELECT 1 FROM jnpa.parking_events pe
  WHERE pe.vehicle_id = e.vehicle_id
    AND pe.detail->>'source' = 'DEMO_RICH'
);

COMMIT;

-- Summary
SELECT 'occupied_slots' AS metric, count(*)::text AS value FROM jnpa.parking_slots WHERE availability_status = 'OCCUPIED'
UNION ALL SELECT 'active_txns', count(*)::text FROM jnpa.parking_transactions WHERE status = 'ACTIVE'
UNION ALL SELECT 'completed_txns', count(*)::text FROM jnpa.parking_transactions WHERE status = 'COMPLETED'
UNION ALL SELECT 'violations', count(*)::text FROM jnpa.parking_events
  WHERE event_type IN ('ILLEGAL_PARKING','NO_PARKING_VIOLATION','OVERFLOW');
