-- ============================================================
-- 0134  GatiShakti reference data — ULIP GATISHAKTI/01..04
-- Additive only. Naming: singular, per architecture conventions.
--
-- Three REFERENCE tables (slow-moving master data, not an event stream) for
-- the /api/gatishakti/* surfaces (services/gatishakti):
--
--   core.gs_toll_plaza    NHAI toll plazas by LGD state code (GATISHAKTI/04).
--                         The registry that gives FASTAG/01's free-text
--                         `tollPlazaName` a canonical geocode, and the only
--                         granted source that enumerates plazas by geography
--                         — so it is also what backs /api/fastag/toll-enroute
--                         (ULIP grants no route-planning API).
--   core.gs_road_segment  national-highway / state road-network rows
--                         (GATISHAKTI/01 by NH number, /02 by state id).
--   core.gs_road_point    named road points carrying lat/lon (GATISHAKTI/03),
--                         the corridor geometry layer.
--
-- All three double as the DATABASE rung of the service's
-- LIVE -> CACHED -> DATABASE -> FALLBACK chain, exactly like
-- core.logistics_event does for /api/logistics.
--
-- Every row keeps the raw upstream item in `detail` so a GatiShakti schema
-- change never silently drops a field we had not modelled yet.
--
-- No existing table is touched.
-- ============================================================
BEGIN;

CREATE TABLE IF NOT EXISTS core.gs_toll_plaza (
    id          bigserial PRIMARY KEY,
    state_id    text NOT NULL,             -- LGD state code, e.g. '27' Maharashtra
    name        text NOT NULL,             -- upstream `vname`
    nh_no       text,                      -- e.g. 'NH-348' (absent on many rows)
    latitude    double precision,
    longitude   double precision,
    source_api  text NOT NULL DEFAULT 'GATISHAKTI/04',
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    created_at  timestamptz NOT NULL DEFAULT now());

-- Re-fetching a state must refresh its plazas, never duplicate them. Name is
-- unique per state in the upstream data; COALESCE keeps the key null-safe.
CREATE UNIQUE INDEX IF NOT EXISTS uq_gs_toll_plaza
    ON core.gs_toll_plaza (state_id, name, COALESCE(nh_no, ''));
CREATE INDEX IF NOT EXISTS idx_gs_toll_plaza_state
    ON core.gs_toll_plaza (state_id, name);

CREATE TABLE IF NOT EXISTS core.gs_road_segment (
    id          bigserial PRIMARY KEY,
    state_id    text,                      -- set when sourced by state (/02)
    nh_no       text,                      -- set when sourced by NH number (/01)
    name        text,
    latitude    double precision,
    longitude   double precision,
    source_api  text NOT NULL DEFAULT 'GATISHAKTI/02',
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    created_at  timestamptz NOT NULL DEFAULT now(),
    -- A segment is keyed by EITHER a state id or an NH number depending on
    -- which API produced it; a row carrying neither is unattributable.
    CONSTRAINT gs_road_segment_key_present
        CHECK (state_id IS NOT NULL OR nh_no IS NOT NULL));

CREATE UNIQUE INDEX IF NOT EXISTS uq_gs_road_segment
    ON core.gs_road_segment (COALESCE(state_id, ''), COALESCE(nh_no, ''),
                             COALESCE(name, ''));
CREATE INDEX IF NOT EXISTS idx_gs_road_segment_state
    ON core.gs_road_segment (state_id);
CREATE INDEX IF NOT EXISTS idx_gs_road_segment_nh
    ON core.gs_road_segment (nh_no);

CREATE TABLE IF NOT EXISTS core.gs_road_point (
    id          bigserial PRIMARY KEY,
    state_id    text NOT NULL,
    name        text,                      -- upstream `vname`
    latitude    double precision,
    longitude   double precision,
    source_api  text NOT NULL DEFAULT 'GATISHAKTI/03',
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    created_at  timestamptz NOT NULL DEFAULT now());

-- Two distinct points can share a name within a state (GatiShakti repeats
-- village names), so the coordinates are part of the identity.
CREATE UNIQUE INDEX IF NOT EXISTS uq_gs_road_point
    ON core.gs_road_point (state_id, COALESCE(name, ''),
                           COALESCE(latitude, 0), COALESCE(longitude, 0));
CREATE INDEX IF NOT EXISTS idx_gs_road_point_state
    ON core.gs_road_point (state_id, name);

COMMIT;
