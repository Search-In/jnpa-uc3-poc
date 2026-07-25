"""UC-III Final-Completion schema bootstrap (idempotent, additive).

Applies the same DDL as infra/postgres/migrations/0024_uc3_completion.sql at
gateway boot so a dev/mock database that never ran the migration still gets the
new tables lazily — exactly the pattern parking/persistence.py and
gateway/routers/kpi.ensure_kpi_gate_schema already use.

Every statement is CREATE ... IF NOT EXISTS: running it against a DB that already
has the tables (because the migration ran) is a no-op. NEVER drops/alters.

Called once from gateway/main.py::_lifespan (best-effort; a DB blip only logs).
"""
from __future__ import annotations

import os

from typing import Optional

from .logging import get_logger

log = get_logger("gateway.uc3_ext")

# One idempotent statement per list item (SQLAlchemy text() runs a single
# statement per execute()). Mirrors migration 0024 exactly.
_DDL: list[str] = [
    "CREATE SCHEMA IF NOT EXISTS core",
    # 1. accidents
    """CREATE TABLE IF NOT EXISTS core.accident (
        id bigserial PRIMARY KEY,
        accident_ref text UNIQUE,
        occurred_at timestamptz NOT NULL DEFAULT now(),
        accident_type text NOT NULL DEFAULT 'ENROUTE'
            CHECK (accident_type IN ('PREMISES','ENROUTE')),
        severity text NOT NULL DEFAULT 'MINOR'
            CHECK (severity IN ('MINOR','MODERATE','MAJOR','FATAL')),
        lat double precision, lon double precision,
        location jsonb NOT NULL DEFAULT '{}'::jsonb,
        vehicle_id text, plate text, driver_id text,
        description text,
        status text NOT NULL DEFAULT 'REPORTED'
            CHECK (status IN ('REPORTED','INVESTIGATING','RESOLVED','CLOSED')),
        investigation_status text NOT NULL DEFAULT 'PENDING'
            CHECK (investigation_status IN ('PENDING','IN_PROGRESS','COMPLETED')),
        resolution text, reported_by text,
        source text NOT NULL DEFAULT 'MANUAL',
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_accidents_status ON core.accident (status, occurred_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_accidents_vehicle ON core.accident (vehicle_id, occurred_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_accidents_occurred ON core.accident (occurred_at DESC)",
    """CREATE TABLE IF NOT EXISTS core.accident_event (
        id bigserial PRIMARY KEY,
        accident_id bigint NOT NULL REFERENCES core.accident(id) ON DELETE CASCADE,
        action text NOT NULL, old_status text, new_status text,
        note text, actor text,
        created_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_accident_events_aid ON core.accident_event (accident_id, created_at)",
    # 2. transporters + blacklist
    """CREATE TABLE IF NOT EXISTS core.transporter (
        id bigserial PRIMARY KEY, code text UNIQUE, name text NOT NULL, gstin text,
        contact jsonb NOT NULL DEFAULT '{}'::jsonb,
        status text NOT NULL DEFAULT 'ACTIVE'
            CHECK (status IN ('ACTIVE','SUSPENDED','BLACKLISTED')),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_transporters_status ON core.transporter (status)",
    "CREATE INDEX IF NOT EXISTS idx_transporters_name ON core.transporter (lower(name))",
    # 2b. Transport Master fields (migration 0025) — additive columns from the
    # official TransporterDetails.xlsx. ADD COLUMN IF NOT EXISTS is idempotent.
    "ALTER TABLE core.transporter ADD COLUMN IF NOT EXISTS source_company_id bigint",
    "ALTER TABLE core.transporter ADD COLUMN IF NOT EXISTS source_user_id bigint",
    "ALTER TABLE core.transporter ADD COLUMN IF NOT EXISTS contact_person text",
    "ALTER TABLE core.transporter ADD COLUMN IF NOT EXISTS designation text",
    "ALTER TABLE core.transporter ADD COLUMN IF NOT EXISTS email text",
    "ALTER TABLE core.transporter ADD COLUMN IF NOT EXISTS mobile text",
    "ALTER TABLE core.transporter ADD COLUMN IF NOT EXISTS address text",
    "ALTER TABLE core.transporter ADD COLUMN IF NOT EXISTS doc_type text",
    "ALTER TABLE core.transporter ADD COLUMN IF NOT EXISTS doc_file text",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_transporters_source_company_id ON core.transporter (source_company_id)",
    "CREATE INDEX IF NOT EXISTS idx_transporters_mobile ON core.transporter (mobile)",
    "CREATE INDEX IF NOT EXISTS idx_transporters_email ON core.transporter (lower(email))",
    """CREATE TABLE IF NOT EXISTS core.transporter_vehicle (
        id bigserial PRIMARY KEY,
        transporter_id bigint NOT NULL REFERENCES core.transporter(id) ON DELETE CASCADE,
        vehicle_no text NOT NULL, vehicle_no_norm text NOT NULL, driver_id text,
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (transporter_id, vehicle_no_norm))""",
    "CREATE INDEX IF NOT EXISTS idx_transporter_veh_norm ON core.transporter_vehicle (vehicle_no_norm)",
    "CREATE INDEX IF NOT EXISTS idx_transporter_veh_driver ON core.transporter_vehicle (driver_id)",
    """CREATE TABLE IF NOT EXISTS core.transporter_blacklist (
        id bigserial PRIMARY KEY,
        transporter_id bigint NOT NULL REFERENCES core.transporter(id) ON DELETE CASCADE,
        reason text NOT NULL,
        severity text NOT NULL DEFAULT 'HIGH'
            CHECK (severity IN ('LOW','MEDIUM','HIGH','CRITICAL')),
        status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','LIFTED')),
        blacklisted_by text, blacklisted_at timestamptz NOT NULL DEFAULT now(),
        lifted_by text, lifted_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_blacklist_transporter ON core.transporter_blacklist (transporter_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_blacklist_status ON core.transporter_blacklist (status, blacklisted_at DESC)",
    # 3. camera_ai_counts
    """CREATE TABLE IF NOT EXISTS core.camera_ai_count (
        id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
        camera_id text, gate_id text,
        vehicle_count integer NOT NULL DEFAULT 0, queue_count integer NOT NULL DEFAULT 0,
        class_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
        congestion_level text NOT NULL DEFAULT 'LOW'
            CHECK (congestion_level IN ('LOW','MEDIUM','HIGH')),
        confidence double precision NOT NULL DEFAULT 0.0,
        source text NOT NULL DEFAULT 'CAMERA_AI',
        detail jsonb NOT NULL DEFAULT '{}'::jsonb)""",
    "CREATE INDEX IF NOT EXISTS idx_cam_counts_cam_ts ON core.camera_ai_count (camera_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_cam_counts_gate_ts ON core.camera_ai_count (gate_id, ts DESC)",
    # 4. trailer_reads
    """CREATE TABLE IF NOT EXISTS core.trailer_read (
        id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
        camera_id text, gate_id text, trailer_number text, plate text, vehicle_id text,
        confidence double precision NOT NULL DEFAULT 0.0, image_url text,
        source text NOT NULL DEFAULT 'CAMERA_AI',
        detail jsonb NOT NULL DEFAULT '{}'::jsonb)""",
    "CREATE INDEX IF NOT EXISTS idx_trailer_reads_ts ON core.trailer_read (ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_trailer_reads_num ON core.trailer_read (trailer_number, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_trailer_reads_plate ON core.trailer_read (plate, ts DESC)",
    # 5. container_reads
    """CREATE TABLE IF NOT EXISTS core.container_read (
        id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
        camera_id text, gate_id text, container_number text, iso_type text,
        check_digit_ok boolean, valid boolean NOT NULL DEFAULT false,
        plate text, vehicle_id text,
        confidence double precision NOT NULL DEFAULT 0.0, image_url text,
        source text NOT NULL DEFAULT 'OCR',
        detail jsonb NOT NULL DEFAULT '{}'::jsonb)""",
    "CREATE INDEX IF NOT EXISTS idx_container_reads_ts ON core.container_read (ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_container_reads_num ON core.container_read (container_number, ts DESC)",
    # 6. document_ocr
    """CREATE TABLE IF NOT EXISTS core.document_ocr (
        id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
        doc_type text NOT NULL DEFAULT 'UNKNOWN', source_ref text, storage_url text,
        raw_text text, fields jsonb NOT NULL DEFAULT '{}'::jsonb,
        confidence double precision NOT NULL DEFAULT 0.0,
        status text NOT NULL DEFAULT 'EXTRACTED'
            CHECK (status IN ('UPLOADED','EXTRACTED','VERIFIED','FAILED')),
        source text NOT NULL DEFAULT 'OCR',
        created_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_document_ocr_ts ON core.document_ocr (ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_document_ocr_type ON core.document_ocr (doc_type, ts DESC)",
    # 7. nvr
    """CREATE TABLE IF NOT EXISTS core.nvr_device (
        id text PRIMARY KEY, name text NOT NULL, vendor text, host text,
        port integer NOT NULL DEFAULT 554,
        protocol text NOT NULL DEFAULT 'RTSP' CHECK (protocol IN ('RTSP','ONVIF','HTTP')),
        channels integer NOT NULL DEFAULT 0, location jsonb NOT NULL DEFAULT '{}'::jsonb,
        status text NOT NULL DEFAULT 'UNKNOWN'
            CHECK (status IN ('ONLINE','OFFLINE','DEGRADED','UNKNOWN')),
        source text NOT NULL DEFAULT 'CONFIG',
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS core.nvr_camera_map (
        id bigserial PRIMARY KEY,
        nvr_id text NOT NULL REFERENCES core.nvr_device(id) ON DELETE CASCADE,
        channel integer NOT NULL, camera_id text, stream_url text,
        codec text NOT NULL DEFAULT 'H264', resolution text NOT NULL DEFAULT '1920x1080',
        fps integer NOT NULL DEFAULT 25, status text NOT NULL DEFAULT 'UNKNOWN',
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (nvr_id, channel))""",
    "CREATE INDEX IF NOT EXISTS idx_nvr_map_camera ON core.nvr_camera_map (camera_id)",
    # 8. trt_records
    """CREATE TABLE IF NOT EXISTS core.trt_record (
        id bigserial PRIMARY KEY, vehicle_id text, plate text, trip_id text,
        gate_in_at timestamptz, parking_at timestamptz, loading_at timestamptz,
        gate_out_at timestamptz,
        gate_to_park_min double precision, park_to_load_min double precision,
        load_to_out_min double precision, trt_min double precision,
        status text NOT NULL DEFAULT 'OPEN'
            CHECK (status IN ('OPEN','GATE_IN','PARKED','LOADING','COMPLETED')),
        source text NOT NULL DEFAULT 'COMPUTED',
        detail jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_trt_vehicle ON core.trt_record (vehicle_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_trt_status ON core.trt_record (status, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_trt_out ON core.trt_record (gate_out_at DESC)",
    # 9. bottleneck_snapshots
    """CREATE TABLE IF NOT EXISTS core.bottleneck_snapshot (
        id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
        rank integer NOT NULL, segment_id text NOT NULL, name text,
        jam_factor double precision NOT NULL DEFAULT 0.0, speed_kmh double precision,
        free_flow_kmh double precision, avg_delay_min double precision,
        lat double precision, lon double precision,
        detail jsonb NOT NULL DEFAULT '{}'::jsonb)""",
    "CREATE INDEX IF NOT EXISTS idx_bottleneck_ts ON core.bottleneck_snapshot (ts DESC, rank)",
    "CREATE INDEX IF NOT EXISTS idx_bottleneck_seg ON core.bottleneck_snapshot (segment_id, ts DESC)",
    # 11. reefer_slots
    """CREATE TABLE IF NOT EXISTS core.reefer_slot (
        id bigserial PRIMARY KEY, facility_id text NOT NULL DEFAULT 'PK-CPP',
        slot_code text NOT NULL, powered boolean NOT NULL DEFAULT true,
        status text NOT NULL DEFAULT 'AVAILABLE'
            CHECK (status IN ('AVAILABLE','OCCUPIED','RESERVED','FAULT')),
        container_number text, set_temperature double precision,
        current_temperature double precision,
        updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (facility_id, slot_code))""",
    "CREATE INDEX IF NOT EXISTS idx_reefer_slots_fac ON core.reefer_slot (facility_id, status)",
    # 12/13. integration audit + ldb movements
    """CREATE TABLE IF NOT EXISTS core.integration_lookup (
        id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
        system text NOT NULL, op text NOT NULL, ref text,
        request jsonb NOT NULL DEFAULT '{}'::jsonb,
        response jsonb NOT NULL DEFAULT '{}'::jsonb,
        source text NOT NULL DEFAULT 'MOCK' CHECK (source IN ('LIVE','MOCK','ERROR')),
        latency_ms integer, created_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_integration_sys_ts ON core.integration_lookup (system, ts DESC)",
    """CREATE TABLE IF NOT EXISTS core.ldb_movement (
        id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
        container_number text NOT NULL, event text NOT NULL, location text,
        terminal text, mode text, source text NOT NULL DEFAULT 'MOCK',
        detail jsonb NOT NULL DEFAULT '{}'::jsonb)""",
    "CREATE INDEX IF NOT EXISTS idx_ldb_container_ts ON core.ldb_movement (container_number, ts DESC)",
    # 14. rms-tas
    """CREATE TABLE IF NOT EXISTS core.tas_appointment (
        id bigserial PRIMARY KEY, slot_code text UNIQUE NOT NULL, gate_id text NOT NULL,
        window_start timestamptz NOT NULL, window_end timestamptz NOT NULL,
        capacity integer NOT NULL DEFAULT 10, booked integer NOT NULL DEFAULT 0,
        status text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','FULL','CLOSED')),
        source text NOT NULL DEFAULT 'LOCAL',
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_tas_gate_window ON core.tas_appointment (gate_id, window_start)",
    "CREATE INDEX IF NOT EXISTS idx_tas_status ON core.tas_appointment (status, window_start)",
    """CREATE TABLE IF NOT EXISTS core.tas_booking (
        id bigserial PRIMARY KEY,
        appointment_id bigint NOT NULL REFERENCES core.tas_appointment(id) ON DELETE CASCADE,
        slot_code text NOT NULL, vehicle_id text, driver_id text,
        status text NOT NULL DEFAULT 'BOOKED'
            CHECK (status IN ('BOOKED','CANCELLED','COMPLETED','NO_SHOW')),
        booked_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_tas_bookings_appt ON core.tas_booking (appointment_id)",
    "CREATE INDEX IF NOT EXISTS idx_tas_bookings_veh ON core.tas_booking (vehicle_id, booked_at DESC)",
    # 15. tt_trips
    """CREATE TABLE IF NOT EXISTS core.tt_trip (
        id bigserial PRIMARY KEY, cycle_id text NOT NULL, vehicle_id text NOT NULL,
        driver_id text, trip_seq integer NOT NULL DEFAULT 1,
        direction text NOT NULL DEFAULT 'INBOUND'
            CHECK (direction IN ('INBOUND','OUTBOUND','RETURN')),
        origin text, destination text,
        started_at timestamptz NOT NULL DEFAULT now(), ended_at timestamptz,
        laden boolean,
        status text NOT NULL DEFAULT 'IN_PROGRESS'
            CHECK (status IN ('IN_PROGRESS','COMPLETED','ABORTED')),
        detail jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_tt_trips_cycle ON core.tt_trip (cycle_id, trip_seq)",
    "CREATE INDEX IF NOT EXISTS idx_tt_trips_vehicle ON core.tt_trip (vehicle_id, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_tt_trips_status ON core.tt_trip (status, started_at DESC)",
]


async def ensure_uc3_schema(dsn: Optional[str]) -> None:
    """Idempotently create every UC-III completion table/index. Best-effort."""
    if os.getenv("JNPA_RUNTIME_DDL", "0") != "1":
        # schema-v3: DDL is owned by infra/postgres/v3 migrations, never runtime.
        return
    if not dsn:
        return
    from jnpa_shared.db import execute

    applied = 0
    for stmt in _DDL:
        try:
            await execute(stmt, {}, dsn=dsn)
            applied += 1
        except Exception as exc:  # noqa: BLE001 - one bad stmt never aborts boot
            log.warning("uc3_ddl_failed", error=str(exc), stmt=stmt[:60])
    log.info("uc3_ext_schema_ready", statements=applied, total=len(_DDL))
