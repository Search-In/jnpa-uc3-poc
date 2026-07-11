-- ===========================================================================
-- Production driver master-data seed — Driver -> Vehicle -> Device mapping.
--
-- Establishes the master-data relationship between a real driver, the vehicle
-- (plate) they operate, and the in-cab device (device_id) they sign in with.
-- This is DATA ONLY: no table is created, no schema is altered, and NO
-- authentication / JWT / WebSocket / push CODE is touched. It only populates the
-- existing master + bridge tables the running services already read/write.
--
-- Tables populated (all pre-existing — see infra/postgres/init.sql):
--   * jnpa.vehicle_master     — vehicle RC master (PK plate)
--   * jnpa.drivers            — driver master      (PK driver_id)
--   * jnpa.device_bindings    — device<->driver<->mobile bridge (PK device_id)
--   * jnpa.push_subscriptions — device push mapping driver_id/vehicle_id (PK device_id)
--
-- NOT touched:
--   * jnpa.otp_requests       — transient one-time codes; never master data.
--
-- Device -> plate is the REAL binding the telemetry source resolves:
--   GET /api/trucks/TRK-000001 -> record.plate = 'MH04KN3106'
--   (ingest/trucking_app .../plates.py::plate_for_index(0), deterministic).
-- Seeding that same plate keeps the master data consistent with what the login
-- flow loads for the driver's vehicle card.
--
-- IDEMPOTENT: every statement is INSERT ... ON CONFLICT DO UPDATE keyed on the
-- table's primary key, so running this file twice updates in place and never
-- duplicates a row. push_subscriptions intentionally does NOT overwrite a live
-- fcm_token / webpush registration — it only (re)asserts the driver/vehicle map.
--
-- APPLY:  psql "$DSN" -v ON_ERROR_STOP=1 -f scripts/seed_production_drivers.sql
--         (DSN e.g. postgresql://jnpa:jnpa@localhost:5433/jnpa)
--
-- EXTEND: add one row per real driver to the drivers_seed CTE at the top of the
--         DO block. Names/mobiles must be REAL — do not fabricate. The rest of
--         the file derives every mapping from that single source list.
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

BEGIN;

-- --------------------------------------------------------------------------
-- Single source of truth for this seed: (driver_id, name, mobile, plate,
-- device_id). Every downstream upsert reads from this list, so adding a real
-- driver here fans out to all four tables consistently. Add rows as needed.
-- --------------------------------------------------------------------------
WITH roster (driver_id, name, mobile, plate, device_id, state, rto_code) AS (
    VALUES
        ('DV101', 'Jayesh More', '7507188300', 'MH04KN3106', 'TRK-000001', 'Maharashtra', 'MH04')
        -- , ('DV102', '<real name>', '<real mobile>', 'MH43SV7025', 'TRK-000002', 'Maharashtra', 'MH43')
),

-- 1) VEHICLE MASTER (PK plate) — assert the plate exists as an RC master row.
--    Only stable, plate-derivable fields are set here (state, rto_code); the
--    Vahan service fills owner/insurance/fitness on the first /vahan/rc lookup,
--    so ON CONFLICT deliberately leaves those columns untouched.
up_vehicle AS (
    INSERT INTO jnpa.vehicle_master AS vm (plate, state, rto_code, blacklist_status, provisional, updated_at)
    SELECT plate, state, rto_code, 'CLEAR', false, now() FROM roster
    ON CONFLICT (plate) DO UPDATE
        SET state      = COALESCE(vm.state, EXCLUDED.state),
            rto_code   = COALESCE(vm.rto_code, EXCLUDED.rto_code),
            updated_at = now()
    RETURNING plate
),

-- 2) DRIVER MASTER (PK driver_id) — the real driver record.
up_driver AS (
    INSERT INTO jnpa.drivers AS d (driver_id, name, mobile, vehicle_no, status, provider, updated_at)
    SELECT driver_id, name, mobile, plate, 'ACTIVE', 'master', now() FROM roster
    ON CONFLICT (driver_id) DO UPDATE
        SET name       = EXCLUDED.name,
            mobile     = EXCLUDED.mobile,
            vehicle_no = EXCLUDED.vehicle_no,
            status     = 'ACTIVE',
            provider   = 'master',
            updated_at = now()
    RETURNING driver_id
),

-- 3) DEVICE BINDING (PK device_id) — the device<->driver<->mobile bridge that
--    the session model (jnpa.device_bindings) is keyed on. active=true so the
--    binding is treated as a live session by the existing refresh/session logic.
up_binding AS (
    INSERT INTO jnpa.device_bindings AS b (device_id, mobile, driver_id, bound_at, last_seen, active)
    SELECT device_id, mobile, driver_id, now(), now(), true FROM roster
    ON CONFLICT (device_id) DO UPDATE
        SET mobile    = EXCLUDED.mobile,
            driver_id = EXCLUDED.driver_id,
            last_seen = now(),
            active    = true
    RETURNING device_id
)

-- 4) PUSH MAPPING (PK device_id) — pre-populate the driver/vehicle identity on
--    the push row so FCM/WebPush deliveries resolve to the right driver even
--    before the first register-device call. fcm_token / webpush are left as-is
--    (NULL here; a live registration fills and MUST NOT be clobbered).
INSERT INTO jnpa.push_subscriptions (device_id, driver_id, vehicle_id, platform, created_at, updated_at)
SELECT device_id, driver_id, plate, 'web', now(), now() FROM roster
ON CONFLICT (device_id) DO UPDATE
    SET driver_id  = EXCLUDED.driver_id,
        vehicle_id = EXCLUDED.vehicle_id,
        updated_at = now();

COMMIT;

-- ===========================================================================
-- VERIFICATION (read-only) — run after applying. Expect one fully-linked row.
-- ===========================================================================
-- 1) The full Driver -> Vehicle -> Device chain, joined across all four tables:
--
-- SELECT d.driver_id, d.name, d.mobile, d.vehicle_no,
--        b.device_id, b.active AS binding_active,
--        v.plate, v.state, v.rto_code,
--        p.driver_id AS push_driver, p.vehicle_id AS push_vehicle
--   FROM jnpa.drivers d
--   JOIN jnpa.device_bindings b     ON b.driver_id = d.driver_id
--   JOIN jnpa.vehicle_master  v     ON v.plate     = d.vehicle_no
--   LEFT JOIN jnpa.push_subscriptions p ON p.device_id = b.device_id
--  WHERE d.driver_id = 'DV101';
--
-- 2) Confirm no duplication after a second run (each must be exactly 1):
--
-- SELECT 'drivers'            AS t, count(*) FROM jnpa.drivers            WHERE driver_id = 'DV101'
-- UNION ALL SELECT 'vehicle_master',    count(*) FROM jnpa.vehicle_master    WHERE plate     = 'MH04KN3106'
-- UNION ALL SELECT 'device_bindings',   count(*) FROM jnpa.device_bindings   WHERE device_id = 'TRK-000001'
-- UNION ALL SELECT 'push_subscriptions',count(*) FROM jnpa.push_subscriptions WHERE device_id = 'TRK-000001';
--
-- 3) Confirm the device resolves to the same plate the telemetry source returns
--    (should equal MH04KN3106 from GET /api/trucks/TRK-000001):
--
-- SELECT b.device_id, d.vehicle_no AS master_plate
--   FROM jnpa.device_bindings b JOIN jnpa.drivers d ON d.driver_id = b.driver_id
--  WHERE b.device_id = 'TRK-000001';
-- ===========================================================================
