-- 0048_marine_port_craft.sql — UC-I Marine: port-craft fleet register. Additive + idempotent.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0048_marine_port_craft.sql
--
-- The tug/launch fleet register from Details_of_Port_Crafts.pdf, ingested through the
-- SHARED marine upload framework (/api/marine/validate|upload). Structure follows
-- schema.sql §2 core.port_craft verbatim; `name` is the natural key (UNIQUE), so import
-- is idempotent via ON CONFLICT (name) — no row_sha256 needed.
--
-- STRICTLY ADDITIVE. Creates ONE new table in the `core` schema; touches NOTHING in
-- jnpa and NOTHING in the existing core tables (vessel / vessel_call / pilotage / …).
--
-- Two columns beyond schema.sql, both additive and both requested:
--   import_file_id — soft link to core.marine_import_files (provenance; no FK).
--   extras         — jsonb; the raw parsed row + any field the PDF heuristic could not
--                    cleanly split. "Never drop client data": whatever is not promoted
--                    to a typed column is preserved here verbatim.
--
-- ROLLBACK: DROP TABLE core.port_craft;

CREATE SCHEMA IF NOT EXISTS core;

CREATE TABLE IF NOT EXISTS core.port_craft (
    craft_id        smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            text NOT NULL UNIQUE,       -- natural key (upsert target)
    craft_type      text,                        -- Tug / Pilot Launch / Utility / Security / VIP
    owned_or_hired  text,
    owner_name      text,
    year_built      text,                        -- mixed 'Apr-18' / '2020' at source
    loa_m           numeric(6,2),
    breadth_m       numeric(6,2),
    draft_m         numeric(5,2),
    main_engines    text,
    bollard_pull_t  numeric(6,2),
    design_speed_kn numeric(5,2),
    import_file_id  bigint,                       -- provenance (soft link; additive)
    extras          jsonb NOT NULL DEFAULT '{}'::jsonb);  -- raw + unparsed remainder (additive)

CREATE INDEX IF NOT EXISTS idx_port_craft_type ON core.port_craft (craft_type);
CREATE INDEX IF NOT EXISTS idx_port_craft_import_file ON core.port_craft (import_file_id);
