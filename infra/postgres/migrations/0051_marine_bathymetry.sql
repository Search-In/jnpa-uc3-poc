-- 0051_marine_bathymetry.sql — UC-I Marine: bathymetry depth soundings. Additive + idempotent.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0051_marine_bathymetry.sql
--
-- Depth soundings extracted from the JNPA multibeam bathymetry chart PDFs, one row per
-- plotted sounding. Earlier drafts assumed core.bathymetry_survey already existed in an
-- external schema.sql; on a PoC DB that never received that dump the sounding CREATE
-- (FK) failed and gateway marine schema bootstrap rolled back entirely. This migration
-- therefore creates BOTH the survey header and the sounding detail table.
--
-- STRICTLY ADDITIVE. Creates TWO new tables in the `core` schema. Touches NOTHING in
-- any existing core table (vessel / vessel_call / pilotage / port_craft / sea_channel / …).
--
-- Column notes:
--   sounding_id   — bigint (NOT smallint like survey_id): one chart yields ~17k rows and
--                   the existing corpus alone is 189,590. smallint would overflow at 32k.
--   survey_id     — REAL foreign key to core.bathymetry_survey. The survey table has no
--                   inbound FKs today; this is the first, and it is what makes a sounding
--                   attributable to a dated chart.
--   easting/northing/lat/lon — NULLABLE BY DESIGN. 3 of the 11 surveys in the existing
--                   corpus (56,627 soundings, 29.9%) carry NO georeferencing at all: the
--                   original parser could not fit a page->UTM affine for those charts, so
--                   only the page-space position is known. Depth data is still valid and
--                   must not be discarded — "never drop client data".
--   page_x_pt/y   — pdfplumber page coordinates; the only position present for EVERY
--                   sounding, and the fallback locator for an ungeoreferenced chart.
--   above_design  — the sounding was plotted red (shoal above design depth) on the chart.
--   row_sha256    — dedup key. A sounding has NO natural key, so idempotent re-import
--                   follows the core.sea_channel precedent (ON CONFLICT DO NOTHING on a
--                   content hash) rather than the port_craft natural-key upsert. Without
--                   it, re-uploading one chart would duplicate ~17k rows.
--   import_file_id— provenance soft link to core.marine_import_files (no FK, matching
--                   core.port_craft / core.sea_channel).
--
-- ROLLBACK: DROP TABLE core.bathymetry_sounding; DROP TABLE core.bathymetry_survey;

CREATE SCHEMA IF NOT EXISTS core;

CREATE TABLE IF NOT EXISTS core.bathymetry_survey (
    survey_id      smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    drawing_no     text NOT NULL UNIQUE,
    section_label  text,
    design_depth_m numeric(5,2),
    survey_start   text,
    survey_end     text,
    survey_vessel  text,
    file_path      text,
    created_at     timestamptz NOT NULL DEFAULT now());

CREATE INDEX IF NOT EXISTS idx_bathy_survey_drawing
    ON core.bathymetry_survey (drawing_no);
CREATE INDEX IF NOT EXISTS idx_bathy_survey_section
    ON core.bathymetry_survey (section_label);

CREATE TABLE IF NOT EXISTS core.bathymetry_sounding (
    sounding_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    survey_id      smallint NOT NULL
                       REFERENCES core.bathymetry_survey (survey_id) ON DELETE CASCADE,
    easting_m      numeric(10,2),           -- UTM Zone 43N (EPSG:32643); NULL if ungeoreferenced
    northing_m     numeric(11,2),
    lat            numeric(9,6),            -- WGS84, derived from easting/northing at parse time
    lon            numeric(9,6),
    depth_m        numeric(5,2) NOT NULL,   -- metres below Chart Datum
    above_design   boolean NOT NULL DEFAULT false,
    page_x_pt      numeric(8,2),            -- pdfplumber page coords (always present)
    page_y_pt      numeric(8,2),
    import_file_id bigint,                  -- provenance (soft link; additive)
    row_sha256     text,                    -- dedup key (see header)
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_bathymetry_sounding_row UNIQUE (row_sha256));

-- Per-survey read + the survey->soundings join.
CREATE INDEX IF NOT EXISTS idx_bathy_sounding_survey
    ON core.bathymetry_sounding (survey_id);
-- Bounding-box reads (controlling depth over a channel segment) without PostGIS: a plain
-- composite btree serves the range scan the depth queries need.
CREATE INDEX IF NOT EXISTS idx_bathy_sounding_bbox
    ON core.bathymetry_sounding (survey_id, easting_m, northing_m);
-- Shoal/controlling-depth lookups (MIN(depth_m) per survey, above-design filters).
CREATE INDEX IF NOT EXISTS idx_bathy_sounding_depth
    ON core.bathymetry_sounding (survey_id, depth_m);
CREATE INDEX IF NOT EXISTS idx_bathy_sounding_import_file
    ON core.bathymetry_sounding (import_file_id);

-- The canonical bathymetry JSON arrives as a NEW physical format, and the ledger's
-- physical_format CHECK is a closed vocabulary (0050 last widened it for ZIP/SHP). Without
-- this, a JSON upload would parse correctly and then abort the whole import transaction on
-- the core.marine_import_files insert. Guarded + idempotent, mirroring 0050 exactly.
DO $$
DECLARE v_conname text;
BEGIN
    SELECT c.conname INTO v_conname
    FROM pg_constraint c
    WHERE c.conrelid = 'core.marine_import_files'::regclass
      AND c.contype = 'c'
      AND pg_get_constraintdef(c.oid) ILIKE '%physical_format%'
      AND pg_get_constraintdef(c.oid) NOT ILIKE '%JSON%';
    IF v_conname IS NOT NULL THEN
        EXECUTE format('ALTER TABLE core.marine_import_files DROP CONSTRAINT %I', v_conname);
        ALTER TABLE core.marine_import_files
            ADD CONSTRAINT marine_import_files_physical_format_check
            CHECK (physical_format IN ('CSV','XLS','XLSX','PDF','XML','LOG','ZIP','SHP','JSON'));
    END IF;
END $$;

COMMENT ON TABLE core.bathymetry_sounding IS
    'Depth soundings from the JNPA multibeam bathymetry chart PDFs. depth_m is metres '
    'below Chart Datum; above_design marks a sounding plotted red (shoal above design '
    'depth). easting/northing are UTM WGS84 43N and are NULL for charts whose page->UTM '
    'affine could not be fitted — page_x_pt/page_y_pt are then the only position.';
