-- ============================================================
-- 0144  core.gs_road_segment — identify a segment by its CONTENT
-- Additive + one index swap. No data is deleted.
--
-- 0134 keyed a segment on (state_id, nh_no, name). That holds for
-- GATISHAKTI/02 (food-storage depots, whose `name` is the depot) but silently
-- destroys GATISHAKTI/01: every fragment of a highway carries the highway
-- itself as `road_name`, so all 186 rows of NH-48 — 135 genuinely distinct
-- ones spanning 8 states and lane statuses from 2L to 8L — collapsed onto a
-- single row whose length and lane count were whichever fragment happened to
-- be written last. That is worse than no data: it reports one arbitrary
-- 600-metre stretch as the attributes of a national highway.
--
-- GATISHAKTI/01 publishes no segment id, no chainage and no coordinates —
-- road_name / road_type / lane_statu / gis_length / state_ut is the entire
-- row. There is therefore no natural key to use, so the row IS its key:
-- `detail_key` fingerprints the upstream item and joins the uniqueness
-- constraint. Byte-identical repeats still dedupe (GATISHAKTI/03 sends every
-- park exactly twice, and /01 repeats fragments up to 14 times), while rows
-- that actually differ each keep their place.
--
-- md5(detail::text) rather than a value computed in Python: jsonb normalises
-- key order on the way in, so the expression is stable, and being GENERATED it
-- backfills every existing row on its own — no separate backfill pass, and no
-- risk of the writer and the constraint disagreeing about the fingerprint.
-- ============================================================
BEGIN;

ALTER TABLE core.gs_road_segment
    ADD COLUMN IF NOT EXISTS detail_key text
        GENERATED ALWAYS AS (md5(detail::text)) STORED;

-- The replacement must exist before the old one goes, so the table is never
-- briefly unconstrained.
CREATE UNIQUE INDEX IF NOT EXISTS uq_gs_road_segment_content
    ON core.gs_road_segment (COALESCE(state_id, ''), COALESCE(nh_no, ''),
                             COALESCE(name, ''), detail_key);

DROP INDEX IF EXISTS core.uq_gs_road_segment;

COMMIT;
