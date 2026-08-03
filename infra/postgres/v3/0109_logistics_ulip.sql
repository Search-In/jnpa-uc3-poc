-- ============================================================
-- 0109  Logistics intelligence — ULIP (Unified Logistics Interface Platform)
-- Additive only. Naming: singular, per architecture conventions.
--
-- Three tables for the /api/logistics/* surfaces (services/logistics):
--
--   core.logistics_event    one row per normalised ULIP logistics event
--                           (FASTag toll crossing, LDB container movement).
--                           Deduped on (ref_type, ref_id, event_type,
--                           event_ts, location) so re-fetching a reference
--                           never duplicates its history. Doubles as the
--                           DATABASE fallback rung
--                           (LIVE -> CACHED -> DATABASE -> FALLBACK).
--   core.logistics_tracking one snapshot row per tracked reference (vehicle
--                           registration / ISO-6346 container), upserted on
--                           every successful LIVE fetch.
--   core.ulip_api_audit     one row per outbound ULIP API call, success or
--                           failure — the audit trail of what the external
--                           ULIP platform actually said.
--
-- Distinct from core.fastag_transaction (the /api/fastag vertical's raw NPCI
-- rows) and core.ldb_movement (the /api/ldb adapter's movement store) — this
-- module owns its own normalised, cross-modal event stream. No existing
-- table is touched.
-- ============================================================
BEGIN;

CREATE TABLE IF NOT EXISTS core.logistics_event (
    id          bigserial PRIMARY KEY,
    ref_type    text NOT NULL,              -- VEHICLE | CONTAINER
    ref_id      text NOT NULL,              -- registration no. / container no.
    event_type  text NOT NULL,              -- TOLL_CROSSING | CONTAINER_MOVEMENT
    event_ts    timestamptz,                -- upstream event time (may be absent)
    location    text,                       -- toll plaza / terminal / place
    latitude    double precision,
    longitude   double precision,
    source      text NOT NULL DEFAULT 'ULIP',
    source_api  text,                       -- FASTAG | LDB (ULIP API that answered)
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,  -- raw upstream item
    created_at  timestamptz NOT NULL DEFAULT now());

-- Null-safe dedup key — the repository's INSERT ... ON CONFLICT target.
CREATE UNIQUE INDEX IF NOT EXISTS uq_logistics_event_dedup
    ON core.logistics_event (ref_type, ref_id, event_type,
                             COALESCE(event_ts, 'epoch'::timestamptz),
                             COALESCE(location, ''));
CREATE INDEX IF NOT EXISTS idx_logistics_event_ref
    ON core.logistics_event (ref_id, event_ts DESC);
CREATE INDEX IF NOT EXISTS idx_logistics_event_created
    ON core.logistics_event (created_at DESC);

CREATE TABLE IF NOT EXISTS core.logistics_tracking (
    id             bigserial PRIMARY KEY,
    ref_type       text NOT NULL,           -- VEHICLE | CONTAINER
    ref_id         text NOT NULL,
    status         text NOT NULL DEFAULT 'UNKNOWN',  -- IN_TRANSIT | IDLE | UNKNOWN
    last_event     text,
    last_location  text,
    last_event_ts  timestamptz,
    event_count    integer NOT NULL DEFAULT 0,
    source         text NOT NULL DEFAULT 'ULIP',
    payload        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_logistics_tracking_ref UNIQUE (ref_type, ref_id));

CREATE INDEX IF NOT EXISTS idx_logistics_tracking_updated
    ON core.logistics_tracking (updated_at DESC);

CREATE TABLE IF NOT EXISTS core.ulip_api_audit (
    id          bigserial PRIMARY KEY,
    api_name    text NOT NULL,              -- e.g. FASTAG/01, LDB/01
    ref_type    text,
    ref_id      text,
    ok          boolean NOT NULL DEFAULT false,
    http_status integer,
    latency_ms  numeric,
    error       text,                       -- typed client error (credentials redacted)
    response    jsonb NOT NULL DEFAULT '{}'::jsonb,  -- summary of the answer
    created_at  timestamptz NOT NULL DEFAULT now());

CREATE INDEX IF NOT EXISTS idx_ulip_api_audit_created
    ON core.ulip_api_audit (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ulip_api_audit_ref
    ON core.ulip_api_audit (ref_id, created_at DESC);

COMMIT;
