-- 0043_marine_geo.sql — UC-I Marine: sea-channel GIS overlay. Additive + idempotent.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0043_marine_geo.sql
--
-- core.sea_channel — the 50 channel / anchorage / berth-pocket polygons from
-- JNPA_Sea_Channels.shp, used as the DUKC / tidal-window static overlay (schema.sql §10).
--
-- STRICTLY ADDITIVE. Creates ONLY new core.* objects; touches NOTHING in jnpa.
--
-- SOURCE OF TRUTH: schema.sql §10 (core.sea_channel). Columns, types verbatim.
--
-- GEOMETRY / NO PostGIS: geometry ships as a GeoJSON `jsonb` column (geom_geojson),
-- exactly as schema.sql specifies — PostGIS is a commented upgrade path there, and this
-- backend runs TimescaleDB + pgcrypto only (no PostGIS; the jnpa.geofence_zones.polygon
-- jsonb precedent). The shapefile is reprojected UTM Zone 43N (EPSG:32643) -> WGS84
-- (EPSG:4326) OFFLINE before load; there is no runtime SHP parser and no runtime
-- reprojection. bathymetry_survey (the schema.sql §10 companion) is deferred to a later
-- slice — its per-reach design depths are entered manually (soundings are not
-- machine-extractable from the survey PDFs).
--
-- Deviations: [D1] core schema; [D9] smallint IDENTITY PK, per schema.sql.
-- numeric(14,4) verbatim.
--
-- ROLLBACK: DROP TABLE core.sea_channel;

CREATE SCHEMA IF NOT EXISTS core;

CREATE TABLE IF NOT EXISTS core.sea_channel (
    channel_id    smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          text NOT NULL,              -- 'JNPA Channel','MbPA Channel','Emergency Anchorage'
    section_label text,                        -- 'Channel Section E-F, JNPA Channel'
    area_ha       numeric(14,4),
    length_m      numeric(14,4),
    geom_geojson  jsonb);                       -- GeoJSON geometry, EPSG:4326 (reprojected offline)

CREATE INDEX IF NOT EXISTS idx_sea_channel_name ON core.sea_channel (name);
