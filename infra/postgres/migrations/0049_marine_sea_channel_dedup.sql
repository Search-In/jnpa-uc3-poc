-- 0049_marine_sea_channel_dedup.sql — UC-I Marine: sea-channel import idempotency.
-- Additive + idempotent.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0049_marine_sea_channel_dedup.sql
--
-- Enables persisting the JNPA_Sea_Channels ESRI shapefile (50 polygons, reprojected
-- UTM 43N → WGS84 at parse time) into the EXISTING core.sea_channel table (migration
-- 0043) through the shared marine upload framework, with row-level idempotency. Two
-- additive columns + one unique index on core.sea_channel; nothing else changes.
--
-- STRICTLY ADDITIVE. Only ALTERs core.sea_channel (owned by 0043) with new nullable
-- columns + a new index; NEVER drops/renames, touches NOTHING in jnpa, and leaves the
-- vessel_call / pilotage / port_craft / VESPRO / CALINF paths untouched. `name` is NOT
-- unique on core.sea_channel (many channels share a name, e.g. 29× 'MbPA Channel'), so
-- a content hash is the dedup key.
--
--   import_file_id — soft link to core.marine_import_files (provenance; no FK).
--   row_sha256     — content hash of the normalized channel row (name + section_label +
--                    area + length + geometry), computed in the parser; the dedup key.
--   uq_sea_channel_row — UNIQUE (row_sha256): the ON CONFLICT target for idempotent
--                    import. NULLs are distinct, so a hand-inserted row is never blocked.
--
-- ROLLBACK:
--   DROP INDEX IF EXISTS core.uq_sea_channel_row;
--   ALTER TABLE core.sea_channel DROP COLUMN IF EXISTS row_sha256;
--   ALTER TABLE core.sea_channel DROP COLUMN IF EXISTS import_file_id;

CREATE SCHEMA IF NOT EXISTS core;

ALTER TABLE core.sea_channel ADD COLUMN IF NOT EXISTS import_file_id bigint;
ALTER TABLE core.sea_channel ADD COLUMN IF NOT EXISTS row_sha256 text;

CREATE UNIQUE INDEX IF NOT EXISTS uq_sea_channel_row ON core.sea_channel (row_sha256);
CREATE INDEX IF NOT EXISTS idx_sea_channel_import_file ON core.sea_channel (import_file_id);
