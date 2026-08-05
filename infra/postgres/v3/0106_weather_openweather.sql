-- ============================================================
-- 0106  Weather readings — OpenWeatherMap enrichment (additive only)
--
-- Extends core.weather_reading (0105) with the two OpenWeather-only scalar
-- observations worth querying directly (humidity %, cloud cover %). The full
-- normalised openweather block (rain, condition, label, temperature
-- validation, …) rides in the existing `payload` jsonb next to the
-- weather/marine blocks, so the DB fallback rung loses nothing.
-- `source` now also records 'OPEN_METEO+OPENWEATHER' when both providers
-- were LIVE for the persisted reading (column default unchanged).
-- Touches NO existing column, removes nothing, no new table.
-- ============================================================
BEGIN;

ALTER TABLE core.weather_reading ADD COLUMN IF NOT EXISTS humidity numeric;  -- %
ALTER TABLE core.weather_reading ADD COLUMN IF NOT EXISTS clouds   numeric;  -- % cover

COMMIT;
