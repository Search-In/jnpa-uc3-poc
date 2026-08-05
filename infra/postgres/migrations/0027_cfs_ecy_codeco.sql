-- 0027_cfs_ecy_codeco.sql
-- CFS-ECY CODECO Integration (UC-III, module 13) — off-dock container gate
-- movements from the JNPA CODECO feeds (CFS-CODECO.xlsx / ECY-CODECO.xlsx).
--
-- PURELY ADDITIVE: one new table + one derived view. Every statement is
-- CREATE ... IF NOT EXISTS / CREATE OR REPLACE VIEW, so re-running is a no-op and
-- NOTHING existing is dropped or altered. It does NOT touch cargo / empty_container
-- / vehicle / driver / transporter / auth tables — it only records raw gate events
-- and soft-links to jnpa.cargo BY VALUE (container_number), never by FK.
--
-- Source data (CODECO = UN/EDIFACT container gate-in/gate-out report):
--   Container Number  ->  container_number  (ISO-6346, validated)
--   Timestamp         ->  event_ts          (parsed DD/MM/YYYY HH:MM, IST -> timestamptz)
--   Mode  In|Out      ->  mode  IN|OUT
--   (facility derived from the source filename, NOT a column in the file)
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0027_cfs_ecy_codeco.sql
-- Runtime-applied at gateway boot by gateway/cfs_ecy_ext.ensure_cfs_ecy_schema().

CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

-- Raw CODECO gate-movement events. Append-only: this table IS its own history.
CREATE TABLE IF NOT EXISTS jnpa.cfs_ecy_movements (
    id               bigserial PRIMARY KEY,
    facility_type    text NOT NULL
                     CHECK (facility_type IN ('CFS','ECY')),   -- derived from source file
    container_number text NOT NULL,                            -- ISO-6346
    iso_valid        boolean NOT NULL DEFAULT true,            -- jnpa_shared.iso6346 check-digit
    event_ts         timestamptz NOT NULL,                     -- gate event time (IST-parsed)
    mode             text NOT NULL
                     CHECK (mode IN ('IN','OUT')),             -- normalized from In / Out
    source           text NOT NULL DEFAULT 'CODECO',
    source_file      text,                                     -- e.g. CFS-CODECO.xlsx
    created_at       timestamptz NOT NULL DEFAULT now(),
    -- Idempotent ingest key: the same gate event never lands twice
    -- (drops the 1 exact-duplicate CFS row on ON CONFLICT DO NOTHING).
    CONSTRAINT uq_cfs_ecy_movement UNIQUE (facility_type, container_number, event_ts, mode)
);

CREATE INDEX IF NOT EXISTS idx_cfsecy_container
    ON jnpa.cfs_ecy_movements (container_number, event_ts DESC);
CREATE INDEX IF NOT EXISTS idx_cfsecy_facility_ts
    ON jnpa.cfs_ecy_movements (facility_type, event_ts DESC);
CREATE INDEX IF NOT EXISTS idx_cfsecy_facility_mode_ts
    ON jnpa.cfs_ecy_movements (facility_type, mode, event_ts DESC);

-- Derived dwell view: pairs each container's earliest IN with its latest OUT per
-- facility. Dwell (hours) is computed for CFS ONLY (the file where containers have
-- paired In+Out cycles). ECY dwell is deliberately NULL — the ECY feed carries a
-- single event per container, so a dwell there would be fabricated. No write path;
-- always re-computed from the raw table.
CREATE OR REPLACE VIEW jnpa.v_cfs_ecy_dwell AS
SELECT
    m.container_number,
    m.facility_type,
    min(m.event_ts) FILTER (WHERE m.mode = 'IN')  AS first_in_ts,
    max(m.event_ts) FILTER (WHERE m.mode = 'OUT') AS last_out_ts,
    count(*)        FILTER (WHERE m.mode = 'IN')  AS in_events,
    count(*)        FILTER (WHERE m.mode = 'OUT') AS out_events,
    CASE
        WHEN m.facility_type = 'CFS'
         AND min(m.event_ts) FILTER (WHERE m.mode = 'IN')  IS NOT NULL
         AND max(m.event_ts) FILTER (WHERE m.mode = 'OUT') IS NOT NULL
         AND max(m.event_ts) FILTER (WHERE m.mode = 'OUT')
             >= min(m.event_ts) FILTER (WHERE m.mode = 'IN')
        THEN round(
            extract(epoch FROM (
                max(m.event_ts) FILTER (WHERE m.mode = 'OUT')
              - min(m.event_ts) FILTER (WHERE m.mode = 'IN')
            )) / 3600.0::numeric, 2)
        ELSE NULL
    END AS dwell_hours
FROM jnpa.cfs_ecy_movements m
GROUP BY m.container_number, m.facility_type;
