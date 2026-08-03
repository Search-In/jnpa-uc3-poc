-- ============================================================
-- 0108  Air-quality readings — OpenAQ Air Quality Intelligence integration
-- Additive only. (Table name plural per the UC-3 integration spec.)
--
-- One row per successful LIVE fetch by services/air_quality (the
-- /api/air-quality/current surface). Doubles as the audit trail of what the
-- external OpenAQ API actually said and as the DATABASE fallback rung when
-- OpenAQ AND Redis are both unavailable
-- (LIVE -> CACHED -> DATABASE -> SYNTHETIC). `payload` keeps the full
-- normalised air_quality block so the DB fallback loses nothing.
-- Touches NO existing table.
-- ============================================================
BEGIN;

CREATE TABLE IF NOT EXISTS core.air_quality_readings (
    id         bigserial PRIMARY KEY,
    latitude   double precision NOT NULL,
    longitude  double precision NOT NULL,
    pm25       numeric,                    -- µg/m³
    pm10       numeric,                    -- µg/m³
    no2        numeric,                    -- µg/m³
    so2        numeric,                    -- µg/m³
    co         numeric,                    -- µg/m³ (mg/m³ converted on ingest)
    o3         numeric,                    -- µg/m³
    aq_status  text,                       -- GOOD | MODERATE | UNHEALTHY | VERY_UNHEALTHY | UNKNOWN
    source     text NOT NULL DEFAULT 'OPENAQ',
    payload    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now());

CREATE INDEX IF NOT EXISTS idx_air_quality_readings_created
    ON core.air_quality_readings (created_at DESC);
-- Coordinate-bucketed lookup (2 dp ~ 1.1 km) — matches the repository's
-- latest-reading / history queries exactly (same convention as 0107).
CREATE INDEX IF NOT EXISTS idx_air_quality_readings_coords
    ON core.air_quality_readings (round(CAST(latitude AS numeric), 2),
                                  round(CAST(longitude AS numeric), 2),
                                  created_at DESC);

COMMIT;
