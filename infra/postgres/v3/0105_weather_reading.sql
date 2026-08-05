-- ============================================================
-- 0105  Weather readings — Open-Meteo Weather + Marine integration
-- Additive only. Naming: singular, per architecture conventions.
--
-- One row per successful LIVE fetch by services/weather (the /api/weather
-- surface). Doubles as the audit trail of what the external API actually
-- said and as the DB fallback rung when Open-Meteo AND Redis are both
-- unavailable (LIVE -> CACHED -> SYNTHETIC). `payload` keeps the full
-- normalised weather+marine blocks so the DB fallback loses nothing.
-- Touches NO existing table.
-- ============================================================
BEGIN;

CREATE TABLE IF NOT EXISTS core.weather_reading (
    id             bigserial PRIMARY KEY,
    latitude       double precision NOT NULL,
    longitude      double precision NOT NULL,
    temperature    numeric,                    -- °C
    wind_speed     numeric,                    -- km/h
    wind_direction numeric,                    -- degrees
    visibility     numeric,                    -- metres
    precipitation  numeric,                    -- mm
    wave_height    numeric,                    -- metres
    wave_period    numeric,                    -- seconds
    source         text NOT NULL DEFAULT 'OPEN_METEO',
    payload        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now());

CREATE INDEX IF NOT EXISTS idx_weather_reading_created
    ON core.weather_reading (created_at DESC);
-- Coordinate-bucketed lookup (2 dp ~ 1.1 km) — matches the repository's
-- latest-reading / history queries exactly.
CREATE INDEX IF NOT EXISTS idx_weather_reading_coords
    ON core.weather_reading (round(CAST(latitude AS numeric), 2),
                             round(CAST(longitude AS numeric), 2),
                             created_at DESC);

COMMIT;
