-- ===========================================================================
-- JNPA Digital Twin — UC-III schema bootstrap.
-- Runs once on first container start (mounted into
-- /docker-entrypoint-initdb.d/). All timestamps are stored in UTC.
-- ===========================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

-- --------------------------------------------------------------------------
-- Reference / master tables
-- --------------------------------------------------------------------------
CREATE TABLE jnpa.gates (
    id        text PRIMARY KEY,
    name      text,
    lat       double precision,
    lon       double precision,
    -- Set by the TFC-1 gate-closure scenario; NULL means open. "Reset to
    -- baseline" clears it back to NULL.
    closed_at timestamptz
);

CREATE TABLE jnpa.cameras (
    id           text PRIMARY KEY,
    gate_id      text REFERENCES jnpa.gates(id),
    name         text,
    lat          double precision,
    lon          double precision,
    role         text CHECK (role IN ('entry','exit','overview','ptz','thermal','anpr')),
    installed_at timestamptz DEFAULT now()
);

CREATE TABLE jnpa.vehicle_master (
    plate             text PRIMARY KEY,
    rc_type           text,
    owner_hash        text,
    fitness_valid_to  date,
    puc_valid_to      date,
    fastag_status     text,
    provisional       boolean DEFAULT false,
    provisional_until timestamptz,
    -- Canonical Parivahan RC fields written back by the Vahan service
    -- (ingest/vahan_sim, ingest/vahan_live) on every successful /vahan/rc/*.
    owner_name_masked text,
    vehicle_class     text,
    fuel_type         text,
    insurance_valid_to date,
    registration_date  date,
    state             text,
    rto_code          text,
    blacklist_status  text DEFAULT 'CLEAR',
    updated_at        timestamptz DEFAULT now()
);

-- --------------------------------------------------------------------------
-- Service registry. Each ingest/lookup service upserts its own row on
-- startup; the fallback orchestrator (Prompt 4) reads this to decide between
-- the simulator and the live (Surepass) adapter.
-- --------------------------------------------------------------------------
CREATE TABLE jnpa.services (
    name          text NOT NULL,         -- logical service, e.g. 'vahan'
    kind          text NOT NULL,         -- 'sim' | 'live'
    base_url      text NOT NULL,         -- reachable on the jnpa network
    healthy       boolean DEFAULT true,
    enabled       boolean DEFAULT true,
    registered_at timestamptz DEFAULT now(),
    meta          jsonb DEFAULT '{}'::jsonb,
    PRIMARY KEY (name, kind)
);

-- --------------------------------------------------------------------------
-- Driver enrolment (UC-III Identity / face-recognition, Appendix C #2).
-- The Driver PWA submits a profile + consented reference face frames; an admin
-- (DTCCC_ADMIN / CUSTOMS) reviews and approves, at which point the identity
-- service generates + stores the face template and the reference photo is
-- persisted to MinIO. DPDP: PoC frames are synthetic/consented only; raw pixels
-- are kept only until approval (then the template + an object-store pointer
-- remain). `reference_image` keeps one approved frame so the template can be
-- rebuilt if the in-memory identity service restarts.
-- --------------------------------------------------------------------------
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
    face_images       jsonb NOT NULL DEFAULT '[]'::jsonb,  -- captured frames pending review
    reference_image   text,                                -- approved canonical frame (base64)
    photo_url         text,                                -- MinIO object URL after approval
    documents         jsonb NOT NULL DEFAULT '[]'::jsonb,  -- uploaded id/licence docs
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

-- Append-only DPDP audit of every enrolment lifecycle event (who, what, when).
CREATE TABLE IF NOT EXISTS jnpa.enrollment_audit (
    id        bigserial PRIMARY KEY,
    driver_id text NOT NULL,
    event     text NOT NULL,   -- SUBMITTED|APPROVED|REJECTED|REENROLL_REQUESTED|CONSENT
    actor     text,
    detail    jsonb NOT NULL DEFAULT '{}'::jsonb,
    ts        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_enrollment_audit_driver
    ON jnpa.enrollment_audit (driver_id, ts DESC);

-- Master driver identity (production data model). `driver_enrollments` above is the
-- WORKFLOW/request table (PENDING→ACTIVE…); this `drivers` table is the canonical
-- identity record an enrolment is PROMOTED into on admin approval. Verification
-- reads the active driver from here. Embeddings live in the identity service; this
-- holds the durable profile + the reference-photo pointer + template metadata.
CREATE TABLE IF NOT EXISTS jnpa.drivers (
    driver_id         text PRIMARY KEY,
    name              text NOT NULL,
    license_no        text,
    mobile            text,
    vehicle_no        text,
    aadhaar_masked    text,
    emergency_contact text,
    status            text NOT NULL DEFAULT 'ACTIVE'
                      CHECK (status IN ('ACTIVE', 'SUSPENDED')),
    photo_url         text,                -- MinIO object URL (drivers/ bucket)
    reference_image   text,                -- base64 reference frame (dev fallback only)
    template_dim      int,
    provider          text,                -- onnx | synthetic
    enrolled_at       timestamptz NOT NULL DEFAULT now(),
    approved_by       text,
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Biometric template store for 1:N identification. One unit-norm ArcFace
-- embedding per active driver; a captured face is matched by nearest cosine
-- across this set (no pgvector in this image, so the search is in-app — fine for
-- thousands of drivers; swap to pgvector/FAISS for larger fleets).
CREATE TABLE IF NOT EXISTS jnpa.driver_faces (
    driver_id     text PRIMARY KEY,
    embedding     jsonb NOT NULL,        -- L2-normalised vector (length = dim)
    dim           int NOT NULL,
    provider      text,
    model_version text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- Append-only verification audit trail (every /verify decision, who/score/path).
CREATE TABLE IF NOT EXISTS jnpa.verification_logs (
    id            bigserial PRIMARY KEY,
    driver_id     text NOT NULL,
    decision      text NOT NULL,          -- VERIFIED | PROVISIONAL | REJECTED
    score         double precision,
    matched       boolean,
    provider      text,                   -- onnx | synthetic | unavailable
    decision_path text,                   -- LIVE | SYNTHETIC
    actor         text,
    purpose       text,
    reason        text,
    ts            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_verification_logs_driver
    ON jnpa.verification_logs (driver_id, ts DESC);

-- --------------------------------------------------------------------------
-- Time-series (hypertables)
-- --------------------------------------------------------------------------
CREATE TABLE jnpa.anpr_reads (
    ts            timestamptz NOT NULL,
    camera_id     text,
    plate         text,
    conf          real,
    vehicle_class text,
    image_url     text,
    weather       text,
    degraded      boolean DEFAULT false
);
SELECT create_hypertable('jnpa.anpr_reads', 'ts');

CREATE TABLE jnpa.rfid_reads (
    ts        timestamptz NOT NULL,
    reader_id text,
    tag_id    text,
    rssi      real
);
SELECT create_hypertable('jnpa.rfid_reads', 'ts');

CREATE TABLE jnpa.truck_telemetry (
    ts         timestamptz NOT NULL,
    device_id  text,
    plate      text,
    lat        double precision,
    lon        double precision,
    speed_kmh  real,
    heading    real,
    battery    real,
    accuracy_m real
);
SELECT create_hypertable('jnpa.truck_telemetry', 'ts');

CREATE TABLE jnpa.traffic_snapshots (
    ts         timestamptz NOT NULL,
    segment_id text,
    speed_kmh  real,
    jam_factor real,
    source     text
);
SELECT create_hypertable('jnpa.traffic_snapshots', 'ts');

-- --------------------------------------------------------------------------
-- Operational tables
-- --------------------------------------------------------------------------
CREATE TABLE jnpa.alerts (
    id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ts       timestamptz DEFAULT now(),
    kind     text,
    severity text,
    gate_id  text,
    plate    text,
    payload  jsonb,
    ack      boolean DEFAULT false
);

CREATE TABLE jnpa.scenarios (
    id         text PRIMARY KEY,
    name       text,
    started_at timestamptz,
    ended_at   timestamptz,
    params     jsonb
);

-- Per-run scenario handles (Sub-Criterion 5). One row per /scenarios/{name}/run
-- so a run is replayable: ``params`` keeps the trigger params + a ``steps[]``
-- array (each step records its trigger source for the reactive-workflow audit).
CREATE TABLE IF NOT EXISTS jnpa.scenario_handles (
    handle_id  text PRIMARY KEY,
    name       text NOT NULL,            -- tfc1 | tfc2 | tfc3
    status     text NOT NULL DEFAULT 'RUNNING',  -- RUNNING | DONE | RESET | FAILED
    params     jsonb NOT NULL DEFAULT '{}'::jsonb,
    trace_id   text,                     -- W3C traceparent for Jaeger deep-link
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at   timestamptz
);

-- Event-by-event scenario timeline (the storyline the dashboard paints). Every
-- downstream action a scenario causes is appended here AND pushed to /api/ws as
-- type=scenario_step, so the timeline survives a page reload (replay).
CREATE TABLE IF NOT EXISTS jnpa.scenario_steps (
    id          bigserial PRIMARY KEY,
    handle_id   text NOT NULL REFERENCES jnpa.scenario_handles(handle_id) ON DELETE CASCADE,
    step_no     int NOT NULL,
    ts          timestamptz NOT NULL DEFAULT now(),
    title       text NOT NULL,
    status      text NOT NULL DEFAULT 'ok',   -- ok | degraded | failed | info
    trigger     text,                          -- the trigger source (audit)
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_scenario_steps_handle ON jnpa.scenario_steps (handle_id, step_no);

-- Helpful secondary indexes for the read paths the gateway uses.
CREATE INDEX IF NOT EXISTS idx_anpr_plate_ts ON jnpa.anpr_reads (plate, ts DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_plate_ts ON jnpa.truck_telemetry (plate, ts DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON jnpa.alerts (ts DESC);
-- Vehicle-intel read path: alerts by plate, newest first (migration 0014).
CREATE INDEX IF NOT EXISTS idx_alerts_plate ON jnpa.alerts (plate, ts DESC);

-- ===========================================================================
-- Seed data
-- ===========================================================================

-- 4 gates at berth-line centroids aligned to the JNPA satellite reference
-- (methodology + values adapted from jnpa_poc_2 config/terminals.json, which
-- fine-tuned each terminal onto the developed berth rather than open water).
-- Display coordinates only: gate throughput joins on jnpa.cameras.gate_id, and
-- the truck simulator's routing coords live in trucking_app/gates.py (unchanged).
INSERT INTO jnpa.gates (id, name, lat, lon) VALUES
    ('G-NSICT', 'Nhava Sheva International Container Terminal', 18.9527, 72.9505),
    ('G-JNPCT', 'Jawaharlal Nehru Port Container Terminal',     18.9497, 72.9479),
    ('G-NSIGT', 'Nhava Sheva India Gateway Terminal',           18.9550, 72.9525),
    ('G-BMCT',  'Bharat Mumbai Container Terminals',            18.9386, 72.9383);

-- 12 gate cameras (3 per gate: entry, exit, overview).
INSERT INTO jnpa.cameras (id, gate_id, name, lat, lon, role) VALUES
    ('CAM-NSICT-ENT', 'G-NSICT', 'NSICT Entry Lane',   18.9491, 72.9490, 'entry'),
    ('CAM-NSICT-EXT', 'G-NSICT', 'NSICT Exit Lane',    18.9487, 72.9494, 'exit'),
    ('CAM-NSICT-OVW', 'G-NSICT', 'NSICT Overview',     18.9489, 72.9492, 'overview'),

    ('CAM-JNPCT-ENT', 'G-JNPCT', 'JNPCT Entry Lane',   18.9514, 72.9503, 'entry'),
    ('CAM-JNPCT-EXT', 'G-JNPCT', 'JNPCT Exit Lane',    18.9510, 72.9507, 'exit'),
    ('CAM-JNPCT-OVW', 'G-JNPCT', 'JNPCT Overview',     18.9512, 72.9505, 'overview'),

    ('CAM-NSIGT-ENT', 'G-NSIGT', 'NSIGT Entry Lane',   18.9459, 72.9529, 'entry'),
    ('CAM-NSIGT-EXT', 'G-NSIGT', 'NSIGT Exit Lane',    18.9455, 72.9533, 'exit'),
    ('CAM-NSIGT-OVW', 'G-NSIGT', 'NSIGT Overview',     18.9457, 72.9531, 'overview'),

    ('CAM-BMCT-ENT',  'G-BMCT',  'BMCT Entry Lane',    18.9422, 72.9558, 'entry'),
    ('CAM-BMCT-EXT',  'G-BMCT',  'BMCT Exit Lane',     18.9418, 72.9562, 'exit'),
    ('CAM-BMCT-OVW',  'G-BMCT',  'BMCT Overview',      18.9420, 72.9560, 'overview');

-- 6 corridor cameras along NH-348 between the gates and Karal Phata.
INSERT INTO jnpa.cameras (id, gate_id, name, lat, lon, role) VALUES
    ('CAM-COR-01', NULL, 'NH-348 Corridor KM 03 (ANPR)',  18.9100, 72.9700, 'anpr'),
    ('CAM-COR-02', NULL, 'NH-348 Corridor KM 06 (PTZ)',   18.8850, 72.9900, 'ptz'),
    ('CAM-COR-03', NULL, 'NH-348 Corridor KM 09 (ANPR)',  18.8600, 73.0100, 'anpr'),
    ('CAM-COR-04', NULL, 'NH-348 Corridor KM 12 (Thermal)',18.8400, 73.0300, 'thermal'),
    ('CAM-COR-05', NULL, 'NH-348 Corridor KM 16 (ANPR)',  18.8150, 73.0550, 'anpr'),
    ('CAM-COR-06', NULL, 'Karal Phata Junction (Overview)',18.7800, 73.0800, 'overview');

-- A couple of vehicle_master rows so downstream PoCs have lookups to hit.
INSERT INTO jnpa.vehicle_master
    (plate, rc_type, owner_hash, fitness_valid_to, puc_valid_to, fastag_status, provisional)
VALUES
    ('MH04AB1234', 'HGV', 'sha256:seed-owner-a', '2027-03-31', '2026-09-30', 'active', false),
    ('MH43CD5678', 'HGV', 'sha256:seed-owner-b', '2026-08-15', '2026-07-01', 'low_balance', false);

-- ===========================================================================
-- Materialised KPI views (Sub-Criterion 3).
-- The API gateway's /api/kpi/{view} surface reads these. They are defined as
-- plain (lazy) views over the hypertables + operational tables so they work on
-- a single-broker / single-node PoC without continuous-aggregate policies; the
-- gateway tolerates any that are missing (older volumes) by returning [].
-- ===========================================================================

-- Per-gate vehicle throughput, last 24h, bucketed hourly. Cameras are mapped to
-- their gate; corridor cameras (gate_id NULL) bucket under 'CORRIDOR'.
CREATE OR REPLACE VIEW jnpa.kpi_gate_throughput AS
SELECT
    time_bucket('1 hour', a.ts)            AS bucket,
    COALESCE(c.gate_id, 'CORRIDOR')        AS gate_id,
    count(*)                               AS reads,
    count(DISTINCT a.plate)                AS unique_plates
FROM jnpa.anpr_reads a
LEFT JOIN jnpa.cameras c ON c.id = a.camera_id
WHERE a.ts > now() - interval '24 hours'
GROUP BY 1, 2
ORDER BY 1 DESC, 2;

-- Median gate dwell proxy: time trucks spend in the AT_GATE_QUEUE speed band
-- (<= 3 km/h) per gate-adjacent telemetry, last 6h. Uses the nearest-by-plate
-- vehicle_master gate is not tracked here, so we report the corridor-wide
-- stationary share as a congestion proxy.
CREATE OR REPLACE VIEW jnpa.kpi_gate_dwell AS
SELECT
    time_bucket('15 minutes', ts)          AS bucket,
    count(*) FILTER (WHERE speed_kmh <= 3) AS stationary_pings,
    count(*)                               AS total_pings,
    round(100.0 * count(*) FILTER (WHERE speed_kmh <= 3)
          / NULLIF(count(*), 0), 1)        AS stationary_pct
FROM jnpa.truck_telemetry
WHERE ts > now() - interval '6 hours'
GROUP BY 1
ORDER BY 1 DESC;

-- Hourly ANPR volume + degraded-read share (camera-feed fallback health).
CREATE OR REPLACE VIEW jnpa.kpi_anpr_hourly AS
SELECT
    time_bucket('1 hour', ts)                       AS bucket,
    count(*)                                        AS reads,
    count(*) FILTER (WHERE degraded)                AS degraded_reads,
    round(avg(conf)::numeric, 3)                    AS avg_conf
FROM jnpa.anpr_reads
WHERE ts > now() - interval '24 hours'
GROUP BY 1
ORDER BY 1 DESC;

-- Latest speed + jam factor per corridor segment (map overlay / congestion KPI).
CREATE OR REPLACE VIEW jnpa.kpi_corridor_speed AS
SELECT DISTINCT ON (segment_id)
    segment_id,
    ts,
    speed_kmh,
    jam_factor,
    source
FROM jnpa.traffic_snapshots
ORDER BY segment_id, ts DESC;

-- Alert volume by kind + severity, last 24h (control-room summary).
CREATE OR REPLACE VIEW jnpa.kpi_alerts_by_kind AS
SELECT
    kind,
    severity,
    count(*)               AS total,
    count(*) FILTER (WHERE NOT ack) AS open
FROM jnpa.alerts
WHERE ts > now() - interval '24 hours'
GROUP BY 1, 2
ORDER BY 3 DESC;

-- Vehicles currently inside their provisional 24h cure window (Sub-Criterion 3).
CREATE OR REPLACE VIEW jnpa.kpi_provisional_open AS
SELECT
    plate,
    provisional_until,
    round(EXTRACT(EPOCH FROM (provisional_until - now())) / 3600.0, 2) AS hours_remaining,
    updated_at
FROM jnpa.vehicle_master
WHERE provisional = true
  AND provisional_until IS NOT NULL
  AND provisional_until > now()
ORDER BY provisional_until ASC;

-- ===========================================================================
-- Gate lifecycle events + Appendix-C gate KPIs.
-- The truck-sim (primary GPS device feed) emits one row per state transition of
-- a port visit: GATE_ARRIVAL (joined queue), GATE_TXN_START (boom processing
-- began), GATE_IN (boom cleared / admitted), GATE_OUT (left the port). The KPI
-- views pair these per trip_id to derive real, event-driven KPI values — no
-- hardcoded numbers. The gateway also auto-provisions this table + views at boot
-- (ensure_kpi_gate_schema) so pre-existing volumes gain them without a reset.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS jnpa.gate_events (
    id         bigserial PRIMARY KEY,
    ts         timestamptz NOT NULL DEFAULT now(),
    device_id  text NOT NULL,
    plate      text,
    gate_id    text,
    trip_id    text NOT NULL,
    event_type text NOT NULL
               CHECK (event_type IN ('GATE_ARRIVAL','GATE_TXN_START','GATE_IN','GATE_OUT')),
    lat        double precision,
    lon        double precision
);
CREATE INDEX IF NOT EXISTS idx_gate_events_trip ON jnpa.gate_events (trip_id);
CREATE INDEX IF NOT EXISTS idx_gate_events_type_ts ON jnpa.gate_events (event_type, ts DESC);
CREATE INDEX IF NOT EXISTS idx_gate_events_ts ON jnpa.gate_events (ts DESC);

-- One row per port visit with the four phase timestamps pivoted out (last 24h).
CREATE OR REPLACE VIEW jnpa.kpi_gate_trip_timeline AS
SELECT
    trip_id,
    max(gate_id)                                        AS gate_id,
    max(plate)                                          AS plate,
    min(ts) FILTER (WHERE event_type = 'GATE_ARRIVAL')   AS arrival_ts,
    min(ts) FILTER (WHERE event_type = 'GATE_TXN_START') AS txn_start_ts,
    min(ts) FILTER (WHERE event_type = 'GATE_IN')        AS gate_in_ts,
    min(ts) FILTER (WHERE event_type = 'GATE_OUT')       AS gate_out_ts
FROM jnpa.gate_events
WHERE ts > now() - interval '24 hours'
GROUP BY trip_id;

-- KPI 1 — Gate Queue Wait Time: GATE_ARRIVAL -> GATE_TXN_START, per 15-min bucket.
CREATE OR REPLACE VIEW jnpa.kpi_gate_queue_wait AS
SELECT
    time_bucket('15 minutes', txn_start_ts)                                  AS bucket,
    round(avg(EXTRACT(EPOCH FROM (txn_start_ts - arrival_ts)))::numeric/60.0, 2) AS wait_min,
    count(*)                                                                 AS trips
FROM jnpa.kpi_gate_trip_timeline
WHERE arrival_ts IS NOT NULL AND txn_start_ts IS NOT NULL
  AND txn_start_ts >= arrival_ts
GROUP BY 1
ORDER BY 1 DESC;

-- KPI 2 — Avg Gate Transaction Time: GATE_TXN_START -> GATE_IN, per 15-min bucket.
CREATE OR REPLACE VIEW jnpa.kpi_gate_txn_time AS
SELECT
    time_bucket('15 minutes', gate_in_ts)                                    AS bucket,
    round(avg(EXTRACT(EPOCH FROM (gate_in_ts - txn_start_ts)))::numeric/60.0, 2) AS txn_min,
    count(*)                                                                 AS trips
FROM jnpa.kpi_gate_trip_timeline
WHERE txn_start_ts IS NOT NULL AND gate_in_ts IS NOT NULL
  AND gate_in_ts >= txn_start_ts
GROUP BY 1
ORDER BY 1 DESC;

-- KPI 4 — Turn-Around Time inside port: GATE_IN -> GATE_OUT, per 15-min bucket.
CREATE OR REPLACE VIEW jnpa.kpi_tat_inside_port AS
SELECT
    time_bucket('15 minutes', gate_out_ts)                                   AS bucket,
    round(avg(EXTRACT(EPOCH FROM (gate_out_ts - gate_in_ts)))::numeric/60.0, 2) AS tat_min,
    count(*)                                                                 AS trips
FROM jnpa.kpi_gate_trip_timeline
WHERE gate_in_ts IS NOT NULL AND gate_out_ts IS NOT NULL
  AND gate_out_ts >= gate_in_ts
GROUP BY 1
ORDER BY 1 DESC;

-- ===========================================================================
-- Geo-fence zones (UC-III Sub-Criterion 4 — Geo-fencing Manager).
-- The dashboard's terra-draw editor PUTs no-parking / restricted polygons here;
-- the behavioural anomaly service (ai/anomaly) reads them live to decide
-- ILLEGAL_PARKING. Polygons are stored as GeoJSON-style ring coordinates
-- ([[lon,lat], ...]) in `polygon`. `escalation` holds the editable
-- 5/15/30-minute escalation thresholds (minutes) for the zone.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS jnpa.geofence_zones (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    kind        text NOT NULL DEFAULT 'no_parking'
                CHECK (kind IN ('no_parking', 'restricted')),
    polygon     jsonb NOT NULL,                 -- [[lon,lat], ...] outer ring
    escalation  jsonb NOT NULL DEFAULT '{"warn_min":5,"notice_min":15,"challan_min":30}'::jsonb,
    enabled     boolean NOT NULL DEFAULT true,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Seed the six corridor no-parking zones (mirror jnpa_shared.corridor.NO_PARK_ZONES).
-- Half-extents ~0.0005 deg lat; lon widened by 1/cos(18.9 deg) ~= 1.0573 so the
-- footprint is roughly square on the ground. Rings are [lon,lat] (GeoJSON order).
INSERT INTO jnpa.geofence_zones (id, name, kind, polygon) VALUES
    ('NPZ-GATE-NSICT', 'NSICT Gate-1 apron', 'no_parking',
     '[[72.948671,18.9484],[72.949729,18.9484],[72.949729,18.9494],[72.948671,18.9494],[72.948671,18.9484]]'::jsonb),
    ('NPZ-GATE-JNPCT', 'JNPCT gate throat', 'no_parking',
     '[[72.949971,18.9507],[72.951029,18.9507],[72.951029,18.9517],[72.949971,18.9517],[72.949971,18.9507]]'::jsonb),
    ('NPZ-YJUNCTION', 'NH-348 Y-junction', 'no_parking',
     '[[72.969971,18.9210],[72.971029,18.9210],[72.971029,18.9220],[72.969971,18.9220],[72.969971,18.9210]]'::jsonb),
    ('NPZ-FLYOVER-RAMP', 'KM-6 flyover ramp', 'no_parking',
     '[[72.989471,18.8845],[72.990529,18.8845],[72.990529,18.8855],[72.989471,18.8855],[72.989471,18.8845]]'::jsonb),
    ('NPZ-WEIGHBRIDGE', 'KM-12 weighbridge approach', 'restricted',
     '[[73.029471,18.8395],[73.030529,18.8395],[73.030529,18.8405],[73.029471,18.8405],[73.029471,18.8395]]'::jsonb),
    ('NPZ-KARAL-JUNCTION', 'Karal Phata junction', 'no_parking',
     '[[73.079471,18.7795],[73.080529,18.7795],[73.080529,18.7805],[73.079471,18.7805],[73.079471,18.7795]]'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- ===========================================================================
-- Enforcement system-of-record (additive; gateway/enforcement.py applies the
-- same idempotent DDL at runtime so existing volumes gain these without a
-- re-init). These NEVER alter jnpa.alerts data — the alerts feed is unchanged.
--   violation_cases : lifecycle anchor (DETECTED..CLOSED), one row per case.
--   challans        : immutable-after-issue, sequenced legal record (1 per case).
--   case_audit      : append-only, hash-chained transition log (tamper-evident).
-- ===========================================================================
CREATE SEQUENCE IF NOT EXISTS jnpa.challan_seq START 1001;

CREATE TABLE IF NOT EXISTS jnpa.violation_cases (
    case_id           uuid PRIMARY KEY,
    vehicle_number    text,
    driver_id         text,
    first_detected_at timestamptz NOT NULL DEFAULT now(),
    last_updated_at   timestamptz NOT NULL DEFAULT now(),
    status            text NOT NULL DEFAULT 'DETECTED'
                      CHECK (status IN ('DETECTED','REVIEWED','CONFIRMED',
                                        'CHALLAN_ISSUED','PAID','CLOSED')),
    total_fine        integer NOT NULL DEFAULT 0,
    evidence_url      text,
    evidence_sha256   text,
    gate_id           text,
    confidence        double precision
);
CREATE INDEX IF NOT EXISTS idx_violation_cases_status
    ON jnpa.violation_cases (status, last_updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_violation_cases_plate
    ON jnpa.violation_cases (vehicle_number, first_detected_at DESC);

CREATE TABLE IF NOT EXISTS jnpa.challans (
    challan_id      uuid PRIMARY KEY,
    challan_no      text UNIQUE,
    case_id         uuid NOT NULL UNIQUE,
    vehicle_number  text,
    total_fine      integer NOT NULL DEFAULT 0,
    status          text NOT NULL DEFAULT 'ISSUED'
                    CHECK (status IN ('ISSUED','PAID','DISPUTED','CLOSED')),
    mva_section     text,
    issued_at       timestamptz NOT NULL DEFAULT now(),
    payment_ref     text,
    pdf_url         text,
    evidence_sha256 text,
    created_by      text
);
CREATE INDEX IF NOT EXISTS idx_challans_case ON jnpa.challans (case_id);
-- Vehicle-intel read path: challans by vehicle, newest first (migration 0014).
CREATE INDEX IF NOT EXISTS idx_challans_vehicle ON jnpa.challans (vehicle_number, issued_at DESC);

CREATE TABLE IF NOT EXISTS jnpa.case_audit (
    id          bigserial PRIMARY KEY,
    case_id     uuid NOT NULL,
    event       text NOT NULL,
    from_status text,
    to_status   text,
    actor       text,
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    prev_hash   text,
    hash        text,
    ts          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_case_audit_case ON jnpa.case_audit (case_id, id);

-- Defence-in-depth idempotency: at most one console-issued alert per (case,kind).
-- Partial index, so other alert sources (anomaly/customs/...) are unaffected.
CREATE UNIQUE INDEX IF NOT EXISTS uq_alerts_case_kind
    ON jnpa.alerts ((payload->>'case_id'), kind)
    WHERE payload->>'source' = 'violation-console';

-- ===========================================================================
-- ULIP FASTag foundation layer (greenfield). Three vendor APIs land here:
--   * RC -> FASTag Balance     -> jnpa.fastag_balance      (RC-keyed snapshot)
--   * RC -> FASTag Transaction -> jnpa.fastag_transactions (one row per crossing)
--   * Toll Enroute             -> jnpa.toll_enroute        (route + plaza JSONB)
-- Money is NUMERIC(10,2) everywhere (never float); all timestamps are timestamptz.
-- All DDL is IF NOT EXISTS so a running DB can be topped up by re-applying this
-- block (init.sql itself only runs on first container start).
-- ===========================================================================

-- A) RC-based FASTag balance snapshot (latest known state per registration).
CREATE TABLE IF NOT EXISTS jnpa.fastag_balance (
    rc_number                 text PRIMARY KEY,
    tag_id                    text,
    provider_name             text,
    provider_code             text,
    customer_name             text,
    available_recharge_limit  numeric(10,2),
    available_balance         numeric(10,2),
    tag_status                text,
    vehicle_class             text,
    vehicle_class_desc        text,
    model_name                text,              -- nullable per ULIP spec
    updated_at                timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fastag_balance_tag
    ON jnpa.fastag_balance (tag_id);

-- B) FASTag plaza transactions (RC -> Transaction API). seq_no is the vendor's
-- idempotency key: UNIQUE so a replayed batch cannot double-insert a crossing.
CREATE TABLE IF NOT EXISTS jnpa.fastag_transactions (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tag_id                 text,
    rc_number              text,
    seq_no                 text UNIQUE,
    transaction_date_time  timestamptz,
    lane_direction         text,
    toll_plaza_name        text,
    toll_plaza_geocode     text,               -- raw "lat,lng" as returned by vendor
    vehicle_type           text,
    bank_name              text,               -- batch-level (provider returns once per lookup)
    status                 text,               -- batch-level tag status
    created_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fastag_txn_rc
    ON jnpa.fastag_transactions (rc_number, transaction_date_time DESC);
CREATE INDEX IF NOT EXISTS idx_fastag_txn_tag
    ON jnpa.fastag_transactions (tag_id, transaction_date_time DESC);

-- C) Toll Enroute route lookups. The full toll_plaza_details array is preserved
-- verbatim as JSONB (name, cost, lat, lng per plaza) so no array data is lost.
CREATE TABLE IF NOT EXISTS jnpa.toll_enroute (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id           text,
    source_state        text,
    source_name         text,
    destination_state   text,
    destination_name    text,
    vehicle_type        text,
    duration            text,
    distance            numeric(10,2),
    toll_plaza_details  jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_toll_enroute_route
    ON jnpa.toll_enroute (source_name, destination_name, created_at DESC);

-- ===========================================================================
-- Common audit & persistence framework (single source of truth) — see
-- infra/postgres/migrations/0003_audit_persistence.sql for the standalone/RDS
-- apply path and gateway/audit.py::ensure_audit_schema() for runtime top-up.
--   api_audit_log       : every external API request + response (integration audit)
--   digital_twin_events : every operational/AI event, unified timeline
--   notifications       : delivery audit trail (webpush/sms/ws/email)
--   decision_audit      : durable replacement for the in-memory DecisionRing
--   geofence_events     : zone enter/exit + dwell violations
-- All DDL is IF NOT EXISTS so re-applying never touches existing data.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS jnpa.api_audit_log (
    id               bigserial PRIMARY KEY,
    service_name     text NOT NULL,
    endpoint         text,
    method           text,
    request_payload  jsonb NOT NULL DEFAULT '{}'::jsonb,
    response_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    status_code      integer,
    latency_ms       numeric(10,2),
    error            text,
    transaction_id   text,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_api_audit_service_ts ON jnpa.api_audit_log (service_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_audit_txn        ON jnpa.api_audit_log (transaction_id);
CREATE INDEX IF NOT EXISTS idx_api_audit_ts         ON jnpa.api_audit_log (created_at DESC);

CREATE TABLE IF NOT EXISTS jnpa.digital_twin_events (
    id           bigserial PRIMARY KEY,
    event_type   text NOT NULL,
    vehicle_id   text,
    driver_id    text,
    location     jsonb NOT NULL DEFAULT '{}'::jsonb,
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dt_events_type_ts    ON jnpa.digital_twin_events (event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dt_events_vehicle_ts ON jnpa.digital_twin_events (vehicle_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dt_events_driver_ts  ON jnpa.digital_twin_events (driver_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dt_events_ts         ON jnpa.digital_twin_events (created_at DESC);

CREATE TABLE IF NOT EXISTS jnpa.notifications (
    id                bigserial PRIMARY KEY,
    event_id          text,
    channel           text NOT NULL,
    receiver          text,
    message           text,
    delivery_status   text NOT NULL DEFAULT 'PENDING'
                      CHECK (delivery_status IN
                             ('PENDING','SENT','DELIVERED','FAILED','SKIPPED','NO_SUBSCRIPTION')),
    provider_response jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_notifications_ts       ON jnpa.notifications (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_receiver ON jnpa.notifications (receiver, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_status   ON jnpa.notifications (delivery_status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_event    ON jnpa.notifications (event_id);

CREATE TABLE IF NOT EXISTS jnpa.decision_audit (
    id            bigserial PRIMARY KEY,
    request_id    text,
    input_data    jsonb NOT NULL DEFAULT '{}'::jsonb,
    rule_executed text,
    decision      text,
    action_taken  text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_decision_audit_ts      ON jnpa.decision_audit (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decision_audit_request ON jnpa.decision_audit (request_id);
CREATE INDEX IF NOT EXISTS idx_decision_audit_rule    ON jnpa.decision_audit (rule_executed, created_at DESC);

-- Workflow Composer: operator-authored IF/THEN automation rules + execution log
-- (audit closure). The gateway also auto-provisions these via CREATE TABLE IF
-- NOT EXISTS so a running stack gains them without a fresh init.
CREATE TABLE IF NOT EXISTS jnpa.automation_rules (
    id         text PRIMARY KEY,
    name       text NOT NULL,
    enabled    boolean NOT NULL DEFAULT true,
    field      text NOT NULL,
    op         text NOT NULL,
    value      text NOT NULL,
    actions    jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS jnpa.automation_executions (
    id            bigserial PRIMARY KEY,
    ts            timestamptz NOT NULL DEFAULT now(),
    event         jsonb NOT NULL,
    results       jsonb NOT NULL,
    matched_count int NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_automation_exec_ts ON jnpa.automation_executions (ts DESC);

CREATE TABLE IF NOT EXISTS jnpa.geofence_events (
    id             bigserial PRIMARY KEY,
    vehicle_id     text,
    zone_id        text,
    entry_time     timestamptz,
    exit_time      timestamptz,
    violation_type text,
    action_taken   text,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_geofence_events_vehicle ON jnpa.geofence_events (vehicle_id, entry_time DESC);
CREATE INDEX IF NOT EXISTS idx_geofence_events_zone    ON jnpa.geofence_events (zone_id, entry_time DESC);
CREATE INDEX IF NOT EXISTS idx_geofence_events_ts      ON jnpa.geofence_events (created_at DESC);

-- ===========================================================================
-- Customs & Gate systems (Phase 2) — e-Seal / Form-13 / Weighbridge / ICEGATE
-- capture + Auto-LEO reconciliation. See migration 0004_gate_customs.sql and
-- gate-data/persistence.py::ensure_gate_schema for the runtime top-up path.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS jnpa.gate_captures (
    id            bigserial PRIMARY KEY,
    capture_type  text NOT NULL
                  CHECK (capture_type IN ('ESEAL','FORM13','WEIGHBRIDGE','ICEGATE')),
    container_no  text,
    vehicle_plate text,
    gate_id       text,
    source_mode   text NOT NULL DEFAULT 'sim',
    status        text,
    captured_at   timestamptz,
    payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (container_no, capture_type, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_gate_captures_container ON jnpa.gate_captures (container_no);
CREATE INDEX IF NOT EXISTS idx_gate_captures_plate     ON jnpa.gate_captures (vehicle_plate);
CREATE INDEX IF NOT EXISTS idx_gate_captures_type_ts   ON jnpa.gate_captures (capture_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gate_captures_ts        ON jnpa.gate_captures (created_at DESC);

CREATE TABLE IF NOT EXISTS jnpa.leo_reconciliation (
    id             bigserial PRIMARY KEY,
    container_no   text,
    vehicle_plate  text,
    leo_ready      boolean NOT NULL DEFAULT false,
    customs_flags  jsonb NOT NULL DEFAULT '[]'::jsonb,
    checks         jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_mode    text NOT NULL DEFAULT 'sim',
    reconciled_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_leo_recon_container ON jnpa.leo_reconciliation (container_no, reconciled_at DESC);
CREATE INDEX IF NOT EXISTS idx_leo_recon_ready     ON jnpa.leo_reconciliation (leo_ready, reconciled_at DESC);
CREATE INDEX IF NOT EXISTS idx_leo_recon_ts        ON jnpa.leo_reconciliation (reconciled_at DESC);

-- ==== Parking Management (migration 0005) ====
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;
CREATE TABLE IF NOT EXISTS jnpa.parking_facilities (
    id            text PRIMARY KEY,
    facility_name text NOT NULL,
    location      jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {lat,lon,gate_id}
    capacity      integer NOT NULL DEFAULT 0,
    status        text NOT NULL DEFAULT 'OPEN',         -- OPEN | CLOSED | FULL
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS jnpa.parking_slots (
    id                  bigserial PRIMARY KEY,
    facility_id         text NOT NULL REFERENCES jnpa.parking_facilities(id) ON DELETE CASCADE,
    slot_number         text NOT NULL,
    availability_status text NOT NULL DEFAULT 'AVAILABLE'
                        CHECK (availability_status IN ('AVAILABLE','OCCUPIED','RESERVED','OUT_OF_SERVICE')),
    vehicle_id          text,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (facility_id, slot_number)
);
CREATE INDEX IF NOT EXISTS idx_parking_slots_facility ON jnpa.parking_slots (facility_id, availability_status);
CREATE INDEX IF NOT EXISTS idx_parking_slots_vehicle  ON jnpa.parking_slots (vehicle_id);
CREATE TABLE IF NOT EXISTS jnpa.parking_transactions (
    id          bigserial PRIMARY KEY,
    vehicle_id  text,
    driver_id   text,
    facility_id text,
    slot_id     bigint,
    entry_time  timestamptz NOT NULL DEFAULT now(),
    exit_time   timestamptz,
    duration    interval,
    status      text NOT NULL DEFAULT 'ACTIVE'
                CHECK (status IN ('ACTIVE','COMPLETED','EXPIRED')),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_parking_txn_vehicle ON jnpa.parking_transactions (vehicle_id, entry_time DESC);
CREATE INDEX IF NOT EXISTS idx_parking_txn_status  ON jnpa.parking_transactions (status, entry_time DESC);
CREATE INDEX IF NOT EXISTS idx_parking_txn_facility ON jnpa.parking_transactions (facility_id, entry_time DESC);
CREATE TABLE IF NOT EXISTS jnpa.parking_events (
    id          bigserial PRIMARY KEY,
    event_type  text NOT NULL
                CHECK (event_type IN ('ALLOCATION','RELEASE','OVERFLOW',
                                      'ILLEGAL_PARKING','NO_PARKING_VIOLATION')),
    vehicle_id  text,
    driver_id   text,
    facility_id text,
    slot_id     bigint,
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_parking_events_type ON jnpa.parking_events (event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_parking_events_vehicle ON jnpa.parking_events (vehicle_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_parking_events_ts ON jnpa.parking_events (created_at DESC);

-- ==== Empty Container (migration 0006) ====
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;
CREATE TABLE IF NOT EXISTS jnpa.empty_container_inventory (
    container_id        text PRIMARY KEY,
    container_type      text,                    -- 20GP | 40GP | 40HC | REEFER | TANKER …
    location            text,                    -- depot / ECD / CFS id
    owner               text,                    -- shipping_line | fleet_owner | CFS | ECD
    availability_status text NOT NULL DEFAULT 'AVAILABLE'
                        CHECK (availability_status IN ('AVAILABLE','ALLOCATED','IN_TRANSIT','DELIVERED')),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ec_inventory_status ON jnpa.empty_container_inventory (availability_status, container_type);
CREATE INDEX IF NOT EXISTS idx_ec_inventory_location ON jnpa.empty_container_inventory (location);
CREATE TABLE IF NOT EXISTS jnpa.empty_container_allocations (
    id                bigserial PRIMARY KEY,
    container_id      text,
    truck_id          text,
    trailer_id        text,
    driver_id         text,
    shipping_line     text,
    cfs               text,
    ecd               text,
    allocation_reason text,
    allocated_at      timestamptz NOT NULL DEFAULT now(),
    status            text NOT NULL DEFAULT 'ALLOCATED'
                      CHECK (status IN ('ALLOCATED','PICKED_UP','IN_TRANSIT','DELIVERED','COMPLETED','CANCELLED'))
);
CREATE INDEX IF NOT EXISTS idx_ec_alloc_container ON jnpa.empty_container_allocations (container_id, allocated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ec_alloc_status    ON jnpa.empty_container_allocations (status, allocated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ec_alloc_ts        ON jnpa.empty_container_allocations (allocated_at DESC);
CREATE TABLE IF NOT EXISTS jnpa.container_movement_history (
    id            bigserial PRIMARY KEY,
    container_id  text,
    allocation_id bigint,
    movement_type text NOT NULL
                  CHECK (movement_type IN ('PICKUP','ALLOCATION','TRANSFER','DELIVERY','COMPLETION')),
    location      text,
    detail        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_container_move_container ON jnpa.container_movement_history (container_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_container_move_type ON jnpa.container_movement_history (movement_type, created_at DESC);

-- ==== geofence_events enforcement columns (migration 0007) ====
ALTER TABLE jnpa.geofence_events ADD COLUMN IF NOT EXISTS driver_id     text;
ALTER TABLE jnpa.geofence_events ADD COLUMN IF NOT EXISTS event_type    text;
ALTER TABLE jnpa.geofence_events ADD COLUMN IF NOT EXISTS dwell_seconds integer;
CREATE INDEX IF NOT EXISTS idx_geofence_events_type ON jnpa.geofence_events (event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_geofence_events_driver ON jnpa.geofence_events (driver_id, created_at DESC);

-- ==== Vehicle & Driver Intelligence history (migration 0008) ====
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;
CREATE TABLE IF NOT EXISTS jnpa.vehicle_verification_history (
    id                  bigserial PRIMARY KEY,
    vehicle_number      text,
    request_payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
    response_payload    jsonb NOT NULL DEFAULT '{}'::jsonb,
    verification_status text,                     -- VERIFIED | PROVISIONAL | NOT_FOUND | ERROR
    source              text,                     -- LIVE_PRIMARY | LIVE_FALLBACK | CACHED | SIM | PROVISIONAL
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_veh_verif_number ON jnpa.vehicle_verification_history (vehicle_number, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_veh_verif_ts     ON jnpa.vehicle_verification_history (created_at DESC);
CREATE TABLE IF NOT EXISTS jnpa.driver_license_lookup_history (
    id                bigserial PRIMARY KEY,
    dl_number         text,
    request_payload   jsonb NOT NULL DEFAULT '{}'::jsonb,
    response_payload  jsonb NOT NULL DEFAULT '{}'::jsonb,
    status            text,                       -- VALID | EXPIRED | NOT_FOUND | ERROR
    source            text,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dl_lookup_number ON jnpa.driver_license_lookup_history (dl_number, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dl_lookup_ts     ON jnpa.driver_license_lookup_history (created_at DESC);
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;
CREATE TABLE IF NOT EXISTS jnpa.otp_requests (
    id          bigserial PRIMARY KEY,
    mobile      text NOT NULL,
    device_id   text,
    code_hash   text NOT NULL,
    expires_at  timestamptz NOT NULL,
    verified    boolean NOT NULL DEFAULT false,
    attempts    integer NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_otp_mobile ON jnpa.otp_requests (mobile, created_at DESC);
CREATE TABLE IF NOT EXISTS jnpa.device_bindings (
    device_id   text PRIMARY KEY,
    mobile      text NOT NULL,
    driver_id   text,
    bound_at    timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),
    active      boolean NOT NULL DEFAULT true
);
CREATE INDEX IF NOT EXISTS idx_device_bindings_mobile ON jnpa.device_bindings (mobile);

-- ==== geofence_events mandatory event_type (migration 0010) ====
-- Backfill any legacy NULL/'' event_type, then guarantee it going forward via a
-- BEFORE-trigger derivation + NOT NULL. Keeps GET /api/geo/events free of blank
-- event types without modifying the audit-framework writer.
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;
UPDATE jnpa.geofence_events
SET event_type = CASE
    WHEN violation_type IS NOT NULL AND violation_type <> '' THEN violation_type
    WHEN exit_time IS NOT NULL                                THEN 'EXIT'
    WHEN COALESCE(dwell_seconds, 0) > 0                       THEN 'DWELL'
    WHEN entry_time IS NOT NULL                               THEN 'ENTER'
    ELSE 'ENTER'
END
WHERE event_type IS NULL OR event_type = '';
CREATE OR REPLACE FUNCTION jnpa.geofence_events_default_event_type()
RETURNS trigger AS $$
BEGIN
    IF NEW.event_type IS NULL OR NEW.event_type = '' THEN
        NEW.event_type := CASE
            WHEN NEW.violation_type IS NOT NULL AND NEW.violation_type <> '' THEN NEW.violation_type
            WHEN NEW.exit_time IS NOT NULL                                   THEN 'EXIT'
            WHEN COALESCE(NEW.dwell_seconds, 0) > 0                          THEN 'DWELL'
            WHEN NEW.entry_time IS NOT NULL                                  THEN 'ENTER'
            ELSE 'ENTER'
        END;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_geofence_events_event_type ON jnpa.geofence_events;
CREATE TRIGGER trg_geofence_events_event_type
    BEFORE INSERT OR UPDATE ON jnpa.geofence_events
    FOR EACH ROW EXECUTE FUNCTION jnpa.geofence_events_default_event_type();
ALTER TABLE jnpa.geofence_events ALTER COLUMN event_type SET NOT NULL;

-- ==== driver push registrations (migration 0011) ====
-- Durable home for the driver-notification transports: the WebPush subscription
-- (webpush jsonb) and the Firebase FCM device token (fcm_token). Keyed on the
-- same device_id as jnpa.device_bindings. Additive only. The gateway also
-- self-provisions this at runtime (gateway/routers/push.py::_ensure), but seeding
-- it here means the table exists on a fresh boot BEFORE the first read path runs
-- (resolve_device / token lookup), which would otherwise error on an empty DB.
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;
CREATE TABLE IF NOT EXISTS jnpa.push_subscriptions (
    device_id    text PRIMARY KEY,
    driver_id    text,
    vehicle_id   text,
    webpush      jsonb,
    fcm_token    text,
    platform     text NOT NULL DEFAULT 'web',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_push_subs_fcm
    ON jnpa.push_subscriptions (fcm_token)
    WHERE fcm_token IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_push_subs_driver
    ON jnpa.push_subscriptions (driver_id);

-- ==== Cargo (migration 0013) ====
-- Single shared cargo record for the Traffic Twin (POC-3) + Cargo Twin (POC-2).
-- POC-3 is the common backend: /api/cargo CRUD lives here; POC-2 consumes it and
-- keeps no backend/DB. `container_number` is the ISO-6346 follow-the-box PK.
-- Mirrors infra/postgres/migrations/0013_cargo.sql for fresh-boot bootstraps.
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;
CREATE OR REPLACE FUNCTION jnpa.set_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TABLE IF NOT EXISTS jnpa.cargo (
    container_number text PRIMARY KEY,
    vessel_name      text,
    customs_status   text NOT NULL DEFAULT 'PENDING'
                     CHECK (customs_status IN ('PENDING','CLEARED','HELD','UNDER_INSPECTION')),
    yard_block       text,
    is_released      boolean NOT NULL DEFAULT false,
    vehicle_number   text,
    gate             text,
    camera_id        text,
    eta              timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
-- Contract extensions (migration 0015): e-Seal, pre-document status, origin stream.
ALTER TABLE jnpa.cargo
    ADD COLUMN IF NOT EXISTS eseal_status text
        CHECK (eseal_status IN ('ACTIVE','ARMED','TAMPERED','REMOVED','NONE'));
ALTER TABLE jnpa.cargo ADD COLUMN IF NOT EXISTS eseal_number text;
ALTER TABLE jnpa.cargo
    ADD COLUMN IF NOT EXISTS pre_document_status text
        CHECK (pre_document_status IN ('NOT_STARTED','PENDING','IN_PROGRESS','COMPLETED'));
ALTER TABLE jnpa.cargo ADD COLUMN IF NOT EXISTS origin_stream text;
CREATE INDEX IF NOT EXISTS idx_cargo_customs_status ON jnpa.cargo (customs_status);
CREATE INDEX IF NOT EXISTS idx_cargo_is_released    ON jnpa.cargo (is_released);
CREATE INDEX IF NOT EXISTS idx_cargo_yard_block     ON jnpa.cargo (yard_block);
CREATE INDEX IF NOT EXISTS idx_cargo_vehicle        ON jnpa.cargo (vehicle_number) WHERE vehicle_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cargo_eta            ON jnpa.cargo (eta DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_cargo_origin_stream       ON jnpa.cargo (origin_stream);
CREATE INDEX IF NOT EXISTS idx_cargo_eseal_status        ON jnpa.cargo (eseal_status);
CREATE INDEX IF NOT EXISTS idx_cargo_pre_document_status ON jnpa.cargo (pre_document_status);
DROP TRIGGER IF EXISTS trg_cargo_updated_at ON jnpa.cargo;
CREATE TRIGGER trg_cargo_updated_at
    BEFORE UPDATE ON jnpa.cargo
    FOR EACH ROW EXECUTE FUNCTION jnpa.set_updated_at();
-- Append-only cargo lifecycle event log (notifications contract; migration 0015).
CREATE TABLE IF NOT EXISTS jnpa.cargo_events (
    id               bigserial PRIMARY KEY,
    event            text NOT NULL,
    container_number text NOT NULL,
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cargo_events_created   ON jnpa.cargo_events (id DESC);
CREATE INDEX IF NOT EXISTS idx_cargo_events_container ON jnpa.cargo_events (container_number);
CREATE INDEX IF NOT EXISTS idx_cargo_events_event     ON jnpa.cargo_events (event);
