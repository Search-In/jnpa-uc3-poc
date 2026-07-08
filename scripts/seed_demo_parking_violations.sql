-- ===========================================================================
-- P0 demo seed — NO_PARKING_VIOLATION events across the full write path.
-- FOR DEMONSTRATION ONLY. Every row is marked source=DEMO / sim=true.
--
-- Writes the same 4 stores a real parking violation would touch:
--   jnpa.parking_events        (enforcement event log — Parking > Violations tab)
--   jnpa.digital_twin_events   (unified event timeline / AI feed)
--   jnpa.alerts                (operator alert stream / notification bell)
--   jnpa.notifications         (driver notification trail)
--
-- Idempotent: guarded so re-running never duplicates.
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f scripts/seed_demo_parking_violations.sql
-- ROLLBACK: see the DELETE block at the bottom.
-- ===========================================================================
SET search_path TO jnpa, public;

-- 1. parking_events — the record the Parking > Violations tab reads -----------
INSERT INTO jnpa.parking_events
    (event_type, vehicle_id, driver_id, facility_id, slot_id, detail, created_at)
SELECT 'NO_PARKING_VIOLATION',
       'MH04PV' || lpad(g::text, 4, '0'),
       'DRV-DEMO-' || ((g % 5) + 1),
       (SELECT id FROM jnpa.parking_facilities ORDER BY id LIMIT 1),
       NULL,
       jsonb_build_object('source','DEMO','sim',true,
                          'reason','Parked in no-parking zone (DEMO)',
                          'zone','NPZ-GATE-NSICT'),
       now() - ((g * 7) || ' minutes')::interval
FROM generate_series(1, 5) AS g
WHERE NOT EXISTS (
    SELECT 1 FROM jnpa.parking_events
    WHERE event_type = 'NO_PARKING_VIOLATION' AND detail->>'source' = 'DEMO');

-- 2. digital_twin_events — unified timeline / AI feed ------------------------
INSERT INTO jnpa.digital_twin_events (event_type, vehicle_id, driver_id, location, payload, created_at)
SELECT 'PARKING_VIOLATION',
       'MH04PV' || lpad(g::text, 4, '0'),
       'DRV-DEMO-' || ((g % 5) + 1),
       jsonb_build_object('facility_id',(SELECT id FROM jnpa.parking_facilities ORDER BY id LIMIT 1)),
       jsonb_build_object('source','DEMO','sim',true,'violation','NO_PARKING_VIOLATION'),
       now() - ((g * 7) || ' minutes')::interval
FROM generate_series(1, 5) AS g
WHERE NOT EXISTS (
    SELECT 1 FROM jnpa.digital_twin_events
    WHERE event_type = 'PARKING_VIOLATION' AND payload->>'source' = 'DEMO');

-- 3. alerts — operator alert stream / notification bell ----------------------
INSERT INTO jnpa.alerts (kind, severity, plate, payload)
SELECT 'NO_PARKING_VIOLATION', 'warning', 'MH04PV' || lpad(g::text, 4, '0'),
       jsonb_build_object('source','DEMO','sim',true,
                          'zone_id','NPZ-GATE-NSICT','zone_kind','no_parking',
                          'vehicle_id','MH04PV' || lpad(g::text, 4, '0'),
                          'message','No-parking violation (DEMO)')
FROM generate_series(1, 5) AS g
WHERE NOT EXISTS (
    SELECT 1 FROM jnpa.alerts
    WHERE kind = 'NO_PARKING_VIOLATION' AND payload->>'source' = 'DEMO');

-- 4. notifications — driver notification trail -------------------------------
INSERT INTO jnpa.notifications (event_id, channel, receiver, message, delivery_status, provider_response)
SELECT NULL, 'push', 'MH04PV' || lpad(g::text, 4, '0'),
       'No-parking violation recorded for MH04PV' || lpad(g::text, 4, '0') || ' (DEMO)',
       'SENT',
       jsonb_build_object('source','DEMO','sim',true,'kind','no_parking_violation')
FROM generate_series(1, 5) AS g
WHERE NOT EXISTS (
    SELECT 1 FROM jnpa.notifications
    WHERE provider_response->>'source' = 'DEMO' AND message LIKE 'No-parking violation%');

-- Summary -------------------------------------------------------------------
SELECT 'parking_events(NPV,DEMO)' AS store, count(*) FROM jnpa.parking_events
    WHERE event_type='NO_PARKING_VIOLATION' AND detail->>'source'='DEMO'
UNION ALL SELECT 'digital_twin_events(DEMO)', count(*) FROM jnpa.digital_twin_events
    WHERE event_type='PARKING_VIOLATION' AND payload->>'source'='DEMO'
UNION ALL SELECT 'alerts(NPV,DEMO)', count(*) FROM jnpa.alerts
    WHERE kind='NO_PARKING_VIOLATION' AND payload->>'source'='DEMO'
UNION ALL SELECT 'notifications(DEMO)', count(*) FROM jnpa.notifications
    WHERE provider_response->>'source'='DEMO' AND message LIKE 'No-parking violation%';

-- ---------------------------------------------------------------------------
-- ROLLBACK:
--   DELETE FROM jnpa.notifications      WHERE provider_response->>'source'='DEMO' AND message LIKE 'No-parking violation%';
--   DELETE FROM jnpa.alerts             WHERE kind='NO_PARKING_VIOLATION' AND payload->>'source'='DEMO';
--   DELETE FROM jnpa.digital_twin_events WHERE event_type='PARKING_VIOLATION' AND payload->>'source'='DEMO';
--   DELETE FROM jnpa.parking_events     WHERE event_type='NO_PARKING_VIOLATION' AND detail->>'source'='DEMO';
-- ---------------------------------------------------------------------------
