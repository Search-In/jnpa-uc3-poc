-- ===========================================================================
-- Production driver master-data seed — Driver -> Vehicle -> Device mapping.
--
-- Establishes the master-data relationship between a real driver, the vehicle
-- (plate) they operate, and the in-cab device (device_id) they sign in with.
-- This is DATA ONLY: no table is created, no schema is altered, and NO
-- authentication / JWT / WebSocket / push CODE is touched. It only populates the
-- existing master + bridge tables the running services already read/write.
--
-- Tables populated (all pre-existing — see infra/postgres/v3/0101_core_operational_ext.sql):
--   * core.vehicle_rc        — vehicle RC master (PK plate; was jnpa.vehicle_master)
--   * core.driver_identity   — driver master      (PK driver_id; was jnpa.drivers)
--   * core.device_binding    — device<->driver<->mobile bridge (PK device_id)
--   * core.push_subscription — device push mapping driver_id/vehicle_id (PK device_id)
--
-- NOT touched:
--   * core.otp_request       — transient one-time codes; never master data.
--
-- Device -> plate is the REAL binding the telemetry source resolves:
--   GET /api/trucks/TRK-000001 -> record.plate = 'MH04KN3106'
--   (ingest/trucking_app .../plates.py::plate_for_index(0), deterministic).
-- Seeding that same plate keeps the master data consistent with what the login
-- flow loads for the driver's vehicle card.
--
-- IDEMPOTENT: every statement is INSERT ... ON CONFLICT DO UPDATE keyed on the
-- table's primary key, so running this file twice updates in place and never
-- duplicates a row. push_subscription intentionally does NOT overwrite a live
-- fcm_token / webpush registration — it only (re)asserts the driver/vehicle map.
--
-- APPLY:  psql "$DSN" -v ON_ERROR_STOP=1 -f scripts/seed_production_drivers.sql
--         (DSN e.g. postgresql://postgres:...@localhost:5434/postgres)
--
-- EXTEND: add one row per real driver to the drivers_seed CTE at the top of the
--         DO block. Names/mobiles must be REAL — do not fabricate. The rest of
--         the file derives every mapping from that single source list.
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS core;
SET search_path TO core, public;

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
    INSERT INTO core.vehicle_rc AS vm (plate, state, rto_code, blacklist_status, provisional, updated_at)
    SELECT plate, state, rto_code, 'CLEAR', false, now() FROM roster
    ON CONFLICT (plate) DO UPDATE
        SET state      = COALESCE(vm.state, EXCLUDED.state),
            rto_code   = COALESCE(vm.rto_code, EXCLUDED.rto_code),
            updated_at = now()
    RETURNING plate
),

-- 2) DRIVER MASTER (PK driver_id) — the real driver record.
up_driver AS (
    INSERT INTO core.driver_identity AS d (driver_id, name, mobile, vehicle_no, status, provider, updated_at)
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
--    the session model (core.device_binding) is keyed on. active=true so the
--    binding is treated as a live session by the existing refresh/session logic.
up_binding AS (
    INSERT INTO core.device_binding AS b (device_id, mobile, driver_id, bound_at, last_seen, active)
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
INSERT INTO core.push_subscription (device_id, driver_id, vehicle_id, platform, created_at, updated_at)
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
--   FROM core.driver_identity d
--   JOIN core.device_binding b      ON b.driver_id = d.driver_id
--   JOIN core.vehicle_rc     v      ON v.plate     = d.vehicle_no
--   LEFT JOIN core.push_subscription p ON p.device_id = b.device_id
--  WHERE d.driver_id = 'DV101';
--
-- 2) Confirm no duplication after a second run (each must be exactly 1):
--
-- SELECT 'driver_identity'          AS t, count(*) FROM core.driver_identity   WHERE driver_id = 'DV101'
-- UNION ALL SELECT 'vehicle_rc',        count(*) FROM core.vehicle_rc        WHERE plate     = 'MH04KN3106'
-- UNION ALL SELECT 'device_binding',    count(*) FROM core.device_binding    WHERE device_id = 'TRK-000001'
-- UNION ALL SELECT 'push_subscription', count(*) FROM core.push_subscription WHERE device_id = 'TRK-000001';
--
-- 3) Confirm the device resolves to the same plate the telemetry source returns
--    (should equal MH04KN3106 from GET /api/trucks/TRK-000001):
--
-- SELECT b.device_id, d.vehicle_no AS master_plate
--   FROM core.device_binding b JOIN core.driver_identity d ON d.driver_id = b.driver_id
--  WHERE b.device_id = 'TRK-000001';
-- ===========================================================================
