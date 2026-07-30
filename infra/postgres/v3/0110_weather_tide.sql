-- ============================================================
-- 0110  Weather readings — WorldTides tide integration columns
-- Additive only (same pattern as 0106's humidity/clouds columns).
--
-- Extends core.weather_reading with the tide fields persisted whenever the
-- /api/weather tide block is served LIVE from WorldTides. The full tide
-- block (next high/low, station, datum) rides in `payload` -> the DATABASE
-- fallback rung loses nothing; these columns exist for SQL-level analytics
-- (DUKC / pilotage windows) without JSON path digging.
-- Touches NO existing column; ADD COLUMN IF NOT EXISTS is a no-op on a
-- database that already has them.
-- ============================================================
BEGIN;

ALTER TABLE core.weather_reading
    ADD COLUMN IF NOT EXISTS tide_height numeric,   -- metres, datum MSL
    ADD COLUMN IF NOT EXISTS tide_state  text;      -- RISING | FALLING

COMMIT;
