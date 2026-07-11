-- ===========================================================================
-- Demo seed — jnpa.cargo (Cargo Twin ⇄ Traffic Twin shared record).
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
-- REMOVE: DELETE FROM jnpa.cargo WHERE container_number IN (see list below);
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

INSERT INTO jnpa.cargo
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
