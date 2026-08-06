-- ============================================================
-- 0127  mart data_origin views — complete the 0103-drift fix-forward
--
-- The post-apply edit of 0103_mart_views.sql (commit bff895a) added
-- data_origin to TWO views, but 0122 fix-forwarded only mart.v_cfs_ecy_dwell.
-- This migration finishes the job:
--
--   * mart.v_shipping_line_container — gains ac.data_origin, so LIVE/DEMO
--     narrowing can filter API-ingested advance-list rows from MANUAL ones.
--   * mart.v_cfs_ecy_dwell — re-asserted byte-identical to 0122. Redundant on
--     a database where 0122 truly executed; converges one where 0122 was only
--     RECORDED (the live RDS ledger was adopted by baseline, so execution is
--     not guaranteed for every recorded version). Views hold no data, so the
--     re-assertion is free.
--
-- 0103_mart_views.sql itself is reverted to the exact bytes the ledger
-- recorded (checksum 2419e6bb6aec80e3), which clears the drift ABORT; the
-- corrected definitions live here and in 0122, per the runner's
-- fix-forward doctrine.
--
-- DROP + CREATE rather than CREATE OR REPLACE: data_origin lands mid-list,
-- and CREATE OR REPLACE VIEW may only append columns at the end. Nothing
-- depends on either view (no pg_depend/pg_rewrite dependents).
-- ============================================================
BEGIN;

DROP VIEW IF EXISTS mart.v_shipping_line_container;

CREATE VIEW mart.v_shipping_line_container AS
 WITH ac AS (
         SELECT DISTINCT ON (a.container_no) a.container_no,
            CASE a.direction WHEN 'E' THEN 'EAL' ELSE 'IAL' END AS list_type,
            t.code AS terminal,
            a.line_code AS shipping_line_code,
            a.category,
            CASE a.load_status WHEN 'F' THEN 'FULL' WHEN 'E' THEN 'EMPTY'
                 ELSE a.load_status::text END AS freight_kind,
            a.gross_weight_kg,
            'KG'::text AS weight_source_uom,
            a.pol, a.pod, a.destination,
            a.bl_no AS bill_of_lading,
            a.vessel_visit, a.voyage, a.iso_code,
            a.seal1 AS seal_no,
            a.reefer_status, a.reefer_temp, a.data_origin, a.id
           FROM core.advance_list_container a
           LEFT JOIN core.ref_terminal t ON t.terminal_id = a.terminal_id
          ORDER BY a.container_no, a.id DESC
        ), edo AS (
         SELECT DISTINCT ON (l.container_no) l.container_no,
            l.gate_pass_no, l.gate_pass_ts, l.vehicle_no, l.delivery_mode,
            d.shipping_agent_code, l.equipment_status,
            l.pol AS loading_port, l.pod AS dest_port, NULL::text AS final_pod,
            l.id
           FROM core.delivery_order_line l
           LEFT JOIN core.delivery_order d ON d.do_number = l.do_number
          ORDER BY l.container_no, l.id DESC
        )
 SELECT COALESCE(ac.container_no, edo.container_no) AS container_no,
    ac.list_type, ac.terminal, ac.shipping_line_code, ac.category,
    ac.freight_kind, ac.gross_weight_kg, ac.weight_source_uom,
    ac.pol, ac.pod, ac.destination, ac.bill_of_lading, ac.vessel_visit,
    ac.voyage, ac.iso_code, ac.seal_no, ac.reefer_status, ac.reefer_temp,
    ac.data_origin,
    edo.gate_pass_no, edo.gate_pass_ts, edo.vehicle_no, edo.delivery_mode,
    edo.shipping_agent_code, edo.equipment_status,
    ac.container_no IS NOT NULL AS in_advance_list,
    edo.container_no IS NOT NULL AS has_delivery_order
   FROM ac
     FULL JOIN edo ON edo.container_no = ac.container_no;

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
