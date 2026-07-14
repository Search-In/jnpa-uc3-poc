-- ===========================================================================
-- P0 demo seed — populate the modules the audit found empty so the dashboard
-- demonstrates end-to-end flows. FOR DEMONSTRATION ONLY.
--
-- Every row is explicitly marked as demo/sim data:
--   * jsonb detail/payload columns carry {"source":"DEMO","sim":true}
--   * text columns are suffixed/prefixed with DEMO / SIM
-- so demo rows are trivially identifiable and removable (see the DELETE block at
-- the bottom, commented out). Seeds the DB layer directly; does NOT fabricate UI
-- data and does NOT touch the audit-framework tables' logic.
--
-- Idempotent: each block is guarded so re-running never duplicates.
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f scripts/seed_demo_p0.sql
-- Modules: FASTag txns · Driver enrollment · Parking history · Empty-container
--          allocation history · Scenario timeline.
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

-- 1. FASTag transactions ----------------------------------------------------
-- (Search-driven tab re-fetches from ULIP-SIM live; these rows give the table a
--  non-empty demo history and satisfy the /api/fastag/health table check.)
INSERT INTO jnpa.fastag_transactions
    (id, tag_id, rc_number, seq_no, transaction_date_time, lane_direction,
     toll_plaza_name, vehicle_type, bank_name, status, created_at)
SELECT gen_random_uuid(), 'TAG-DEMO-001', 'MH04DM0001', 'DEMO-' || g,
       now() - (g || ' hours')::interval,
       CASE WHEN g % 2 = 0 THEN 'N' ELSE 'S' END,
       'JNPA Toll Plaza (DEMO)', 'VC4', 'SIM DEMO BANK', 'SUCCESS',
       now() - (g || ' hours')::interval
FROM generate_series(1, 8) AS g
WHERE NOT EXISTS (SELECT 1 FROM jnpa.fastag_transactions WHERE rc_number = 'MH04DM0001');

-- 2. Driver enrollment (create table if the service hasn't yet) ---------------
CREATE TABLE IF NOT EXISTS jnpa.driver_enrollments (
    driver_id         text PRIMARY KEY,
    name              text NOT NULL,
    license_no        text,
    mobile            text,
    vehicle_no        text,
    aadhaar_masked    text,
    emergency_contact text,
    status            text NOT NULL DEFAULT 'PENDING'
                      CHECK (status IN ('PENDING', 'ACTIVE', 'REJECTED', 'REENROLL')),
    consent           boolean NOT NULL DEFAULT false,
    consent_at        timestamptz,
    face_images       jsonb NOT NULL DEFAULT '[]'::jsonb,
    reference_image   text,
    photo_url         text,
    documents         jsonb NOT NULL DEFAULT '[]'::jsonb,
    template_dim      int,
    provider          text,
    submitted_at      timestamptz NOT NULL DEFAULT now(),
    reviewed_at       timestamptz,
    reviewed_by       text,
    rejection_reason  text,
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_driver_enrol_status
    ON jnpa.driver_enrollments (status, submitted_at DESC);

INSERT INTO jnpa.driver_enrollments
    (driver_id, name, license_no, mobile, vehicle_no, aadhaar_masked,
     status, consent, consent_at, provider, submitted_at)
SELECT 'DRV-DEMO-' || g,
       'Demo Driver ' || g || ' (DEMO)',
       'MH04-2021-000' || g, '90000000' || (10 + g),
       'MH04DM' || lpad(g::text, 4, '0'),
       'XXXX-XXXX-' || (1000 + g), 'PENDING', true, now(), 'demo',
       now() - (g || ' hours')::interval
FROM generate_series(1, 5) AS g
WHERE NOT EXISTS (SELECT 1 FROM jnpa.driver_enrollments WHERE driver_id LIKE 'DRV-DEMO-%');

-- 3. Parking history (COMPLETED transactions + events) ----------------------
-- Uses the first real facility so FK/geo joins resolve; occupancy stays RDS-real
-- (these are COMPLETED, so they don't inflate the live occupied count).
INSERT INTO jnpa.parking_transactions
    (vehicle_id, driver_id, facility_id, slot_id, entry_time, exit_time,
     duration, status, created_at)
SELECT 'MH04DM' || lpad(g::text, 4, '0'), 'DRV-DEMO-' || ((g % 5) + 1),
       (SELECT id FROM jnpa.parking_facilities ORDER BY id LIMIT 1),
       NULL,
       now() - ((g * 3) || ' hours')::interval,
       now() - ((g * 3 - 2) || ' hours')::interval,
       (2 || ' hours')::interval, 'COMPLETED',
       now() - ((g * 3) || ' hours')::interval
FROM generate_series(1, 8) AS g
WHERE NOT EXISTS (SELECT 1 FROM jnpa.parking_transactions WHERE vehicle_id LIKE 'MH04DM%');

INSERT INTO jnpa.parking_events
    (event_type, vehicle_id, driver_id, facility_id, slot_id, detail, created_at)
SELECT (ARRAY['ALLOCATION','RELEASE'])[1 + (g % 2)],
       'MH04DM' || lpad(g::text, 4, '0'), 'DRV-DEMO-' || ((g % 5) + 1),
       (SELECT id FROM jnpa.parking_facilities ORDER BY id LIMIT 1),
       NULL, '{"source":"DEMO","sim":true}'::jsonb,
       now() - ((g * 3) || ' hours')::interval
FROM generate_series(1, 6) AS g
WHERE NOT EXISTS (
    SELECT 1 FROM jnpa.parking_events
    WHERE detail->>'source' = 'DEMO' AND vehicle_id LIKE 'MH04DM%');

-- 4. Empty-container allocation history --------------------------------------
INSERT INTO jnpa.empty_container_allocations
    (container_id, truck_id, trailer_id, driver_id, shipping_line, cfs, ecd,
     allocation_reason, allocated_at, status)
SELECT (SELECT container_id FROM jnpa.empty_container_inventory
        ORDER BY container_id LIMIT 1 OFFSET g),
       'TRK-DEMO-' || g, 'TRL-DEMO-' || g, 'DRV-DEMO-' || ((g % 5) + 1),
       'SIM DEMO LINE', 'CFS-DEMO', 'ECD-DEMO', 'DEMO seed allocation',
       now() - (g || ' hours')::interval, 'ALLOCATED'
FROM generate_series(1, 6) AS g
WHERE NOT EXISTS (
    SELECT 1 FROM jnpa.empty_container_allocations WHERE shipping_line = 'SIM DEMO LINE');

-- 5. Scenario timeline (handle + steps) --------------------------------------
INSERT INTO jnpa.scenario_handles (handle_id, name, status, params, trace_id, started_at, ended_at)
SELECT 'demo-tfc1-0001', 'tfc1', 'completed',
       '{"source":"DEMO","sim":true,"gate_id":"G-NSICT"}'::jsonb,
       'demo-trace-0001', now() - interval '30 min', now() - interval '25 min'
WHERE NOT EXISTS (SELECT 1 FROM jnpa.scenario_handles WHERE handle_id = 'demo-tfc1-0001');

INSERT INTO jnpa.scenario_steps (handle_id, step_no, ts, title, status, trigger, detail)
SELECT 'demo-tfc1-0001', s.step_no, now() - interval '30 min' + (s.step_no || ' min')::interval,
       s.title, s.status, s.trigger, '{"source":"DEMO","sim":true}'::jsonb
FROM (VALUES
    (1, 'Appointment booked (TFC-1)', 'ok',       'booking'),
    (2, 'Gate camera ANPR match',     'ok',       'anpr'),
    (3, 'FASTag debit confirmed',     'ok',       'fastag'),
    (4, 'Congestion re-route advised','info',     'reroute'),
    (5, 'Gate-in completed',          'ok',       'gate_in')
) AS s(step_no, title, status, trigger)
WHERE NOT EXISTS (SELECT 1 FROM jnpa.scenario_steps WHERE handle_id = 'demo-tfc1-0001');

-- Summary of what is now present (demo rows only) ---------------------------
SELECT 'fastag_transactions' AS table, count(*) FROM jnpa.fastag_transactions WHERE rc_number='MH04DM0001'
UNION ALL SELECT 'driver_enrollments', count(*) FROM jnpa.driver_enrollments WHERE driver_id LIKE 'DRV-DEMO-%'
UNION ALL SELECT 'parking_transactions', count(*) FROM jnpa.parking_transactions WHERE vehicle_id LIKE 'MH04DM%'
UNION ALL SELECT 'parking_events(demo)', count(*) FROM jnpa.parking_events WHERE detail->>'source'='DEMO'
UNION ALL SELECT 'empty_container_allocations', count(*) FROM jnpa.empty_container_allocations WHERE shipping_line='SIM DEMO LINE'
UNION ALL SELECT 'scenario_steps', count(*) FROM jnpa.scenario_steps WHERE handle_id='demo-tfc1-0001';

-- ---------------------------------------------------------------------------
-- To REMOVE all P0 demo seed data (rollback):
--   DELETE FROM jnpa.scenario_steps       WHERE handle_id='demo-tfc1-0001';
--   DELETE FROM jnpa.scenario_handles     WHERE handle_id='demo-tfc1-0001';
--   DELETE FROM jnpa.empty_container_allocations WHERE shipping_line='SIM DEMO LINE';
--   DELETE FROM jnpa.parking_events       WHERE detail->>'source'='DEMO';
--   DELETE FROM jnpa.parking_transactions WHERE vehicle_id LIKE 'MH04DM%';
--   DELETE FROM jnpa.driver_enrollments   WHERE driver_id LIKE 'DRV-DEMO-%';
--   DELETE FROM jnpa.fastag_transactions  WHERE rc_number='MH04DM0001';
-- ---------------------------------------------------------------------------
