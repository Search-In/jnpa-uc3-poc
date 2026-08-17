-- 0140  Unpinned twins of the rolling-window gate KPI views.  GAP-API-04
--
-- The problem
-- -----------
-- `mart.v_gate_trip_timeline` carries `WHERE ts > now() - interval '24 hours'`
-- baked into the view body, and `v_gate_queue_wait`, `v_gate_txn_time` and
-- `v_tat_inside_port` all select FROM it. That is right for a live control-room
-- board and useless for everything else: the corpus gate events are June and
-- July 2026, so any historical trace — the golden thread, a date-filtered
-- report, a what-if replayed over the demo week — reads exactly zero rows and
-- reports "no data" for events that are sitting in the table.
--
-- Why new views rather than editing the existing ones
-- ---------------------------------------------------
-- `jnpa_schema_v3` is shared with five other engineers and the 24-hour views
-- are what their live boards read. Widening those in place would silently
-- change every consumer's result set — a live queue-wait board would start
-- averaging two months of history into its current figure. So the pinned views
-- are left exactly as they are, and these `_all` twins are added beside them.
-- Callers choose: no date window -> the live 24h view, a date window -> the
-- unpinned twin with the window applied (see gateway/routers/kpi.py).
--
-- Purely additive: CREATE OR REPLACE on names that did not previously exist.
-- No ALTER, no DROP, no change to any existing object.

CREATE OR REPLACE VIEW mart.v_gate_trip_timeline_all AS
SELECT trip_id,
       max(gate_id) AS gate_id,
       max(plate)   AS plate,
       min(ts) FILTER (WHERE event_type = 'GATE_ARRIVAL')   AS arrival_ts,
       min(ts) FILTER (WHERE event_type = 'GATE_TXN_START') AS txn_start_ts,
       min(ts) FILTER (WHERE event_type = 'GATE_IN')        AS gate_in_ts,
       min(ts) FILTER (WHERE event_type = 'GATE_OUT')       AS gate_out_ts
FROM core.gate_event
GROUP BY trip_id;

COMMENT ON VIEW mart.v_gate_trip_timeline_all IS
  'Unpinned twin of mart.v_gate_trip_timeline (GAP-API-04). Same shape, no '
  '24-hour cutoff, so a historical date window can be applied by the caller. '
  'The pinned view remains the default for live boards.';

-- The three derived KPIs, rebuilt on the unpinned timeline. The ordering guards
-- are copied verbatim from the pinned definitions so a figure computed over a
-- window is the same figure, just over a different span.
--
-- Bucketing uses the portable to_timestamp(floor(epoch/900)*900) form, NOT
-- `time_bucket`: that is a TimescaleDB function and the extension is not
-- installed on this RDS instance. The repo SQL for the pinned views still says
-- `time_bucket`, but what is actually DEPLOYED there is this same portable
-- expression — verified with pg_get_viewdef on 17-Aug. Written this way, the
-- file applies as-is.

CREATE OR REPLACE VIEW mart.v_gate_queue_wait_all AS
SELECT to_timestamp(floor(EXTRACT(epoch FROM txn_start_ts) / 900) * 900) AS bucket,
       round(avg(EXTRACT(EPOCH FROM (txn_start_ts - arrival_ts)))::numeric / 60.0, 2) AS wait_min,
       count(*) AS trips
FROM mart.v_gate_trip_timeline_all
WHERE arrival_ts IS NOT NULL AND txn_start_ts IS NOT NULL
  AND txn_start_ts >= arrival_ts
GROUP BY 1 ORDER BY 1 DESC;

CREATE OR REPLACE VIEW mart.v_gate_txn_time_all AS
SELECT to_timestamp(floor(EXTRACT(epoch FROM gate_in_ts) / 900) * 900) AS bucket,
       round(avg(EXTRACT(EPOCH FROM (gate_in_ts - txn_start_ts)))::numeric / 60.0, 2) AS txn_min,
       count(*) AS trips
FROM mart.v_gate_trip_timeline_all
WHERE txn_start_ts IS NOT NULL AND gate_in_ts IS NOT NULL
  AND gate_in_ts >= txn_start_ts
GROUP BY 1 ORDER BY 1 DESC;

CREATE OR REPLACE VIEW mart.v_tat_inside_port_all AS
SELECT to_timestamp(floor(EXTRACT(epoch FROM gate_out_ts) / 900) * 900) AS bucket,
       round(avg(EXTRACT(EPOCH FROM (gate_out_ts - gate_in_ts)))::numeric / 60.0, 2) AS tat_min,
       count(*) AS trips
FROM mart.v_gate_trip_timeline_all
WHERE gate_in_ts IS NOT NULL AND gate_out_ts IS NOT NULL
  AND gate_out_ts >= gate_in_ts
GROUP BY 1 ORDER BY 1 DESC;

-- The two other views that bake in the same 24-hour cutoff, for the same reason.
CREATE OR REPLACE VIEW mart.v_alerts_by_kind_all AS
SELECT kind, severity, count(*) AS total,
       count(*) FILTER (WHERE NOT ack) AS open
FROM core.alert
GROUP BY kind, severity
ORDER BY count(*) DESC;

CREATE OR REPLACE VIEW mart.v_anpr_hourly_all AS
SELECT date_trunc('hour', ts) AS bucket,
       count(*) AS reads,
       count(*) FILTER (WHERE degraded) AS degraded_reads,
       round(avg(conf)::numeric, 3) AS avg_conf
FROM core.anpr_read
GROUP BY 1 ORDER BY 1 DESC;
