-- ===========================================================================
-- P0 demo seed — NO_PARKING_VIOLATION events across the full write path.
-- FOR DEMONSTRATION ONLY. Every row is marked source=DEMO / sim=true.
--
-- Writes the same 4 stores a real parking violation would touch:
--   core.parking_event        (enforcement event log — Parking > Violations tab)
--   core.digital_twin_event   (unified event timeline / AI feed)
--   core.alert                (operator alert stream / notification bell)
--   core.notification         (driver notification trail)
--
-- Idempotent: guarded so re-running never duplicates.
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f scripts/seed_demo_parking_violations.sql
-- ROLLBACK: see the DELETE block at the bottom.
-- ===========================================================================
SET search_path TO core, public;

-- 1. parking_event — the record the Parking > Violations tab reads -----------
INSERT INTO core.parking_event
    (event_type, vehicle_id, driver_id, facility_id, slot_id, detail, created_at)
SELECT 'NO_PARKING_VIOLATION',
       'MH04PV' || lpad(g::text, 4, '0'),
       'DRV-DEMO-' || ((g % 5) + 1),
       (SELECT id FROM core.parking_facility ORDER BY id LIMIT 1),
       NULL,
       jsonb_build_object('source','DEMO','sim',true,
                          'reason','Parked in no-parking zone (DEMO)',
                          'zone','NPZ-GATE-NSICT'),
       now() - ((g * 7) || ' minutes')::interval
FROM generate_series(1, 5) AS g
WHERE NOT EXISTS (
    SELECT 1 FROM core.parking_event
    WHERE event_type = 'NO_PARKING_VIOLATION' AND detail->>'source' = 'DEMO');

-- 2. digital_twin_event — unified timeline / AI feed ------------------------
INSERT INTO core.digital_twin_event (event_type, vehicle_id, driver_id, location, payload, created_at)
SELECT 'PARKING_VIOLATION',
       'MH04PV' || lpad(g::text, 4, '0'),
       'DRV-DEMO-' || ((g % 5) + 1),
       jsonb_build_object('facility_id',(SELECT id FROM core.parking_facility ORDER BY id LIMIT 1)),
       jsonb_build_object('source','DEMO','sim',true,'violation','NO_PARKING_VIOLATION'),
       now() - ((g * 7) || ' minutes')::interval
FROM generate_series(1, 5) AS g
WHERE NOT EXISTS (
    SELECT 1 FROM core.digital_twin_event
    WHERE event_type = 'PARKING_VIOLATION' AND payload->>'source' = 'DEMO');

-- 3. alert — operator alert stream / notification bell ----------------------
INSERT INTO core.alert (kind, severity, plate, payload)
SELECT 'NO_PARKING_VIOLATION', 'warning', 'MH04PV' || lpad(g::text, 4, '0'),
       jsonb_build_object('source','DEMO','sim',true,
                          'zone_id','NPZ-GATE-NSICT','zone_kind','no_parking',
                          'vehicle_id','MH04PV' || lpad(g::text, 4, '0'),
                          'message','No-parking violation (DEMO)')
FROM generate_series(1, 5) AS g
WHERE NOT EXISTS (
    SELECT 1 FROM core.alert
    WHERE kind = 'NO_PARKING_VIOLATION' AND payload->>'source' = 'DEMO');

-- 4. notification — driver notification trail -------------------------------
INSERT INTO core.notification (event_id, channel, receiver, message, delivery_status, provider_response)
SELECT NULL, 'push', 'MH04PV' || lpad(g::text, 4, '0'),
       'No-parking violation recorded for MH04PV' || lpad(g::text, 4, '0') || ' (DEMO)',
       'SENT',
       jsonb_build_object('source','DEMO','sim',true,'kind','no_parking_violation')
FROM generate_series(1, 5) AS g
WHERE NOT EXISTS (
    SELECT 1 FROM core.notification
    WHERE provider_response->>'source' = 'DEMO' AND message LIKE 'No-parking violation%');

-- Summary -------------------------------------------------------------------
SELECT 'parking_event(NPV,DEMO)' AS store, count(*) FROM core.parking_event
    WHERE event_type='NO_PARKING_VIOLATION' AND detail->>'source'='DEMO'
UNION ALL SELECT 'digital_twin_event(DEMO)', count(*) FROM core.digital_twin_event
    WHERE event_type='PARKING_VIOLATION' AND payload->>'source'='DEMO'
UNION ALL SELECT 'alert(NPV,DEMO)', count(*) FROM core.alert
    WHERE kind='NO_PARKING_VIOLATION' AND payload->>'source'='DEMO'
UNION ALL SELECT 'notification(DEMO)', count(*) FROM core.notification
    WHERE provider_response->>'source'='DEMO' AND message LIKE 'No-parking violation%';

-- ---------------------------------------------------------------------------
-- ROLLBACK:
--   DELETE FROM core.notification       WHERE provider_response->>'source'='DEMO' AND message LIKE 'No-parking violation%';
--   DELETE FROM core.alert              WHERE kind='NO_PARKING_VIOLATION' AND payload->>'source'='DEMO';
--   DELETE FROM core.digital_twin_event WHERE event_type='PARKING_VIOLATION' AND payload->>'source'='DEMO';
--   DELETE FROM core.parking_event      WHERE event_type='NO_PARKING_VIOLATION' AND detail->>'source'='DEMO';
-- ---------------------------------------------------------------------------
