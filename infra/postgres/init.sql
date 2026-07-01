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

-- ===========================================================================
-- Seed data
-- ===========================================================================

-- 4 gates with realistic JNPA terminal coordinates.
INSERT INTO jnpa.gates (id, name, lat, lon) VALUES
    ('G-NSICT', 'Nhava Sheva International Container Terminal', 18.9489, 72.9492),
    ('G-JNPCT', 'Jawaharlal Nehru Port Container Terminal',     18.9512, 72.9505),
    ('G-NSIGT', 'Nhava Sheva India Gateway Terminal',           18.9457, 72.9531),
    ('G-BMCT',  'Bharat Mumbai Container Terminals',            18.9420, 72.9560);

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
