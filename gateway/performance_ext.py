"""Performance & Daily Reports schema bootstrap (idempotent, additive).

Applies the same DDL as infra/postgres/migrations/0028_performance_reports.sql at
gateway boot so a dev/mock database that never ran the migration still gets the
new tables + terminal seed lazily — exactly the pattern
gateway/cfs_ecy_ext.ensure_cfs_ecy_schema and gateway/uc3_ext.ensure_uc3_schema
already use.

Every statement is CREATE ... IF NOT EXISTS / INSERT ... ON CONFLICT DO NOTHING:
running it against a DB that already has the objects is a no-op. NEVER drops or
alters existing objects; touches no other module (auth / JWT / RBAC / cargo /
vehicle / driver / transporter / ldb_movements are all untouched).

Called once from gateway/main.py::_lifespan (best-effort; a DB blip only logs).
Also reused by the scripts/import_performance_*.py importers so they are
self-contained (Module 12).
"""
from __future__ import annotations

import os

from typing import Optional

from .logging import get_logger

log = get_logger("gateway.performance_ext")

# One idempotent statement per list item (SQLAlchemy text() runs a single
# statement per execute()). Mirrors migration 0028 exactly.
_DDL: list[str] = [
    "CREATE SCHEMA IF NOT EXISTS core",
    """CREATE TABLE IF NOT EXISTS core.perf_terminal (
        id            bigserial PRIMARY KEY,
        code          text NOT NULL,
        full_name     text,
        operator      text,
        terminal_type text NOT NULL DEFAULT 'CONTAINER'
                           CHECK (terminal_type IN ('CONTAINER','MULTIPURPOSE','LIQUID','TOTAL')),
        is_container  boolean NOT NULL DEFAULT true,
        aliases       text[]  NOT NULL DEFAULT '{}',
        sort_order    int     NOT NULL DEFAULT 100,
        created_at    timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_perf_terminals_code UNIQUE (code))""",
    """INSERT INTO core.perf_terminal (code, full_name, operator, terminal_type, is_container, aliases, sort_order) VALUES
        ('NSFT',   'Nhava Sheva Freeport Terminal',              'NSFT',          'CONTAINER',    true,  ARRAY['NSFT']::text[],                       10),
        ('NSICT',  'Nhava Sheva International Container Terminal','DP World',      'CONTAINER',    true,  ARRAY['NSICT']::text[],                      20),
        ('NSIGT',  'Nhava Sheva India Gateway Terminal',         'NSIGT',         'CONTAINER',    true,  ARRAY['NSIGT']::text[],                      30),
        ('APMT',   'APM Terminals / Gateway Terminals India',    'APM Terminals', 'CONTAINER',    true,  ARRAY['GTI','APM','APMT']::text[],           40),
        ('BMCT',   'Bharat Mumbai Container Terminals (PSA)',    'PSA',           'CONTAINER',    true,  ARRAY['BMCTPL','BMCTPSA','PSA']::text[],     50),
        ('NSDT',   'Nhava Sheva Distribution Terminal',          'NSDT',          'MULTIPURPOSE', false, ARRAY['NSDT']::text[],                       60),
        ('JNPCT',  'Jawaharlal Nehru Port Container Terminal',   'JNPA',          'CONTAINER',    true,  ARRAY['JNPCT']::text[],                       70),
        ('JN_PORT','JN Port (all terminals)',                    'JNPA',          'TOTAL',        true,  ARRAY['JN PORT','JNPORT','JN_PORT']::text[], 90)
        ON CONFLICT ON CONSTRAINT uq_perf_terminals_code DO NOTHING""",
    """CREATE TABLE IF NOT EXISTS core.perf_daily_snapshot (
        id          bigserial PRIMARY KEY,
        report_date date NOT NULL,
        as_of_ts    timestamptz,
        source_file text,
        created_at  timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_perf_daily_snapshot UNIQUE (report_date))""",
    """CREATE TABLE IF NOT EXISTS core.perf_daily_traffic (
        id              bigserial PRIMARY KEY,
        report_date     date NOT NULL,
        terminal_code   text NOT NULL,
        period          text NOT NULL CHECK (period IN ('DAY','MONTH','YEAR')),
        vessels         int,
        imp_teus        numeric,
        exp_teus        numeric,
        total_teus      numeric,
        rakes           int,
        rail_dis_teus   numeric,
        rail_ldg_teus   numeric,
        rail_total_teus numeric,
        created_at      timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_perf_daily_traffic UNIQUE (report_date, terminal_code, period))""",
    "CREATE INDEX IF NOT EXISTS idx_perf_traffic_date_term ON core.perf_daily_traffic (report_date, terminal_code)",
    "CREATE INDEX IF NOT EXISTS idx_perf_traffic_term_period ON core.perf_daily_traffic (terminal_code, period)",
    """CREATE TABLE IF NOT EXISTS core.perf_daily_tonnage (
        id                bigserial PRIMARY KEY,
        report_date       date NOT NULL,
        category          text NOT NULL CHECK (category IN
                            ('BPCL','NSDT','JJLTPL','OTHER','BULK_TOTAL','CONTAINER_TOTAL','JNPA_TOTAL')),
        period            text NOT NULL CHECK (period IN ('DAY','MONTH','YEAR')),
        vessels           int,
        liquid_tonnes     numeric,
        dry_bulk_tonnes   numeric,
        break_bulk_tonnes numeric,
        total_tonnes      numeric,
        created_at        timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_perf_daily_tonnage UNIQUE (report_date, category, period))""",
    "CREATE INDEX IF NOT EXISTS idx_perf_tonnage_date ON core.perf_daily_tonnage (report_date)",
    """CREATE TABLE IF NOT EXISTS core.perf_daily_terminal_status (
        id                        bigserial PRIMARY KEY,
        report_date               date NOT NULL,
        terminal_code             text NOT NULL,
        icd_pendency_teus         numeric,
        cfs_pendency_teus         numeric,
        yard_import_teus          numeric,
        yard_export_teus          numeric,
        yard_transhipment_teus    numeric,
        yard_total_teus           numeric,
        yard_usable_capacity_teus numeric,
        yard_occupancy_pct        numeric,
        gate_in_teus              numeric,
        gate_out_teus             numeric,
        gate_total_teus           numeric,
        reefer_total_slots        int,
        reefer_occupied_slots     int,
        reefer_available_slots    int,
        created_at                timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_perf_daily_status UNIQUE (report_date, terminal_code))""",
    "CREATE INDEX IF NOT EXISTS idx_perf_status_date ON core.perf_daily_terminal_status (report_date)",
    """CREATE TABLE IF NOT EXISTS core.perf_daily_vessel (
        id                  bigserial PRIMARY KEY,
        report_date         date NOT NULL,
        terminal_code       text NOT NULL,
        berth_no            text NOT NULL,
        via_no              text,
        vessel_name         text,
        cargo_commodity     text,
        berthed_on          timestamptz,
        expected_completion timestamptz,
        created_at          timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_perf_daily_vessel UNIQUE (report_date, terminal_code, berth_no, via_no))""",
    "CREATE INDEX IF NOT EXISTS idx_perf_vessels_date ON core.perf_daily_vessel (report_date)",
    """CREATE TABLE IF NOT EXISTS core.perf_monthly_teu (
        id             bigserial PRIMARY KEY,
        fiscal_year    text NOT NULL,
        month_date     date NOT NULL,
        year_label     text,
        month_label    text,
        terminal_code  text NOT NULL,
        vessel_calls   int,
        discharge_teus numeric,
        load_teus      numeric,
        total_teus     numeric,
        created_at     timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_perf_monthly_teu UNIQUE (month_date, terminal_code))""",
    "CREATE INDEX IF NOT EXISTS idx_perf_monthly_term ON core.perf_monthly_teu (terminal_code, month_date)",
    """CREATE TABLE IF NOT EXISTS core.perf_ldb_port_dwell (
        id               bigserial PRIMARY KEY,
        report_month     date NOT NULL,
        terminal_code    text NOT NULL,
        cycle            text NOT NULL CHECK (cycle IN ('IMPORT','EXPORT')),
        segment          text NOT NULL CHECK (segment IN ('OVERALL','TRUCK','TRAIN')),
        dwell_hours      numeric,
        dwell_hours_prev numeric,
        created_at       timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_perf_ldb_port_dwell UNIQUE (report_month, terminal_code, cycle, segment))""",
    "CREATE INDEX IF NOT EXISTS idx_perf_ldb_dwell_month ON core.perf_ldb_port_dwell (report_month, cycle)",
    """CREATE TABLE IF NOT EXISTS core.perf_ldb_facility_dwell (
        id                bigserial PRIMARY KEY,
        report_month      date NOT NULL,
        facility_type     text NOT NULL CHECK (facility_type IN ('CFS','ICD')),
        facility_name     text NOT NULL,
        facility_name_norm text NOT NULL,
        dwell_hours       numeric,
        dwell_hours_prev  numeric,
        created_at        timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_perf_ldb_facility_dwell UNIQUE (report_month, facility_type, facility_name_norm))""",
    "CREATE INDEX IF NOT EXISTS idx_perf_ldb_facility_month ON core.perf_ldb_facility_dwell (report_month, facility_type)",
    """CREATE TABLE IF NOT EXISTS core.perf_ldb_congestion (
        id                bigserial PRIMARY KEY,
        report_month      date NOT NULL,
        cycle             text NOT NULL CHECK (cycle IN ('IMPORT','EXPORT')),
        cluster_no        int  NOT NULL,
        cluster_name      text,
        cfs_count         int,
        pct_containers    numeric,
        congestion_level  text CHECK (congestion_level IN ('HIGH','MEDIUM','LOW')),
        created_at        timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_perf_ldb_congestion UNIQUE (report_month, cycle, cluster_no))""",
    """CREATE TABLE IF NOT EXISTS core.perf_ldb_route_movement (
        id             bigserial PRIMARY KEY,
        report_month   date NOT NULL,
        cycle          text NOT NULL CHECK (cycle IN ('IMPORT','EXPORT')),
        transport_mode text NOT NULL CHECK (transport_mode IN ('TRAIN','TRUCK')),
        route_name     text NOT NULL,
        pct_share      numeric,
        created_at     timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_perf_ldb_route UNIQUE (report_month, cycle, transport_mode, route_name))""",
    """CREATE TABLE IF NOT EXISTS core.perf_ldb_weather (
        id            bigserial PRIMARY KEY,
        report_month  date NOT NULL,
        terminal_code text NOT NULL,
        cycle         text NOT NULL CHECK (cycle IN ('IMPORT','EXPORT')),
        weather       text NOT NULL CHECK (weather IN ('NORMAL','ABNORMAL')),
        dwell_hours   numeric,
        created_at    timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_perf_ldb_weather UNIQUE (report_month, terminal_code, cycle, weather))""",
    # Upgrade path (mirrors migration 0029): a dev/mock DB that first created
    # perf_daily_vessels under the OLD 0028 3-column key keeps it, because the
    # CREATE TABLE IF NOT EXISTS above is a no-op on an existing table. Swap the
    # constraint to include via_no in place — idempotent (no-op once it already
    # includes via_no, as on a fresh DB created by the statement above).
    """DO $$
    DECLARE v_cols text;
    BEGIN
        IF to_regclass('core.perf_daily_vessel') IS NULL THEN RETURN; END IF;
        SELECT string_agg(a.attname, ',' ORDER BY k.ord) INTO v_cols
        FROM pg_constraint c
        JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.conname = 'uq_perf_daily_vessel'
          AND c.conrelid = 'core.perf_daily_vessel'::regclass AND c.contype = 'u';
        IF v_cols = 'report_date,terminal_code,berth_no' THEN
            ALTER TABLE core.perf_daily_vessel DROP CONSTRAINT uq_perf_daily_vessel;
            ALTER TABLE core.perf_daily_vessel ADD CONSTRAINT uq_perf_daily_vessel
                UNIQUE (report_date, terminal_code, berth_no, via_no);
        ELSIF v_cols IS NULL THEN
            ALTER TABLE core.perf_daily_vessel ADD CONSTRAINT uq_perf_daily_vessel
                UNIQUE (report_date, terminal_code, berth_no, via_no);
        END IF;
    END $$""",
    # Upgrade path (mirrors migration 0038): declared NUMERIC SCALE + row-level
    # provenance. Without a scale, a float measure is stored as its full binary
    # expansion (84.8700000000000045…) and served verbatim to the API and CSV export.
    # Setting the scale rounds existing rows too. Idempotent: the guard skips any
    # column that already has a scale.
    """DO $$
    DECLARE
        v_scale int;
        targets text[][] := ARRAY[
            ['perf_daily_traffic','imp_teus','16','2'],
            ['perf_daily_traffic','exp_teus','16','2'],
            ['perf_daily_traffic','total_teus','16','2'],
            ['perf_daily_traffic','rail_dis_teus','16','2'],
            ['perf_daily_traffic','rail_ldg_teus','16','2'],
            ['perf_daily_traffic','rail_total_teus','16','2'],
            ['perf_daily_tonnage','liquid_tonnes','16','2'],
            ['perf_daily_tonnage','dry_bulk_tonnes','16','2'],
            ['perf_daily_tonnage','break_bulk_tonnes','16','2'],
            ['perf_daily_tonnage','total_tonnes','16','2'],
            ['perf_daily_terminal_status','icd_pendency_teus','16','2'],
            ['perf_daily_terminal_status','cfs_pendency_teus','16','2'],
            ['perf_daily_terminal_status','yard_import_teus','16','2'],
            ['perf_daily_terminal_status','yard_export_teus','16','2'],
            ['perf_daily_terminal_status','yard_transhipment_teus','16','2'],
            ['perf_daily_terminal_status','yard_total_teus','16','2'],
            ['perf_daily_terminal_status','yard_usable_capacity_teus','16','2'],
            ['perf_daily_terminal_status','yard_occupancy_pct','6','2'],
            ['perf_daily_terminal_status','gate_in_teus','16','2'],
            ['perf_daily_terminal_status','gate_out_teus','16','2'],
            ['perf_daily_terminal_status','gate_total_teus','16','2'],
            ['perf_monthly_teu','discharge_teus','16','2'],
            ['perf_monthly_teu','load_teus','16','2'],
            ['perf_monthly_teu','total_teus','16','2'],
            ['perf_ldb_port_dwell','dwell_hours','8','2'],
            ['perf_ldb_port_dwell','dwell_hours_prev','8','2'],
            ['perf_ldb_facility_dwell','dwell_hours','8','2'],
            ['perf_ldb_facility_dwell','dwell_hours_prev','8','2'],
            ['perf_ldb_weather','dwell_hours','8','2'],
            ['perf_ldb_congestion','pct_containers','6','2'],
            ['perf_ldb_route_movement','pct_share','6','2']
        ];
        prov text[] := ARRAY[
            'perf_daily_traffic','perf_daily_tonnage','perf_daily_terminal_status',
            'perf_daily_vessels','perf_monthly_teu','perf_ldb_port_dwell',
            'perf_ldb_facility_dwell','perf_ldb_congestion','perf_ldb_route_movement',
            'perf_ldb_weather'];
        t text;
    BEGIN
        FOR i IN 1 .. array_length(targets, 1) LOOP
            IF to_regclass('core.' || targets[i][1]) IS NULL THEN CONTINUE; END IF;
            SELECT numeric_scale INTO v_scale FROM information_schema.columns
            WHERE table_schema='core' AND table_name=targets[i][1]
              AND column_name=targets[i][2];
            IF v_scale IS NULL AND EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema='core' AND table_name=targets[i][1]
                      AND column_name=targets[i][2]) THEN
                EXECUTE format('ALTER TABLE core.%I ALTER COLUMN %I TYPE numeric(%s,%s)',
                               targets[i][1], targets[i][2], targets[i][3], targets[i][4]);
            END IF;
        END LOOP;
        FOREACH t IN ARRAY prov LOOP
            IF to_regclass('core.' || t) IS NULL THEN CONTINUE; END IF;
            EXECUTE format('ALTER TABLE core.%I ADD COLUMN IF NOT EXISTS source_file text', t);
            EXECUTE format('ALTER TABLE core.%I ADD COLUMN IF NOT EXISTS upload_id uuid', t);
            EXECUTE format('ALTER TABLE core.%I ADD COLUMN IF NOT EXISTS uploaded_at timestamptz', t);
            EXECUTE format('CREATE INDEX IF NOT EXISTS ix_%s_upload ON jnpa.%I (upload_id)', t, t);
        END LOOP;
    END $$""",
    "ALTER TABLE core.perf_daily_snapshot ADD COLUMN IF NOT EXISTS upload_id uuid",
    "ALTER TABLE core.perf_daily_snapshot ADD COLUMN IF NOT EXISTS uploaded_at timestamptz",
]


async def ensure_performance_schema(dsn: Optional[str] = None) -> None:
    """Create the Performance & Daily Reports tables + terminal seed if absent.
    Idempotent, additive. Mirrors migration 0028."""
    if os.getenv("JNPA_RUNTIME_DDL", "0") != "1":
        # schema-v3: DDL is owned by infra/postgres/v3 migrations, never runtime.
        return
    from sqlalchemy import text

    from jnpa_shared.db import get_engine

    engine = get_engine(dsn)
    async with engine.begin() as conn:
        for stmt in _DDL:
            await conn.execute(text(stmt))
    log.info("performance_schema_ready")
