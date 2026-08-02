-- ============================================================
-- 0107  Traffic readings — TomTom Traffic Flow + Incidents integration
-- Additive only. Naming: singular, per architecture conventions.
--
-- One row per successful LIVE fetch by services/traffic (the
-- /api/traffic/current surface). Doubles as the audit trail of what the
-- external TomTom API actually said and as the DATABASE fallback rung when
-- TomTom AND Redis are both unavailable
-- (LIVE -> CACHED -> DATABASE -> SYNTHETIC). `payload` keeps the full
-- normalised traffic block + incident list so the DB fallback loses nothing.
--
-- Distinct from core.traffic_snapshot (per-corridor-segment rows fed by the
-- simulation/ingest pipeline for the map overlay) — no duplication.
-- Touches NO existing table.
-- ============================================================
BEGIN;

CREATE TABLE IF NOT EXISTS core.traffic_reading (
    id               bigserial PRIMARY KEY,
    latitude         double precision NOT NULL,
    longitude        double precision NOT NULL,
    current_speed    numeric,                    -- km/h
    free_flow_speed  numeric,                    -- km/h
    congestion_level text,                       -- LOW | MEDIUM | HIGH | SEVERE | UNKNOWN
    delay_seconds    numeric,                    -- seconds vs free flow
    source           text NOT NULL DEFAULT 'TOMTOM',
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now());

CREATE INDEX IF NOT EXISTS idx_traffic_reading_created
    ON core.traffic_reading (created_at DESC);
-- Coordinate-bucketed lookup (2 dp ~ 1.1 km) — matches the repository's
-- latest-reading / history queries exactly.
CREATE INDEX IF NOT EXISTS idx_traffic_reading_coords
    ON core.traffic_reading (round(CAST(latitude AS numeric), 2),
                             round(CAST(longitude AS numeric), 2),
                             created_at DESC);

COMMIT;
