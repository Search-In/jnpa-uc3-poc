-- ===========================================================================
-- Migration 0024 — UC-III Final Feature Completion (ADDITIVE, non-breaking).
--
-- Adds the persistence for the tender requirements that the implementation audit
-- flagged as Missing / Partial. Every statement is idempotent (IF NOT EXISTS) and
-- ADDITIVE — no existing table, column, index, view or seed row is modified or
-- dropped. Reuses the existing audit framework (jnpa.alerts / notifications /
-- digital_twin_events) and existing masters (jnpa.vehicle_master / drivers /
-- cameras / gate_events / cargo) rather than duplicating them.
--
-- Each router also applies the same DDL at runtime via gateway/uc3_ext.py
-- (ensure_uc3_schema), exactly like parking/persistence.py + kpi.ensure_kpi_gate_schema,
-- so a dev DB that never ran this file still gets the tables lazily.
--
-- Sections:
--   1  Accident lifecycle          (accidents, accident_events)
--   2  Transporter blacklist       (transporters, transporter_vehicles, transporter_blacklist)
--   3  Camera-AI counting          (camera_ai_counts)
--   4  Trailer identification      (trailer_reads)
--   5  Container identification    (container_reads)
--   6  Document OCR                (document_ocr)
--   7  NVR integration             (nvr_devices, nvr_camera_map)
--   8  ECY TRT KPI                 (trt_records)
--   9  Road-bottleneck analytics   (bottleneck_snapshots)
--  11  Reefer availability         (reefer_slots)
--  12/13 PDP + LDB adapters        (integration_lookups, ldb_movements)
--  14  RMS-TAS integration         (tas_appointments, tas_bookings)
--  15  TT double-trip workflow     (tt_trips)
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0024_uc3_completion.sql
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

-- ---------------------------------------------------------------------------
-- 1. ACCIDENT LIFECYCLE
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.accidents (
    id                  bigserial PRIMARY KEY,
    accident_ref        text UNIQUE,                         -- human ref ACC-YYYYMMDD-####
    occurred_at         timestamptz NOT NULL DEFAULT now(),
    accident_type       text NOT NULL DEFAULT 'ENROUTE'
                        CHECK (accident_type IN ('PREMISES','ENROUTE')),
    severity            text NOT NULL DEFAULT 'MINOR'
                        CHECK (severity IN ('MINOR','MODERATE','MAJOR','FATAL')),
    lat                 double precision,
    lon                 double precision,
    location            jsonb NOT NULL DEFAULT '{}'::jsonb,  -- {name, gate_id, segment_id, ...}
    vehicle_id          text,                                -- FK-soft to fleet_vehicles.vehicle_id
    plate               text,
    driver_id           text,                                -- FK-soft to drivers.driver_id
    description         text,
    status              text NOT NULL DEFAULT 'REPORTED'
                        CHECK (status IN ('REPORTED','INVESTIGATING','RESOLVED','CLOSED')),
    investigation_status text NOT NULL DEFAULT 'PENDING'
                        CHECK (investigation_status IN ('PENDING','IN_PROGRESS','COMPLETED')),
    resolution          text,
    reported_by         text,
    source              text NOT NULL DEFAULT 'MANUAL',      -- MANUAL | CAMERA_AI | SENSOR
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_accidents_status   ON jnpa.accidents (status, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_accidents_vehicle  ON jnpa.accidents (vehicle_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_accidents_occurred ON jnpa.accidents (occurred_at DESC);

-- append-only timeline (report -> investigate -> resolve -> close)
CREATE TABLE IF NOT EXISTS jnpa.accident_events (
    id           bigserial PRIMARY KEY,
    accident_id  bigint NOT NULL REFERENCES jnpa.accidents(id) ON DELETE CASCADE,
    action       text NOT NULL,                              -- REPORTED | STATUS_CHANGE | INVESTIGATION | NOTE | RESOLVED | CLOSED
    old_status   text,
    new_status   text,
    note         text,
    actor        text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_accident_events_aid ON jnpa.accident_events (accident_id, created_at);

-- ---------------------------------------------------------------------------
-- 2. TRANSPORTER BLACKLIST
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.transporters (
    id          bigserial PRIMARY KEY,
    code        text UNIQUE,                                 -- transporter code / license
    name        text NOT NULL,
    gstin       text,
    contact     jsonb NOT NULL DEFAULT '{}'::jsonb,          -- {phone, email, address}
    status      text NOT NULL DEFAULT 'ACTIVE'
                CHECK (status IN ('ACTIVE','SUSPENDED','BLACKLISTED')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_transporters_status ON jnpa.transporters (status);
CREATE INDEX IF NOT EXISTS idx_transporters_name   ON jnpa.transporters (lower(name));

-- vehicle <-> transporter mapping (drives vehicle & driver validation)
CREATE TABLE IF NOT EXISTS jnpa.transporter_vehicles (
    id             bigserial PRIMARY KEY,
    transporter_id bigint NOT NULL REFERENCES jnpa.transporters(id) ON DELETE CASCADE,
    vehicle_no     text NOT NULL,
    vehicle_no_norm text NOT NULL,                           -- upper, no spaces
    driver_id      text,                                     -- optional driver association
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (transporter_id, vehicle_no_norm)
);
CREATE INDEX IF NOT EXISTS idx_transporter_veh_norm   ON jnpa.transporter_vehicles (vehicle_no_norm);
CREATE INDEX IF NOT EXISTS idx_transporter_veh_driver ON jnpa.transporter_vehicles (driver_id);

CREATE TABLE IF NOT EXISTS jnpa.transporter_blacklist (
    id             bigserial PRIMARY KEY,
    transporter_id bigint NOT NULL REFERENCES jnpa.transporters(id) ON DELETE CASCADE,
    reason         text NOT NULL,
    severity       text NOT NULL DEFAULT 'HIGH'
                   CHECK (severity IN ('LOW','MEDIUM','HIGH','CRITICAL')),
    status         text NOT NULL DEFAULT 'ACTIVE'
                   CHECK (status IN ('ACTIVE','LIFTED')),
    blacklisted_by text,
    blacklisted_at timestamptz NOT NULL DEFAULT now(),
    lifted_by      text,
    lifted_at      timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_blacklist_transporter ON jnpa.transporter_blacklist (transporter_id, status);
CREATE INDEX IF NOT EXISTS idx_blacklist_status      ON jnpa.transporter_blacklist (status, blacklisted_at DESC);

-- ---------------------------------------------------------------------------
-- 3. CAMERA-AI COUNTING (vehicle count / queue count / congestion / confidence)
--    Object-detection *events* already land in jnpa.digital_twin_events via
--    /api/ai/event; this table is the periodic counting/aggregation snapshot.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.camera_ai_counts (
    id              bigserial PRIMARY KEY,
    ts              timestamptz NOT NULL DEFAULT now(),
    camera_id       text,
    gate_id         text,
    vehicle_count   integer NOT NULL DEFAULT 0,
    queue_count     integer NOT NULL DEFAULT 0,
    class_counts    jsonb NOT NULL DEFAULT '{}'::jsonb,      -- {car, lcv, hgv, trailer, ...}
    congestion_level text NOT NULL DEFAULT 'LOW'
                    CHECK (congestion_level IN ('LOW','MEDIUM','HIGH')),
    confidence      double precision NOT NULL DEFAULT 0.0,
    source          text NOT NULL DEFAULT 'CAMERA_AI',       -- CAMERA_AI | SIM
    detail          jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_cam_counts_cam_ts ON jnpa.camera_ai_counts (camera_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_cam_counts_gate_ts ON jnpa.camera_ai_counts (gate_id, ts DESC);

-- ---------------------------------------------------------------------------
-- 4. TRAILER IDENTIFICATION
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.trailer_reads (
    id             bigserial PRIMARY KEY,
    ts             timestamptz NOT NULL DEFAULT now(),
    camera_id      text,
    gate_id        text,
    trailer_number text,
    plate          text,                                     -- towing tractor plate (association)
    vehicle_id     text,
    confidence     double precision NOT NULL DEFAULT 0.0,
    image_url      text,
    source         text NOT NULL DEFAULT 'CAMERA_AI',
    detail         jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_trailer_reads_ts    ON jnpa.trailer_reads (ts DESC);
CREATE INDEX IF NOT EXISTS idx_trailer_reads_num   ON jnpa.trailer_reads (trailer_number, ts DESC);
CREATE INDEX IF NOT EXISTS idx_trailer_reads_plate ON jnpa.trailer_reads (plate, ts DESC);

-- ---------------------------------------------------------------------------
-- 5. CONTAINER IDENTIFICATION (ISO-6346 OCR)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.container_reads (
    id               bigserial PRIMARY KEY,
    ts               timestamptz NOT NULL DEFAULT now(),
    camera_id        text,
    gate_id          text,
    container_number text,
    iso_type         text,                                   -- ISO-6346 size/type group
    check_digit_ok   boolean,
    valid            boolean NOT NULL DEFAULT false,
    plate            text,
    vehicle_id       text,
    confidence       double precision NOT NULL DEFAULT 0.0,
    image_url        text,
    source           text NOT NULL DEFAULT 'OCR',
    detail           jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_container_reads_ts   ON jnpa.container_reads (ts DESC);
CREATE INDEX IF NOT EXISTS idx_container_reads_num  ON jnpa.container_reads (container_number, ts DESC);

-- ---------------------------------------------------------------------------
-- 6. DOCUMENT OCR
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.document_ocr (
    id           bigserial PRIMARY KEY,
    ts           timestamptz NOT NULL DEFAULT now(),
    doc_type     text NOT NULL DEFAULT 'UNKNOWN',            -- LR | INVOICE | EWAYBILL | PERMIT | RC | DL | FORM13 | UNKNOWN
    source_ref   text,                                       -- vehicle/plate/container/manual ref
    storage_url  text,                                       -- MinIO/object-store URL of the uploaded doc
    raw_text     text,
    fields       jsonb NOT NULL DEFAULT '{}'::jsonb,         -- extracted key/values
    confidence   double precision NOT NULL DEFAULT 0.0,
    status       text NOT NULL DEFAULT 'EXTRACTED'
                 CHECK (status IN ('UPLOADED','EXTRACTED','VERIFIED','FAILED')),
    source       text NOT NULL DEFAULT 'OCR',
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_document_ocr_ts   ON jnpa.document_ocr (ts DESC);
CREATE INDEX IF NOT EXISTS idx_document_ocr_type ON jnpa.document_ocr (doc_type, ts DESC);

-- ---------------------------------------------------------------------------
-- 7. NVR INTEGRATION (device registry + channel->camera mapping + stream meta)
--    Camera *registry* stays jnpa.cameras; this adds the recorder + streams.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.nvr_devices (
    id          text PRIMARY KEY,                            -- NVR-01
    name        text NOT NULL,
    vendor      text,                                        -- Hikvision | Dahua | ...
    host        text,
    port        integer NOT NULL DEFAULT 554,
    protocol    text NOT NULL DEFAULT 'RTSP'
                CHECK (protocol IN ('RTSP','ONVIF','HTTP')),
    channels    integer NOT NULL DEFAULT 0,
    location    jsonb NOT NULL DEFAULT '{}'::jsonb,
    status      text NOT NULL DEFAULT 'UNKNOWN'
                CHECK (status IN ('ONLINE','OFFLINE','DEGRADED','UNKNOWN')),
    source      text NOT NULL DEFAULT 'CONFIG',              -- CONFIG | LIVE | MOCK
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jnpa.nvr_camera_map (
    id          bigserial PRIMARY KEY,
    nvr_id      text NOT NULL REFERENCES jnpa.nvr_devices(id) ON DELETE CASCADE,
    channel     integer NOT NULL,
    camera_id   text,                                        -- FK-soft to jnpa.cameras.id
    stream_url  text,                                        -- rtsp://host:port/chN (metadata only)
    codec       text NOT NULL DEFAULT 'H264',
    resolution  text NOT NULL DEFAULT '1920x1080',
    fps         integer NOT NULL DEFAULT 25,
    status      text NOT NULL DEFAULT 'UNKNOWN',
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (nvr_id, channel)
);
CREATE INDEX IF NOT EXISTS idx_nvr_map_camera ON jnpa.nvr_camera_map (camera_id);

-- ---------------------------------------------------------------------------
-- 8. ECY TRT (Gate-In -> Parking -> Loading -> Gate-Out -> TRT)
--    Persisted per-vehicle turnaround record + its phase timestamps.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.trt_records (
    id           bigserial PRIMARY KEY,
    vehicle_id   text,
    plate        text,
    trip_id      text,
    gate_in_at   timestamptz,
    parking_at   timestamptz,
    loading_at   timestamptz,
    gate_out_at  timestamptz,
    -- phase minutes (persisted, computed on close)
    gate_to_park_min  double precision,
    park_to_load_min  double precision,
    load_to_out_min   double precision,
    trt_min      double precision,                           -- gate_out - gate_in (minutes)
    status       text NOT NULL DEFAULT 'OPEN'
                 CHECK (status IN ('OPEN','GATE_IN','PARKED','LOADING','COMPLETED')),
    source       text NOT NULL DEFAULT 'COMPUTED',
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trt_vehicle ON jnpa.trt_records (vehicle_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trt_status  ON jnpa.trt_records (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trt_out     ON jnpa.trt_records (gate_out_at DESC);

-- ---------------------------------------------------------------------------
-- 9. ROAD-BOTTLENECK ANALYTICS (persisted ranking snapshots over corridor)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.bottleneck_snapshots (
    id           bigserial PRIMARY KEY,
    ts           timestamptz NOT NULL DEFAULT now(),
    rank         integer NOT NULL,
    segment_id   text NOT NULL,
    name         text,
    jam_factor   double precision NOT NULL DEFAULT 0.0,
    speed_kmh    double precision,
    free_flow_kmh double precision,
    avg_delay_min double precision,
    lat          double precision,
    lon          double precision,
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_bottleneck_ts   ON jnpa.bottleneck_snapshots (ts DESC, rank);
CREATE INDEX IF NOT EXISTS idx_bottleneck_seg  ON jnpa.bottleneck_snapshots (segment_id, ts DESC);

-- ---------------------------------------------------------------------------
-- 11. REEFER AVAILABILITY (powered slots at the reefer plaza / CPP)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.reefer_slots (
    id               bigserial PRIMARY KEY,
    facility_id      text NOT NULL DEFAULT 'PK-CPP',         -- reefer plaza id
    slot_code        text NOT NULL,                          -- REEFER-A01
    powered          boolean NOT NULL DEFAULT true,
    status           text NOT NULL DEFAULT 'AVAILABLE'
                     CHECK (status IN ('AVAILABLE','OCCUPIED','RESERVED','FAULT')),
    container_number text,
    set_temperature  double precision,
    current_temperature double precision,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (facility_id, slot_code)
);
CREATE INDEX IF NOT EXISTS idx_reefer_slots_fac ON jnpa.reefer_slots (facility_id, status);

-- ---------------------------------------------------------------------------
-- 12/13. INTEGRATION ADAPTER AUDIT (PDP/LDB/RMS-TAS/NVR/WEATHER) + LDB movements
--    Every adapter call is logged with source LIVE|MOCK so the external
--    dependency posture is explicit (never a silent hardcode).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.integration_lookups (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    system      text NOT NULL,                               -- PDP | LDB | RMS_TAS | NVR | WEATHER
    op          text NOT NULL,                               -- vehicle | event | traffic | container | movement | slots ...
    ref         text,
    request     jsonb NOT NULL DEFAULT '{}'::jsonb,
    response    jsonb NOT NULL DEFAULT '{}'::jsonb,
    source      text NOT NULL DEFAULT 'MOCK'
                CHECK (source IN ('LIVE','MOCK','ERROR')),
    latency_ms  integer,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_integration_sys_ts ON jnpa.integration_lookups (system, ts DESC);

CREATE TABLE IF NOT EXISTS jnpa.ldb_movements (
    id               bigserial PRIMARY KEY,
    ts               timestamptz NOT NULL DEFAULT now(),
    container_number text NOT NULL,
    event            text NOT NULL,                          -- GATE_IN | RAIL_OUT | VESSEL_LOAD | ...
    location         text,
    terminal         text,
    mode             text,                                   -- ROAD | RAIL | VESSEL
    source           text NOT NULL DEFAULT 'MOCK',
    detail           jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_ldb_container_ts ON jnpa.ldb_movements (container_number, ts DESC);

-- ---------------------------------------------------------------------------
-- 14. RMS-TAS (Terminal Appointment System) — persisted slots + bookings.
--     NOTE: the legacy in-memory /api/tas/* (gateway/tas_mock.py) is left
--     untouched; this is the NEW persisted /api/rms-tas/* surface.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.tas_appointments (
    id            bigserial PRIMARY KEY,
    slot_code     text UNIQUE NOT NULL,                      -- G-NSICT-2026-07-16-0900
    gate_id       text NOT NULL,
    window_start  timestamptz NOT NULL,
    window_end    timestamptz NOT NULL,
    capacity      integer NOT NULL DEFAULT 10,
    booked        integer NOT NULL DEFAULT 0,
    status        text NOT NULL DEFAULT 'OPEN'
                  CHECK (status IN ('OPEN','FULL','CLOSED')),
    source        text NOT NULL DEFAULT 'LOCAL',             -- LOCAL | LIVE | MOCK
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tas_gate_window ON jnpa.tas_appointments (gate_id, window_start);
CREATE INDEX IF NOT EXISTS idx_tas_status      ON jnpa.tas_appointments (status, window_start);

CREATE TABLE IF NOT EXISTS jnpa.tas_bookings (
    id             bigserial PRIMARY KEY,
    appointment_id bigint NOT NULL REFERENCES jnpa.tas_appointments(id) ON DELETE CASCADE,
    slot_code      text NOT NULL,
    vehicle_id     text,
    driver_id      text,
    status         text NOT NULL DEFAULT 'BOOKED'
                   CHECK (status IN ('BOOKED','CANCELLED','COMPLETED','NO_SHOW')),
    booked_at      timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tas_bookings_appt ON jnpa.tas_bookings (appointment_id);
CREATE INDEX IF NOT EXISTS idx_tas_bookings_veh  ON jnpa.tas_bookings (vehicle_id, booked_at DESC);

-- ---------------------------------------------------------------------------
-- 15. TT DOUBLE-TRIP WORKFLOW (Trip-1 -> Return -> Trip-2 -> statistics)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.tt_trips (
    id           bigserial PRIMARY KEY,
    cycle_id     text NOT NULL,                              -- groups the two legs into one double-trip
    vehicle_id   text NOT NULL,
    driver_id    text,
    trip_seq     integer NOT NULL DEFAULT 1,                 -- 1 = first trip, 2 = second trip
    direction    text NOT NULL DEFAULT 'INBOUND'
                 CHECK (direction IN ('INBOUND','OUTBOUND','RETURN')),
    origin       text,
    destination  text,
    started_at   timestamptz NOT NULL DEFAULT now(),
    ended_at     timestamptz,
    laden        boolean,                                    -- true = carrying container
    status       text NOT NULL DEFAULT 'IN_PROGRESS'
                 CHECK (status IN ('IN_PROGRESS','COMPLETED','ABORTED')),
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tt_trips_cycle   ON jnpa.tt_trips (cycle_id, trip_seq);
CREATE INDEX IF NOT EXISTS idx_tt_trips_vehicle ON jnpa.tt_trips (vehicle_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_tt_trips_status  ON jnpa.tt_trips (status, started_at DESC);
