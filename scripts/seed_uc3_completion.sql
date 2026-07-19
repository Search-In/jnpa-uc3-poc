-- ===========================================================================
-- Demo seed for the UC-III Final-Completion features (migration 0024).
-- Idempotent: every INSERT is guarded (ON CONFLICT / NOT EXISTS) so re-running
-- never duplicates. Reefer slots and RMS-TAS appointments have dedicated seed
-- endpoints (POST /api/reefer/seed, POST /api/rms-tas/seed) and are not seeded here.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f scripts/seed_uc3_completion.sql
-- ===========================================================================
SET search_path TO jnpa, public;

-- --- Transporters + vehicle mapping + one active blacklist -------------------
INSERT INTO jnpa.transporters (code, name, gstin, status)
VALUES ('TRP-1001', 'Bhagwati Roadlines', '27AABCB1234C1Z5', 'ACTIVE'),
       ('TRP-1002', 'Konkan Carriers',   '27AAECK5678D1Z2', 'ACTIVE'),
       ('TRP-1003', 'Overload Movers',   '27AAOCO9012E1Z9', 'ACTIVE')
ON CONFLICT (code) DO NOTHING;

INSERT INTO jnpa.transporter_vehicles (transporter_id, vehicle_no, vehicle_no_norm, driver_id)
SELECT t.id, v.vno, regexp_replace(upper(v.vno), '[^A-Z0-9]', '', 'g'), v.did
FROM (VALUES
    ('TRP-1001', 'MH04AB1234', 'DRV-001'),
    ('TRP-1002', 'MH05CD5678', 'DRV-002'),
    ('TRP-1003', 'MH06EF9012', 'DRV-003')
) AS v(code, vno, did)
JOIN jnpa.transporters t ON t.code = v.code
ON CONFLICT (transporter_id, vehicle_no_norm) DO NOTHING;

-- Blacklist "Overload Movers" (demo enforcement target).
INSERT INTO jnpa.transporter_blacklist (transporter_id, reason, severity, blacklisted_by)
SELECT t.id, 'Repeated axle-overload violations at weighbridge', 'HIGH', 'seed'
FROM jnpa.transporters t
WHERE t.code = 'TRP-1003'
  AND NOT EXISTS (SELECT 1 FROM jnpa.transporter_blacklist b
                  WHERE b.transporter_id = t.id AND b.status = 'ACTIVE');
UPDATE jnpa.transporters SET status = 'BLACKLISTED'
WHERE code = 'TRP-1003' AND status <> 'BLACKLISTED';

-- --- Accidents (one open, one resolved) -------------------------------------
INSERT INTO jnpa.accidents (accident_ref, accident_type, severity, lat, lon, location,
                            plate, vehicle_id, description, status, investigation_status, source)
SELECT * FROM (VALUES
    ('ACC-SEED01', 'ENROUTE',  'MAJOR', 18.951, 72.951, '{"segment_id":"SEG-07"}'::jsonb,
     'MH04AB1234', 'TRK-000123', 'Trailer overturn on NH-348 near SEG-07', 'INVESTIGATING', 'IN_PROGRESS', 'MANUAL'),
    ('ACC-SEED02', 'PREMISES', 'MINOR', 18.949, 72.949, '{"gate_id":"G-NSICT"}'::jsonb,
     'MH05CD5678', 'TRK-000124', 'Minor bump inside gate lane', 'RESOLVED', 'COMPLETED', 'MANUAL')
) AS a(accident_ref, accident_type, severity, lat, lon, location, plate, vehicle_id, description, status, investigation_status, source)
WHERE NOT EXISTS (SELECT 1 FROM jnpa.accidents x WHERE x.accident_ref = a.accident_ref);

INSERT INTO jnpa.accident_events (accident_id, action, new_status, note, actor)
SELECT a.id, 'REPORTED', 'REPORTED', a.description, 'seed'
FROM jnpa.accidents a
WHERE a.accident_ref IN ('ACC-SEED01', 'ACC-SEED02')
  AND NOT EXISTS (SELECT 1 FROM jnpa.accident_events e WHERE e.accident_id = a.id);

-- --- Camera-AI counts / trailer / container ---------------------------------
INSERT INTO jnpa.camera_ai_counts (camera_id, gate_id, vehicle_count, queue_count, class_counts, congestion_level, confidence)
SELECT * FROM (VALUES
    ('CAM-G-NSICT-1', 'G-NSICT', 48, 26, '{"hgv":30,"lcv":12,"car":6}'::jsonb, 'HIGH',   0.92),
    ('CAM-G-JNPCT-1', 'G-JNPCT', 22, 9,  '{"hgv":14,"lcv":6,"car":2}'::jsonb,  'MEDIUM', 0.88),
    ('CAM-G-BMCT-1',  'G-BMCT',  11, 3,  '{"hgv":7,"lcv":3,"car":1}'::jsonb,   'LOW',    0.85)
) AS c(camera_id, gate_id, vehicle_count, queue_count, class_counts, congestion_level, confidence)
WHERE NOT EXISTS (SELECT 1 FROM jnpa.camera_ai_counts x WHERE x.camera_id = c.camera_id);

INSERT INTO jnpa.trailer_reads (camera_id, gate_id, trailer_number, plate, vehicle_id, confidence)
SELECT 'CAM-G-NSICT-1', 'G-NSICT', 'HR55T7788', 'MH04AB1234', 'TRK-000123', 0.83
WHERE NOT EXISTS (SELECT 1 FROM jnpa.trailer_reads WHERE trailer_number = 'HR55T7788');

INSERT INTO jnpa.container_reads (camera_id, gate_id, container_number, iso_type, check_digit_ok, valid, plate, confidence)
SELECT 'CAM-G-NSICT-1', 'G-NSICT', 'CSQU3054383', '45G1', true, true, 'MH04AB1234', 0.90
WHERE NOT EXISTS (SELECT 1 FROM jnpa.container_reads WHERE container_number = 'CSQU3054383');

-- --- Document OCR sample ----------------------------------------------------
INSERT INTO jnpa.document_ocr (doc_type, source_ref, fields, confidence, status, source)
SELECT 'LR', 'MH04AB1234',
       '{"lr_number":"LR-77321","consignor":"Bhagwati Roadlines","consignee":"JNPA Terminal","date":"2026-07-16"}'::jsonb,
       0.86, 'EXTRACTED', 'MOCK'
WHERE NOT EXISTS (SELECT 1 FROM jnpa.document_ocr WHERE fields->>'lr_number' = 'LR-77321');

-- --- NVR device + channel map -----------------------------------------------
INSERT INTO jnpa.nvr_devices (id, name, vendor, host, port, protocol, channels, status, source)
VALUES ('NVR-G-NSICT', 'NSICT Gate NVR', 'Hikvision', '10.20.0.11', 554, 'RTSP', 8, 'ONLINE', 'CONFIG')
ON CONFLICT (id) DO NOTHING;

INSERT INTO jnpa.nvr_camera_map (nvr_id, channel, camera_id, stream_url, codec, resolution, fps, status)
SELECT 'NVR-G-NSICT', c.ch, c.cam, 'rtsp://10.20.0.11:554/ch' || c.ch, 'H264', '1920x1080', 25, 'ONLINE'
FROM (VALUES (1, 'CAM-G-NSICT-1'), (2, 'CAM-G-NSICT-2')) AS c(ch, cam)
ON CONFLICT (nvr_id, channel) DO NOTHING;

-- --- ECY TRT: one completed turnaround (Gate-In -> Parking -> Loading -> Gate-Out) ---
INSERT INTO jnpa.trt_records (vehicle_id, plate, trip_id, gate_in_at, parking_at, loading_at, gate_out_at,
                              gate_to_park_min, park_to_load_min, load_to_out_min, trt_min, status)
SELECT 'TRK-000123', 'MH04AB1234', 'TRIP-SEED-1',
       now() - interval '135 min', now() - interval '120 min',
       now() - interval '80 min',  now(),
       15.0, 40.0, 80.0, 135.0, 'COMPLETED'
WHERE NOT EXISTS (SELECT 1 FROM jnpa.trt_records WHERE trip_id = 'TRIP-SEED-1');

-- --- Bottleneck snapshot (top-3) --------------------------------------------
INSERT INTO jnpa.bottleneck_snapshots (rank, segment_id, name, jam_factor, speed_kmh, free_flow_kmh, avg_delay_min, lat, lon)
SELECT * FROM (VALUES
    (1, 'SEG-07', 'Karal Phata approach', 8.4, 7.0,  50.0, 12.6, 18.951, 72.951),
    (2, 'SEG-05', 'Y-junction',           7.1, 12.0, 50.0, 8.2,  18.948, 72.948),
    (3, 'SEG-10', 'Gate cluster merge',   6.5, 16.0, 50.0, 5.9,  18.955, 72.955)
) AS b(rank, segment_id, name, jam_factor, speed_kmh, free_flow_kmh, avg_delay_min, lat, lon)
WHERE NOT EXISTS (SELECT 1 FROM jnpa.bottleneck_snapshots x WHERE x.segment_id = b.segment_id);

-- --- TT double-trip: one completed 2-leg cycle ------------------------------
INSERT INTO jnpa.tt_trips (cycle_id, vehicle_id, driver_id, trip_seq, direction, origin, destination,
                           started_at, ended_at, laden, status)
SELECT * FROM (VALUES
    ('TT-SEED-1', 'TRK-000123', 'DRV-001', 1, 'INBOUND',  'ICD-Dronagiri', 'JNPA-NSICT',
     now() - interval '6 hours', now() - interval '4 hours', true,  'COMPLETED'),
    ('TT-SEED-1', 'TRK-000123', 'DRV-001', 2, 'RETURN',   'JNPA-NSICT',    'ICD-Dronagiri',
     now() - interval '3 hours', now() - interval '1 hours', true,  'COMPLETED')
) AS tt(cycle_id, vehicle_id, driver_id, trip_seq, direction, origin, destination, started_at, ended_at, laden, status)
WHERE NOT EXISTS (SELECT 1 FROM jnpa.tt_trips x WHERE x.cycle_id = 'TT-SEED-1' AND x.trip_seq = tt.trip_seq);
