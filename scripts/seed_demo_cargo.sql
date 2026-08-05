-- ===========================================================================
-- Demo seed — core.cargo (Cargo Twin ⇄ Traffic Twin shared record).
-- FOR DEMONSTRATION ONLY. ~15 realistic cargo records so /api/cargo (consumed by
-- both the POC-3 dashboard and the POC-2 Cargo-Twin frontend) has non-empty data
-- on a fresh boot.
--
-- Every container_number is a check-digit-valid ISO-6346 number (verified with
-- jnpa_shared.iso6346.is_valid_container_no). ETAs are relative to now() so the
-- arrival board stays plausible whenever the seed is applied.
--
-- Idempotent: INSERT ... ON CONFLICT (container_number) DO NOTHING — re-running
-- never duplicates and never overwrites edits made through the API.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f scripts/seed_demo_cargo.sql
-- REMOVE: DELETE FROM core.cargo WHERE container_number IN (see list below);
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS core;
SET search_path TO core, public;

INSERT INTO core.cargo
    (container_number, vessel_name, customs_status, yard_block, is_released,
     vehicle_number, gate, camera_id, eta)
VALUES
    ('MAEU6123458', 'MAERSK SEMBAWANG',   'CLEARED',          'A-01', true,  'MH04AB1234', 'GATE-1', 'CAM-ANPR-01', now() - interval '2 hours'),
    ('MSCU7789010', 'MSC ANNA',           'PENDING',          'A-04', false, 'MH05CD4567', 'GATE-1', 'CAM-ANPR-01', now() + interval '3 hours'),
    ('CMAU4455661', 'CMA CGM MARCO POLO', 'UNDER_INSPECTION', 'B-02', false, 'MH06EF8901', 'GATE-2', 'CAM-ANPR-02', now() + interval '6 hours'),
    ('HLCU2030403', 'HAPAG BREMEN',       'CLEARED',          'B-07', true,  'MH12GH2345', 'GATE-2', 'CAM-ANPR-02', now() - interval '30 minutes'),
    ('OOLU9050118', 'OOCL SHENZHEN',      'HELD',             'C-03', false, 'MH14JK6789', 'GATE-3', 'CAM-ANPR-03', now() + interval '10 hours'),
    ('APLU1188221', 'APL SINGAPORE',      'PENDING',          'C-05', false, 'MH01LM1122', 'GATE-3', 'CAM-ANPR-03', now() + interval '1 hour'),
    ('TGHU6677001', 'EVER GIVEN',         'CLEARED',          'A-09', true,  'MH02NP3344', 'GATE-1', 'CAM-ANPR-01', now() - interval '4 hours'),
    ('TCLU3344559', 'ONE APUS',           'PENDING',          'D-01', false, 'MH03QR5566', 'GATE-4', 'CAM-ANPR-04', now() + interval '8 hours'),
    ('GESU5123996', 'COSCO SHIPPING ARIES','UNDER_INSPECTION','D-04', false, 'MH43ST7788', 'GATE-4', 'CAM-ANPR-04', now() + interval '12 hours'),
    ('TEMU7001236', 'MAERSK HONAM',       'CLEARED',          'B-11', true,  'MH46UV9900', 'GATE-2', 'CAM-ANPR-02', now() - interval '1 hour'),
    ('BMOU8102340', 'MSC GULSUN',         'HELD',             'C-08', false, 'MH04WX1235', 'GATE-3', 'CAM-ANPR-03', now() + interval '5 hours'),
    ('FCIU9203457', 'CMA CGM JACQUES',    'PENDING',          'A-15', false, 'MH05YZ4568', 'GATE-1', 'CAM-ANPR-01', now() + interval '2 hours'),
    ('CAIU1304566', 'HAPAG ANTWERP',      'CLEARED',          'D-06', true,  'MH12AB7890', 'GATE-4', 'CAM-ANPR-04', now() - interval '3 hours'),
    ('DFSU2405676', 'OOCL GERMANY',       'UNDER_INSPECTION', 'B-03', false, 'MH14CD2346', 'GATE-2', 'CAM-ANPR-02', now() + interval '9 hours'),
    ('NYKU3506780', 'NYK VESTA',          'PENDING',          'C-12', false, 'MH01EF6780', 'GATE-3', 'CAM-ANPR-03', now() + interval '7 hours')
ON CONFLICT (container_number) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Doc-pack Auto-LEO demo containers — these four container numbers appear in
-- the demo documentation pack and MUST resolve in cargo lookups (Auto-LEO
-- reconciliation demo). Values follow the same shape as the block above.
-- Idempotent: ON CONFLICT (container_number) DO NOTHING (core.cargo PK).
-- ---------------------------------------------------------------------------
INSERT INTO core.cargo
    (container_number, vessel_name, customs_status, yard_block, is_released,
     vehicle_number, gate, camera_id, eta)
VALUES
    ('APLU0896946', 'APL SINGAPORE',      'CLEARED', 'A-11', false, 'MH04KN3106', 'GATE-1', 'CAM-ANPR-01', now() - interval '90 minutes'),
    ('CMAU3549370', 'CMA CGM MARCO POLO', 'CLEARED', 'B-05', false, 'MH43SV7025', 'GATE-2', 'CAM-ANPR-02', now() - interval '45 minutes'),
    ('MSCU1234566', 'MSC ANNA',           'PENDING', 'C-09', false, 'MH05CD4567', 'GATE-3', 'CAM-ANPR-03', now() + interval '4 hours'),
    ('MAEU7654320', 'MAERSK SEMBAWANG',   'CLEARED', 'D-02', true,  'MH04AB1234', 'GATE-4', 'CAM-ANPR-04', now() - interval '15 minutes')
ON CONFLICT (container_number) DO NOTHING;
