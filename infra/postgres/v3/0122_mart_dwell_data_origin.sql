-- ============================================================
-- 0122  mart.v_cfs_ecy_dwell — expose data_origin (fix-forward for 0103 drift)
--
-- 0121 added data_origin to the core.* base tables, but mart.v_cfs_ecy_dwell
-- selects an explicit column list and aggregates with GROUP BY, so it never
-- inherited the new column. /api/cfs-ecy/stats and /api/cfs-ecy/dwell filter
-- `AND d.data_origin = :mode` against this view and were returning 500
-- (UndefinedColumnError: column d.data_origin does not exist).
--
-- 0103_mart_views.sql was ALREADY edited to carry m.data_origin in both the
-- select list and the GROUP BY, but 0103 had long since been applied and
-- migrations run once — so the live view kept the pre-edit definition. That
-- edit is the source of the 0103 checksum drift the runner reports. This
-- migration re-applies that same corrected definition as a new numbered step,
-- which is the fix-forward the runner asks for; it does NOT clear the 0103
-- drift warning (the ledger checksum for 0103 stays as-applied).
--
-- DROP + CREATE rather than CREATE OR REPLACE: the corrected definition places
-- data_origin third, and CREATE OR REPLACE VIEW may only append columns at the
-- end, never reorder or rename existing ones. Verified nothing depends on this
-- view (no pg_depend/pg_rewrite dependents), so the drop is non-cascading and
-- loses nothing. Views hold no data.
--
-- Grouping now includes data_origin, so a container present in both the API and
-- the MANUAL corpus yields one dwell row per origin — which is what the
-- per-origin LIVE/DEMO split in 0120 intends. With the current corpus (all rows
-- MANUAL) the row count is unchanged.
-- ============================================================
BEGIN;

DROP VIEW IF EXISTS mart.v_cfs_ecy_dwell;

CREATE VIEW mart.v_cfs_ecy_dwell AS
 SELECT m.container_number,
    m.facility_type,
    m.data_origin,
    min(m.event_ts) FILTER (WHERE m.mode = 'IN'::text) AS first_in_ts,
    max(m.event_ts) FILTER (WHERE m.mode = 'OUT'::text) AS last_out_ts,
    count(*) FILTER (WHERE m.mode = 'IN'::text) AS in_events,
    count(*) FILTER (WHERE m.mode = 'OUT'::text) AS out_events,
        CASE
            WHEN m.facility_type = 'CFS'::text AND min(m.event_ts) FILTER (WHERE m.mode = 'IN'::text) IS NOT NULL AND max(m.event_ts) FILTER (WHERE m.mode = 'OUT'::text) IS NOT NULL AND max(m.event_ts) FILTER (WHERE m.mode = 'OUT'::text) >= min(m.event_ts) FILTER (WHERE m.mode = 'IN'::text) THEN round(EXTRACT(epoch FROM max(m.event_ts) FILTER (WHERE m.mode = 'OUT'::text) - min(m.event_ts) FILTER (WHERE m.mode = 'IN'::text)) / 3600.0, 2)
            ELSE NULL::numeric
        END AS dwell_hours
   FROM core.cfs_ecy_movement m
  GROUP BY m.container_number, m.facility_type, m.data_origin;

COMMIT;
