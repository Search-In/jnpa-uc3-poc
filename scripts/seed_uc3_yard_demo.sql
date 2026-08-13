-- seed_uc3_yard_demo.sql — demo data for UC-3 peak yard / truck-arrival management.
--
-- Idempotent (every statement is guarded), safe to re-run, and additive: it
-- creates nothing outside the rows it tags and touches no UC-1/UC-2 data.
--
-- What it guarantees, matching the UC-3 acceptance criteria:
--   1. FIVE ACTIVE enrolled driver vehicles that the Congestion Rerouting
--      console lists under "registered driver devices" and that arrival
--      management can hold and notify:
--        core.vehicle          (ACTIVE, the Vehicle-ID login gate)
--        core.driver_identity  (ACTIVE driver bound to the vehicle)
--        core.push_subscription(a push registration inside the 12 h active window)
--   2. The four terminal yards seeded by migration 0144 reset to their normal
--      opening utilisation, so the demo always starts from NORMAL.
--   3. Recent telemetry for each enrolled vehicle so the console shows a real
--      position instead of a null one.
--
-- Run:  psql "$DSN" -f scripts/seed_uc3_yard_demo.sql
-- Undo: see the DELETEs at the bottom of this file (commented).

BEGIN;

-- ---------------------------------------------------------------- 1) vehicles
INSERT INTO core.vehicle (vehicle_id, vehicle_no, vehicle_type, status, created_by)
SELECT v.vehicle_id, v.vehicle_no, 'TRAILER', 'ACTIVE', 'seed:uc3-yard-demo'
FROM (VALUES
        ('TRK-900001', 'MH46AB1001'),
        ('TRK-900002', 'MH46AB1002'),
        ('TRK-900003', 'MH46AB1003'),
        ('TRK-900004', 'MH46AB1004'),
        ('TRK-900005', 'MH46AB1005')
     ) AS v(vehicle_id, vehicle_no)
ON CONFLICT (vehicle_id) DO UPDATE
    SET status = 'ACTIVE', updated_at = now();

-- ----------------------------------------------------------------- 2) drivers
-- vehicle_no_norm is what core.driver_identity is joined on by the fleet list
-- (gateway/routers/trucks.py::_list_registered_pwa_devices) — it holds the
-- DEVICE id there, which for an enrolled PWA vehicle is the vehicle_id.
INSERT INTO core.driver_identity
    (driver_id, name, license_no, mobile, vehicle_no, vehicle_no_norm, status, created_by)
SELECT d.driver_id, d.name, d.license_no, d.mobile, d.vehicle_no, d.vehicle_no_norm,
       'ACTIVE', 'seed:uc3-yard-demo'
FROM (VALUES
        ('DRV-UC3-001', 'Ramesh Patil',   'MH0120110001234', '+919900000001', 'MH46AB1001', 'TRK-900001'),
        ('DRV-UC3-002', 'Sunil Kamble',   'MH0120110001235', '+919900000002', 'MH46AB1002', 'TRK-900002'),
        ('DRV-UC3-003', 'Imran Shaikh',   'MH0120110001236', '+919900000003', 'MH46AB1003', 'TRK-900003'),
        ('DRV-UC3-004', 'Ganesh Jadhav',  'MH0120110001237', '+919900000004', 'MH46AB1004', 'TRK-900004'),
        ('DRV-UC3-005', 'Vikas More',     'MH0120110001238', '+919900000005', 'MH46AB1005', 'TRK-900005')
     ) AS d(driver_id, name, license_no, mobile, vehicle_no, vehicle_no_norm)
-- uq_driver_identity_vehicle_active forbids two ACTIVE drivers on one vehicle;
-- skip a vehicle that already has one rather than fighting the constraint.
WHERE NOT EXISTS (
    SELECT 1 FROM core.driver_identity x
    WHERE x.vehicle_no_norm = d.vehicle_no_norm AND x.status = 'ACTIVE'
      AND x.driver_id <> d.driver_id)
ON CONFLICT (driver_id) DO UPDATE
    SET status = 'ACTIVE', updated_at = now();

-- ------------------------------------------------------- 3) push registration
-- A registration is what makes a device "an enrolled driver device" to the
-- console. The fcm_token here is a DEMO placeholder: with Firebase unconfigured
-- the FCM leg no-ops and delivery falls back to WebSocket + WebPush exactly as
-- it does for a real driver who has not granted push permission.
INSERT INTO core.push_subscription (device_id, driver_id, vehicle_id, fcm_token, platform, updated_at)
SELECT p.device_id, p.driver_id, p.device_id, p.token, 'web', now()
FROM (VALUES
        ('TRK-900001', 'DRV-UC3-001', 'demo-fcm-token-uc3-001'),
        ('TRK-900002', 'DRV-UC3-002', 'demo-fcm-token-uc3-002'),
        ('TRK-900003', 'DRV-UC3-003', 'demo-fcm-token-uc3-003'),
        ('TRK-900004', 'DRV-UC3-004', 'demo-fcm-token-uc3-004'),
        ('TRK-900005', 'DRV-UC3-005', 'demo-fcm-token-uc3-005')
     ) AS p(device_id, driver_id, token)
ON CONFLICT (device_id) DO UPDATE
    -- Refresh updated_at so the device falls inside REGISTERED_ACTIVE_WINDOW
    -- (12 h) and the console lists it. A real signed-in driver overwrites this.
    SET updated_at = now(), driver_id = EXCLUDED.driver_id;

-- ------------------------------------------------------------- 4) telemetry
-- One recent position each, on the NH-348 approach to the NSICT gate, so the
-- console shows a measured location rather than a null one.
INSERT INTO core.truck_telemetry (ts, device_id, plate, lat, lon, speed_kmh, heading, battery, accuracy_m)
SELECT now() - make_interval(secs => 30 * v.n),
       v.device_id, v.plate,
       18.9000 + 0.004 * v.n, 72.9800 - 0.004 * v.n,
       38.0, 310.0, 88.0, 6.0
FROM (VALUES
        ('TRK-900001', 'MH46AB1001', 1),
        ('TRK-900002', 'MH46AB1002', 2),
        ('TRK-900003', 'MH46AB1003', 3),
        ('TRK-900004', 'MH46AB1004', 4),
        ('TRK-900005', 'MH46AB1005', 5)
     ) AS v(device_id, plate, n)
WHERE NOT EXISTS (
    SELECT 1 FROM core.truck_telemetry t
    WHERE t.device_id = v.device_id AND t.ts > now() - interval '10 minutes');

-- ----------------------------------------------------- 5) reset yards to normal
-- Put every yard back at its declared opening utilisation and audit the reset,
-- so a re-run of the demo always starts from NORMAL.
WITH target AS (
    SELECT yard_id, capacity_slots,
           CASE yard_id
               WHEN 'JNPA-NSICT-YARD' THEN 3360
               WHEN 'JNPA-JNPCT-YARD' THEN 4680
               WHEN 'JNPA-NSIGT-YARD' THEN 3900
               WHEN 'JNPA-BMCT-YARD'  THEN 6480
           END AS want,
           occupied_slots AS before
    FROM core.yard_capacity_state
), changed AS (
    UPDATE core.yard_capacity_state s
    SET occupied_slots = t.want, source = 'DECLARED_SEED'
    FROM target t
    WHERE s.yard_id = t.yard_id AND t.want IS NOT NULL AND s.occupied_slots <> t.want
    RETURNING s.yard_id, s.capacity_slots, s.occupied_slots, t.before
)
INSERT INTO core.yard_capacity_event
    (yard_id, event_type, delta_slots, occupied_before, occupied_after,
     capacity_slots, utilization_pct, status, reason, actor)
SELECT c.yard_id, 'SET', c.occupied_slots - c.before, c.before, c.occupied_slots,
       c.capacity_slots, round(100.0 * c.occupied_slots / c.capacity_slots, 2),
       'NORMAL', 'seed_uc3_yard_demo.sql reset to opening utilisation',
       'seed:uc3-yard-demo'
FROM changed c;

-- --------------------------------------------------- 6) clear stale demo holds
UPDATE core.truck_arrival_hold
SET status = 'CANCELLED', released_at = now()
WHERE status = 'HOLD_AT_PARKING';

INSERT INTO core.truck_arrival_hold_event (hold_id, device_id, action, actor, detail)
SELECT h.id, h.device_id, 'CANCELLED', 'seed:uc3-yard-demo',
       jsonb_build_object('reason', 'demo reset')
FROM core.truck_arrival_hold h
WHERE h.status = 'CANCELLED' AND h.released_at > now() - interval '1 minute'
  AND NOT EXISTS (SELECT 1 FROM core.truck_arrival_hold_event e
                  WHERE e.hold_id = h.id AND e.action = 'CANCELLED');

COMMIT;

-- Verify:
--   SELECT yard_id, capacity_slots, occupied_slots,
--          round(100.0*occupied_slots/capacity_slots,1) AS pct
--   FROM core.yard_capacity_state ORDER BY yard_id;
--   SELECT p.device_id, d.name, v.status
--   FROM core.push_subscription p
--   JOIN core.vehicle v ON v.vehicle_id = p.device_id
--   LEFT JOIN core.driver_identity d ON d.vehicle_no_norm = p.device_id AND d.status='ACTIVE'
--   WHERE p.device_id LIKE 'TRK-9000%';
--
-- Undo (removes ONLY what this seed created):
--   DELETE FROM core.push_subscription WHERE device_id LIKE 'TRK-9000%';
--   DELETE FROM core.driver_identity   WHERE driver_id LIKE 'DRV-UC3-%';
--   DELETE FROM core.vehicle           WHERE vehicle_id LIKE 'TRK-9000%';
--   DELETE FROM core.truck_telemetry   WHERE device_id LIKE 'TRK-9000%';
