-- 0047_marine_pilotage_dedup.sql — UC-I Marine: pilotage import idempotency.
-- Additive + idempotent.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0047_marine_pilotage_dedup.sql
--
-- Enables persisting the Pilot_card_data.xlsx sheets (INWARD/OUTWARD/SHIFTING) into the
-- EXISTING core.pilotage table (migration 0042) through the shared marine upload
-- framework, with row-level idempotency. Two additive columns + one unique index on
-- core.pilotage; nothing else changes. Mirrors the sl_advance_containers content-hash
-- pattern: a byte-identical pilotage row collapses on re-import, any differing field
-- persists as its own row.
--
-- STRICTLY ADDITIVE. Only ALTERs core.pilotage (owned by 0042) with new nullable columns
-- + a new index; NEVER drops/renames, touches NOTHING in jnpa, and leaves the vessel_call
-- / VESPRO / CALINF / BERMAN / VESARR / VESDEP path untouched.
--
--   import_file_id — soft link to core.marine_import_files (provenance; no FK, matching
--                    the module's "soft value-links only" convention).
--   row_sha256     — content hash of the normalized pilotage row (computed in the parser);
--                    the dedup key.
--   uq_pilotage_row — UNIQUE (row_sha256): the ON CONFLICT target for idempotent import.
--                    NULLs are distinct, so a manually-inserted row without a hash is never
--                    blocked.
--
-- ROLLBACK:
--   DROP INDEX IF EXISTS core.uq_pilotage_row;
--   ALTER TABLE core.pilotage DROP COLUMN IF EXISTS row_sha256;
--   ALTER TABLE core.pilotage DROP COLUMN IF EXISTS import_file_id;

CREATE SCHEMA IF NOT EXISTS core;

ALTER TABLE core.pilotage ADD COLUMN IF NOT EXISTS import_file_id bigint;
ALTER TABLE core.pilotage ADD COLUMN IF NOT EXISTS row_sha256 text;

CREATE UNIQUE INDEX IF NOT EXISTS uq_pilotage_row ON core.pilotage (row_sha256);
CREATE INDEX IF NOT EXISTS idx_pilotage_import_file ON core.pilotage (import_file_id);
CREATE INDEX IF NOT EXISTS idx_pilotage_movement ON core.pilotage (movement_type);
CREATE INDEX IF NOT EXISTS idx_pilotage_submitted ON core.pilotage (submitted_at DESC);
