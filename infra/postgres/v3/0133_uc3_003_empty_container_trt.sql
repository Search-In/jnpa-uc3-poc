-- ============================================================================
-- 0133  UC3-003 — CFS/ECY gate-event ingestion + the empty-container TRT KPI.
--
-- The two CODECO workbooks in Data/13-CFS-ECY (ECY-CODECO.xlsx = 961 rows,
-- CFS-CODECO.xlsx = 968 rows) land in core.container_event, which already
-- carries every column the feed needs (container_no / event_ts / event_type /
-- location_type / direction / source_table / source_file). NOTHING new is
-- modelled for the events themselves — see scripts/import_uc3_003_cfs_ecy.py.
--
-- What was missing before this migration:
--
--   1. The customer's own TRT view, mart.v_ecy_trt, pairs an ECY_IN with the
--      NEXT ECY_OUT of the same container. Against the real corpus it returns
--      ZERO rows, because the ECY feed's OUT block (01–12 Jul, 529 events) and
--      its IN block (12–26 Jul, 432 events) are date-disjoint and share not one
--      container. That is the planted anomaly, and v_ecy_trt is left EXACTLY as
--      the customer wrote it — this migration adds a second, complementary view
--      rather than "repairing" theirs.
--   2. There was no materialisation of the empty-container lifecycle that the
--      corpus DOES support end-to-end: ECY gate-out -> CFS gate-in -> CFS
--      gate-out. mart.v_empty_container_chain derives it per container and
--      mart.v_empty_container_trt scores it, so KPI 3 ("TRT for empty
--      containers from ECD") is computed from real events instead of an
--      optimiser estimate.
--   3. The importer's idempotency probe and the DQ console's filters had no
--      supporting index.
--
-- Fully ADDITIVE and idempotent: two CREATE OR REPLACE VIEWs and three CREATE
-- INDEX IF NOT EXISTS. No table is created, altered or dropped; no row is
-- written; core.container_event, core.dq_issue, mart.v_ecy_trt, and every
-- UC3-001 / UC3-002 object are untouched. Safe to re-run.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/v3/0133_uc3_003_empty_container_trt.sql
-- Runtime-applied at gateway boot by gateway/cfs_ecy_ext.ensure_cfs_ecy_schema().
-- ============================================================================
BEGIN;

CREATE SCHEMA IF NOT EXISTS mart;

-- ------------------------------------------------------------ 1. query paths
-- The importer probes "how many rows of MY source_table already exist for this
-- (container, ts, type)" once per distinct source event; without this index that
-- is a sequential scan per probe. Also serves the /api/cfs-ecy/events feed's
-- source filter.
CREATE INDEX IF NOT EXISTS idx_container_event_source_table
    ON core.container_event (source_table, event_ts DESC);

-- Event-type + time is the access path for the CODECO leg aggregation below and
-- for the events API's type filter; the shipped indexes cover container and
-- location_type only.
CREATE INDEX IF NOT EXISTS idx_container_event_type_ts
    ON core.container_event (event_type, event_ts DESC);

-- The Data Quality console lists by source table and groups by issue type.
CREATE INDEX IF NOT EXISTS idx_dq_issue_source_type
    ON core.dq_issue (source_table, issue_type);

-- ------------------------------------------------- 2. empty-container chain
-- One row per container seen in the CODECO feeds, with the four legs the feeds
-- can carry. Aggregation only — no filtering, no repair: a container with a
-- single orphan leg is present here exactly as the source left it, and the
-- per-leg event COUNTS are exposed so a duplicated source row (the corpus's
-- COSU4663595 CFS gate-in, recorded twice at the same minute) stays visible
-- instead of being flattened away by min()/max().
--
-- Scoped to the CODECO event vocabulary so terminal/scanner/gate-document
-- events written by other modules can never leak into the empty-container KPI.
CREATE OR REPLACE VIEW mart.v_empty_container_chain AS
SELECT
    e.container_no,
    min(e.event_ts) FILTER (WHERE e.event_type = 'ECY_OUT') AS ecy_out_ts,
    min(e.event_ts) FILTER (WHERE e.event_type = 'ECY_IN')  AS ecy_in_ts,
    min(e.event_ts) FILTER (WHERE e.event_type = 'CFS_IN')  AS cfs_in_ts,
    max(e.event_ts) FILTER (WHERE e.event_type = 'CFS_OUT') AS cfs_out_ts,
    -- earliest CFS gate-out, so an OUT that precedes the first IN is detectable
    min(e.event_ts) FILTER (WHERE e.event_type = 'CFS_OUT') AS cfs_first_out_ts,
    count(*) FILTER (WHERE e.event_type = 'ECY_OUT')::int   AS ecy_out_events,
    count(*) FILTER (WHERE e.event_type = 'ECY_IN')::int    AS ecy_in_events,
    count(*) FILTER (WHERE e.event_type = 'CFS_IN')::int    AS cfs_in_events,
    count(*) FILTER (WHERE e.event_type = 'CFS_OUT')::int   AS cfs_out_events,
    count(*)::int                                           AS event_count,
    min(e.event_ts)                                         AS first_event_ts,
    max(e.event_ts)                                         AS last_event_ts
FROM core.container_event e
WHERE e.location_type IN ('ECY', 'CFS')
  AND e.event_type IN ('ECY_OUT', 'ECY_IN', 'CFS_IN', 'CFS_OUT')
GROUP BY e.container_no;

COMMENT ON VIEW mart.v_empty_container_chain IS
    'Per-container roll-up of the CFS/ECY CODECO legs held in '
    'core.container_event (UC3-003). Aggregation only: no row is filtered, '
    'reordered or repaired, and per-leg event counts keep source duplicates '
    'visible.';

-- ------------------------------------------------------- 3. TRT scoring view
-- Scores each chain and computes the KPI-3 durations IN MINUTES (the tender's
-- unit for "TRT for empty containers from ECD"; the UI switches to hours when a
-- value is large).
--
--   trt_min    ECY gate-out -> CFS gate-in.  THE headline KPI. This is the
--              existing project definition verbatim — jnpa_shared.kpi
--              .trt_empty_ecd_min() is documented as "ECD pickup to gate-in" —
--              applied to real events instead of the optimiser's estimate.
--   dwell_min  CFS gate-in  -> CFS gate-out. Supporting metric.
--   cycle_min  ECY gate-out -> CFS gate-out. Supporting metric (full chain).
--
-- A duration is NULL unless BOTH its endpoints exist AND are correctly ordered,
-- so an out-of-order source pair can never contribute a negative sample. Only
-- chain_status = 'COMPLETE' rows feed the KPI aggregate.
--
-- chain_status / anomaly_codes reuse the vocabulary of core.ecy_cfs_chain
-- (migration 0114) so the two lifecycle surfaces read the same.
CREATE OR REPLACE VIEW mart.v_empty_container_trt AS
SELECT
    c.*,
    ((c.ecy_out_ts IS NOT NULL)::int + (c.cfs_in_ts IS NOT NULL)::int
     + (c.cfs_out_ts IS NOT NULL)::int) AS legs_present,
    CASE
        WHEN c.ecy_out_ts IS NOT NULL AND c.cfs_in_ts IS NOT NULL
             AND c.cfs_out_ts IS NOT NULL
             AND c.cfs_in_ts >= c.ecy_out_ts AND c.cfs_out_ts >= c.cfs_in_ts
        THEN 'COMPLETE'
        WHEN c.ecy_out_ts IS NULL AND c.cfs_in_ts IS NULL AND c.cfs_out_ts IS NULL
        THEN 'ORPHAN'
        ELSE 'PARTIAL'
    END AS chain_status,
    CASE WHEN c.ecy_out_ts IS NOT NULL AND c.cfs_in_ts IS NOT NULL
              AND c.cfs_in_ts >= c.ecy_out_ts
         THEN round(extract(epoch FROM (c.cfs_in_ts - c.ecy_out_ts)) / 60.0::numeric, 2)
    END AS trt_min,
    CASE WHEN c.cfs_in_ts IS NOT NULL AND c.cfs_out_ts IS NOT NULL
              AND c.cfs_out_ts >= c.cfs_in_ts
         THEN round(extract(epoch FROM (c.cfs_out_ts - c.cfs_in_ts)) / 60.0::numeric, 2)
    END AS dwell_min,
    CASE WHEN c.ecy_out_ts IS NOT NULL AND c.cfs_out_ts IS NOT NULL
              AND c.cfs_out_ts >= c.ecy_out_ts
         THEN round(extract(epoch FROM (c.cfs_out_ts - c.ecy_out_ts)) / 60.0::numeric, 2)
    END AS cycle_min,
    ARRAY_REMOVE(ARRAY[
        CASE WHEN c.cfs_in_events  > 1 THEN 'DUPLICATE_IN' END,
        CASE WHEN c.cfs_out_events > 1 THEN 'MULTI_OUT' END,
        CASE WHEN c.cfs_first_out_ts IS NOT NULL AND c.cfs_in_ts IS NOT NULL
                  AND c.cfs_first_out_ts < c.cfs_in_ts THEN 'OUT_BEFORE_IN' END,
        CASE WHEN c.ecy_out_ts IS NULL AND c.cfs_in_ts IS NOT NULL
             THEN 'ORPHAN_CFS_IN' END,
        CASE WHEN c.ecy_out_ts IS NOT NULL AND c.cfs_in_ts IS NULL
             THEN 'NO_CFS_IN' END,
        CASE WHEN c.ecy_in_ts IS NOT NULL AND c.ecy_out_ts IS NULL
             THEN 'ECY_IN_WITHOUT_ECY_OUT' END
    ], NULL) AS anomaly_codes
FROM mart.v_empty_container_chain c;

COMMENT ON VIEW mart.v_empty_container_trt IS
    'UC3-003 KPI 3 — TRT for empty containers from ECD. trt_min is the '
    'ECY-gate-out -> CFS-gate-in leg (jnpa_shared.kpi.trt_empty_ecd_min: "ECD '
    'pickup to gate-in"); only chain_status = COMPLETE rows are eligible. '
    'Complements, and does not replace, the customer''s mart.v_ecy_trt, which '
    'returns no rows because the ECY OUT and IN blocks are date-disjoint.';

COMMIT;
