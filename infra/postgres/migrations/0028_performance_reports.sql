-- =====================================================================
-- 0028_performance_reports.sql  —  UC-III Module 12: Performance & Daily Reports
-- =====================================================================
-- PURELY ADDITIVE. Creates a NEW analytical layer for the official JNPA
-- performance reports (Daily Status Report, monthly JN Port TEUs, and the
-- NLDS/LDB Analytics report). Every statement is CREATE ... IF NOT EXISTS /
-- INSERT ... ON CONFLICT DO NOTHING, so re-running is a no-op.
--
-- Does NOT touch / alter / drop any existing table, view, index, sequence,
-- auth/JWT/RBAC policy, or any other module. All objects live in schema `jnpa`
-- under the `perf_` namespace (the LDB *analytics* tables use `perf_ldb_` to
-- avoid any confusion with the pre-existing operational `jnpa.ldb_movements`).
--
-- Cross-module linkage is BY VALUE ONLY (terminal_code text, no FK), mirroring
-- how jnpa.cfs_ecy_movements soft-links to jnpa.cargo.
--
-- Apply:
--   psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0028_performance_reports.sql
-- (also re-applied idempotently at gateway boot via
--  gateway/performance_ext.ensure_performance_schema)
-- =====================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

-- ---------------------------------------------------------------------
-- Dimension: canonical terminals (reconciles GTI≡APMT, BMCTPL≡BMCT, …)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.perf_terminals (
    id            bigserial PRIMARY KEY,
    code          text NOT NULL,                 -- canonical: NSFT/NSICT/NSIGT/APMT/BMCT/NSDT/JN_PORT/JNPCT
    full_name     text,
    operator      text,
    terminal_type text NOT NULL DEFAULT 'CONTAINER'
                       CHECK (terminal_type IN ('CONTAINER','MULTIPURPOSE','LIQUID','TOTAL')),
    is_container  boolean NOT NULL DEFAULT true,
    aliases       text[]  NOT NULL DEFAULT '{}', -- alternative labels seen across the 3 report families
    sort_order    int     NOT NULL DEFAULT 100,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_perf_terminals_code UNIQUE (code)
);

INSERT INTO jnpa.perf_terminals (code, full_name, operator, terminal_type, is_container, aliases, sort_order) VALUES
  ('NSFT',   'Nhava Sheva Freeport Terminal',            'NSFT',            'CONTAINER',    true,  ARRAY['NSFT']::text[],                       10),
  ('NSICT',  'Nhava Sheva International Container Terminal','DP World',      'CONTAINER',    true,  ARRAY['NSICT']::text[],                      20),
  ('NSIGT',  'Nhava Sheva India Gateway Terminal',        'NSIGT',          'CONTAINER',    true,  ARRAY['NSIGT']::text[],                      30),
  ('APMT',   'APM Terminals / Gateway Terminals India',   'APM Terminals',  'CONTAINER',    true,  ARRAY['GTI','APM','APMT']::text[],           40),
  ('BMCT',   'Bharat Mumbai Container Terminals (PSA)',   'PSA',            'CONTAINER',    true,  ARRAY['BMCTPL','BMCTPSA','PSA']::text[],     50),
  ('NSDT',   'Nhava Sheva Distribution Terminal',         'NSDT',           'MULTIPURPOSE', false, ARRAY['NSDT']::text[],                       60),
  ('JNPCT',  'Jawaharlal Nehru Port Container Terminal',  'JNPA',           'CONTAINER',    true,  ARRAY['JNPCT']::text[],                       70),
  ('JN_PORT','JN Port (all terminals)',                   'JNPA',           'TOTAL',        true,  ARRAY['JN PORT','JNPORT','JN_PORT']::text[], 90)
ON CONFLICT ON CONSTRAINT uq_perf_terminals_code DO NOTHING;

-- ---------------------------------------------------------------------
-- Daily Status Report — one snapshot header per report date (07:00 IST)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.perf_daily_snapshot (
    id          bigserial PRIMARY KEY,
    report_date date NOT NULL,
    as_of_ts    timestamptz,          -- the "AS ON dd-mm-yyyy AT 07:00 HRS" instant (IST)
    source_file text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_perf_daily_snapshot UNIQUE (report_date)
);

-- Sections (A) Container Terminal TEUs + (C) Rail Operations (merged: same grain)
CREATE TABLE IF NOT EXISTS jnpa.perf_daily_traffic (
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
    CONSTRAINT uq_perf_daily_traffic UNIQUE (report_date, terminal_code, period)
);
CREATE INDEX IF NOT EXISTS idx_perf_traffic_date_term ON jnpa.perf_daily_traffic (report_date, terminal_code);
CREATE INDEX IF NOT EXISTS idx_perf_traffic_term_period ON jnpa.perf_daily_traffic (terminal_code, period);

-- Section (B) Total Traffic Throughput in Tonnage
CREATE TABLE IF NOT EXISTS jnpa.perf_daily_tonnage (
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
    CONSTRAINT uq_perf_daily_tonnage UNIQUE (report_date, category, period)
);
CREATE INDEX IF NOT EXISTS idx_perf_tonnage_date ON jnpa.perf_daily_tonnage (report_date);

-- Sections (D) Import Pendency + (E) Yard Inventory + (F) Gate Movements + (G) Reefer
-- (all terminal-keyed snapshots for the same report date → one merged row per terminal)
CREATE TABLE IF NOT EXISTS jnpa.perf_daily_terminal_status (
    id                        bigserial PRIMARY KEY,
    report_date               date NOT NULL,
    terminal_code             text NOT NULL,       -- incl. 'TOTAL' aggregate column
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
    CONSTRAINT uq_perf_daily_status UNIQUE (report_date, terminal_code)
);
CREATE INDEX IF NOT EXISTS idx_perf_status_date ON jnpa.perf_daily_terminal_status (report_date);

-- Section (H) Vessels Under Operation
CREATE TABLE IF NOT EXISTS jnpa.perf_daily_vessels (
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
    CONSTRAINT uq_perf_daily_vessel UNIQUE (report_date, terminal_code, berth_no)
);
CREATE INDEX IF NOT EXISTS idx_perf_vessels_date ON jnpa.perf_daily_vessels (report_date);

-- ---------------------------------------------------------------------
-- Monthly JN Port TEUs (per terminal, per month, with vessel calls)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.perf_monthly_teu (
    id             bigserial PRIMARY KEY,
    fiscal_year    text NOT NULL,          -- 'FY-2025-26' | 'FY-2026-27'
    month_date     date NOT NULL,          -- 1st-of-month key, e.g. 2025-04-01
    year_label     text,                   -- '2025'
    month_label    text,                   -- 'APR'
    terminal_code  text NOT NULL,          -- NSDT/NSFT/NSICT/NSIGT/APMT/BMCT/JN_PORT
    vessel_calls   int,
    discharge_teus numeric,
    load_teus      numeric,
    total_teus     numeric,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_perf_monthly_teu UNIQUE (month_date, terminal_code)
);
CREATE INDEX IF NOT EXISTS idx_perf_monthly_term ON jnpa.perf_monthly_teu (terminal_code, month_date);

-- ---------------------------------------------------------------------
-- NLDS / LDB Analytics report (monthly)  —  perf_ldb_* namespace
-- ---------------------------------------------------------------------
-- Port dwell time by terminal / cycle / transit-segment (current + prev month)
CREATE TABLE IF NOT EXISTS jnpa.perf_ldb_port_dwell (
    id               bigserial PRIMARY KEY,
    report_month     date NOT NULL,          -- 1st-of-month, e.g. 2026-03-01
    terminal_code    text NOT NULL,
    cycle            text NOT NULL CHECK (cycle IN ('IMPORT','EXPORT')),
    segment          text NOT NULL CHECK (segment IN ('OVERALL','TRUCK','TRAIN')),
    dwell_hours      numeric,
    dwell_hours_prev numeric,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_perf_ldb_port_dwell UNIQUE (report_month, terminal_code, cycle, segment)
);
CREATE INDEX IF NOT EXISTS idx_perf_ldb_dwell_month ON jnpa.perf_ldb_port_dwell (report_month, cycle);

-- CFS / ICD facility dwell time (current + prev month)
CREATE TABLE IF NOT EXISTS jnpa.perf_ldb_facility_dwell (
    id                bigserial PRIMARY KEY,
    report_month      date NOT NULL,
    facility_type     text NOT NULL CHECK (facility_type IN ('CFS','ICD')),
    facility_name     text NOT NULL,
    facility_name_norm text NOT NULL,
    dwell_hours       numeric,
    dwell_hours_prev  numeric,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_perf_ldb_facility_dwell UNIQUE (report_month, facility_type, facility_name_norm)
);
CREATE INDEX IF NOT EXISTS idx_perf_ldb_facility_month ON jnpa.perf_ldb_facility_dwell (report_month, facility_type);

-- Region congestion by CFS cluster / cycle
CREATE TABLE IF NOT EXISTS jnpa.perf_ldb_congestion (
    id                bigserial PRIMARY KEY,
    report_month      date NOT NULL,
    cycle             text NOT NULL CHECK (cycle IN ('IMPORT','EXPORT')),
    cluster_no        int  NOT NULL,
    cluster_name      text,
    cfs_count         int,
    pct_containers    numeric,
    congestion_level  text CHECK (congestion_level IN ('HIGH','MEDIUM','LOW')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_perf_ldb_congestion UNIQUE (report_month, cycle, cluster_no)
);

-- Container movement modal share by route / cycle
CREATE TABLE IF NOT EXISTS jnpa.perf_ldb_route_movement (
    id             bigserial PRIMARY KEY,
    report_month   date NOT NULL,
    cycle          text NOT NULL CHECK (cycle IN ('IMPORT','EXPORT')),
    transport_mode text NOT NULL CHECK (transport_mode IN ('TRAIN','TRUCK')),
    route_name     text NOT NULL,
    pct_share      numeric,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_perf_ldb_route UNIQUE (report_month, cycle, transport_mode, route_name)
);

-- Weather-conditioned dwell (terminal-wise)
CREATE TABLE IF NOT EXISTS jnpa.perf_ldb_weather (
    id            bigserial PRIMARY KEY,
    report_month  date NOT NULL,
    terminal_code text NOT NULL,
    cycle         text NOT NULL CHECK (cycle IN ('IMPORT','EXPORT')),
    weather       text NOT NULL CHECK (weather IN ('NORMAL','ABNORMAL')),
    dwell_hours   numeric,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_perf_ldb_weather UNIQUE (report_month, terminal_code, cycle, weather)
);
