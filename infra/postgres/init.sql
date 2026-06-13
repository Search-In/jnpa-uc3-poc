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
    id   text PRIMARY KEY,
    name text,
    lat  double precision,
    lon  double precision
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
