-- 0103  Legacy operational views re-created in mart over core tables.
CREATE OR REPLACE VIEW mart.v_gate_trip_timeline AS
 SELECT gate_event.trip_id,
    max(gate_event.gate_id) AS gate_id,
    max(gate_event.plate) AS plate,
    min(gate_event.ts) FILTER (WHERE gate_event.event_type = 'GATE_ARRIVAL'::text) AS arrival_ts,
    min(gate_event.ts) FILTER (WHERE gate_event.event_type = 'GATE_TXN_START'::text) AS txn_start_ts,
    min(gate_event.ts) FILTER (WHERE gate_event.event_type = 'GATE_IN'::text) AS gate_in_ts,
    min(gate_event.ts) FILTER (WHERE gate_event.event_type = 'GATE_OUT'::text) AS gate_out_ts
   FROM core.gate_event
  WHERE gate_event.ts > (now() - '24:00:00'::interval)
  GROUP BY gate_event.trip_id;
CREATE OR REPLACE VIEW mart.v_alerts_by_kind AS
 SELECT alert.kind,
    alert.severity,
    count(*) AS total,
    count(*) FILTER (WHERE NOT alert.ack) AS open
   FROM core.alert
  WHERE alert.ts > (now() - '24:00:00'::interval)
  GROUP BY alert.kind, alert.severity
  ORDER BY (count(*)) DESC;
CREATE OR REPLACE VIEW mart.v_anpr_hourly AS
 SELECT time_bucket('01:00:00'::interval, anpr_read.ts) AS bucket,
    count(*) AS reads,
    count(*) FILTER (WHERE anpr_read.degraded) AS degraded_reads,
    round(avg(anpr_read.conf)::numeric, 3) AS avg_conf
   FROM core.anpr_read
  WHERE anpr_read.ts > (now() - '24:00:00'::interval)
  GROUP BY (time_bucket('01:00:00'::interval, anpr_read.ts))
  ORDER BY (time_bucket('01:00:00'::interval, anpr_read.ts)) DESC;
CREATE OR REPLACE VIEW mart.v_corridor_speed AS
 SELECT DISTINCT ON (traffic_snapshot.segment_id) traffic_snapshot.segment_id,
    traffic_snapshot.ts,
    traffic_snapshot.speed_kmh,
    traffic_snapshot.jam_factor,
    traffic_snapshot.source
   FROM core.traffic_snapshot
  ORDER BY traffic_snapshot.segment_id, traffic_snapshot.ts DESC;
CREATE OR REPLACE VIEW mart.v_gate_dwell AS
 SELECT time_bucket('00:15:00'::interval, truck_telemetry.ts) AS bucket,
    count(*) FILTER (WHERE truck_telemetry.speed_kmh <= 3::double precision) AS stationary_pings,
    count(*) AS total_pings,
    round(100.0 * count(*) FILTER (WHERE truck_telemetry.speed_kmh <= 3::double precision)::numeric / NULLIF(count(*), 0)::numeric, 1) AS stationary_pct
   FROM core.truck_telemetry
  WHERE truck_telemetry.ts > (now() - '06:00:00'::interval)
  GROUP BY (time_bucket('00:15:00'::interval, truck_telemetry.ts))
  ORDER BY (time_bucket('00:15:00'::interval, truck_telemetry.ts)) DESC;
CREATE OR REPLACE VIEW mart.v_gate_queue_wait AS
 SELECT time_bucket('00:15:00'::interval, v_gate_trip_timeline.txn_start_ts) AS bucket,
    round(avg(EXTRACT(epoch FROM v_gate_trip_timeline.txn_start_ts - v_gate_trip_timeline.arrival_ts)) / 60.0, 2) AS wait_min,
    count(*) AS trips
   FROM mart.v_gate_trip_timeline
  WHERE v_gate_trip_timeline.arrival_ts IS NOT NULL AND v_gate_trip_timeline.txn_start_ts IS NOT NULL AND v_gate_trip_timeline.txn_start_ts >= v_gate_trip_timeline.arrival_ts
  GROUP BY (time_bucket('00:15:00'::interval, v_gate_trip_timeline.txn_start_ts))
  ORDER BY (time_bucket('00:15:00'::interval, v_gate_trip_timeline.txn_start_ts)) DESC;
CREATE OR REPLACE VIEW mart.v_gate_throughput AS
 SELECT time_bucket('01:00:00'::interval, a.ts) AS bucket,
    COALESCE(c.gate_id, 'CORRIDOR'::text) AS gate_id,
    count(*) AS reads,
    count(DISTINCT a.plate) AS unique_plates
   FROM core.anpr_read a
     LEFT JOIN core.camera c ON c.id = a.camera_id
  WHERE a.ts > (now() - '24:00:00'::interval)
  GROUP BY (time_bucket('01:00:00'::interval, a.ts)), (COALESCE(c.gate_id, 'CORRIDOR'::text))
  ORDER BY (time_bucket('01:00:00'::interval, a.ts)) DESC, (COALESCE(c.gate_id, 'CORRIDOR'::text));
CREATE OR REPLACE VIEW mart.v_gate_txn_time AS
 SELECT time_bucket('00:15:00'::interval, v_gate_trip_timeline.gate_in_ts) AS bucket,
    round(avg(EXTRACT(epoch FROM v_gate_trip_timeline.gate_in_ts - v_gate_trip_timeline.txn_start_ts)) / 60.0, 2) AS txn_min,
    count(*) AS trips
   FROM mart.v_gate_trip_timeline
  WHERE v_gate_trip_timeline.txn_start_ts IS NOT NULL AND v_gate_trip_timeline.gate_in_ts IS NOT NULL AND v_gate_trip_timeline.gate_in_ts >= v_gate_trip_timeline.txn_start_ts
  GROUP BY (time_bucket('00:15:00'::interval, v_gate_trip_timeline.gate_in_ts))
  ORDER BY (time_bucket('00:15:00'::interval, v_gate_trip_timeline.gate_in_ts)) DESC;
CREATE OR REPLACE VIEW mart.v_provisional_open AS
 SELECT vehicle_rc.plate,
    vehicle_rc.provisional_until,
    round(EXTRACT(epoch FROM vehicle_rc.provisional_until - now()) / 3600.0, 2) AS hours_remaining,
    vehicle_rc.updated_at
   FROM core.vehicle_rc
  WHERE vehicle_rc.provisional = true AND vehicle_rc.provisional_until IS NOT NULL AND vehicle_rc.provisional_until > now()
  ORDER BY vehicle_rc.provisional_until;
CREATE OR REPLACE VIEW mart.v_tat_inside_port AS
 SELECT time_bucket('00:15:00'::interval, v_gate_trip_timeline.gate_out_ts) AS bucket,
    round(avg(EXTRACT(epoch FROM v_gate_trip_timeline.gate_out_ts - v_gate_trip_timeline.gate_in_ts)) / 60.0, 2) AS tat_min,
    count(*) AS trips
   FROM mart.v_gate_trip_timeline
  WHERE v_gate_trip_timeline.gate_in_ts IS NOT NULL AND v_gate_trip_timeline.gate_out_ts IS NOT NULL AND v_gate_trip_timeline.gate_out_ts >= v_gate_trip_timeline.gate_in_ts
  GROUP BY (time_bucket('00:15:00'::interval, v_gate_trip_timeline.gate_out_ts))
  ORDER BY (time_bucket('00:15:00'::interval, v_gate_trip_timeline.gate_out_ts)) DESC;
CREATE OR REPLACE VIEW mart.v_cfs_ecy_dwell AS
 SELECT m.container_number,
    m.facility_type,
    min(m.event_ts) FILTER (WHERE m.mode = 'IN'::text) AS first_in_ts,
    max(m.event_ts) FILTER (WHERE m.mode = 'OUT'::text) AS last_out_ts,
    count(*) FILTER (WHERE m.mode = 'IN'::text) AS in_events,
    count(*) FILTER (WHERE m.mode = 'OUT'::text) AS out_events,
        CASE
            WHEN m.facility_type = 'CFS'::text AND min(m.event_ts) FILTER (WHERE m.mode = 'IN'::text) IS NOT NULL AND max(m.event_ts) FILTER (WHERE m.mode = 'OUT'::text) IS NOT NULL AND max(m.event_ts) FILTER (WHERE m.mode = 'OUT'::text) >= min(m.event_ts) FILTER (WHERE m.mode = 'IN'::text) THEN round(EXTRACT(epoch FROM max(m.event_ts) FILTER (WHERE m.mode = 'OUT'::text) - min(m.event_ts) FILTER (WHERE m.mode = 'IN'::text)) / 3600.0, 2)
            ELSE NULL::numeric
        END AS dwell_hours
   FROM core.cfs_ecy_movement m
  GROUP BY m.container_number, m.facility_type;
CREATE OR REPLACE VIEW mart.v_customs_container_status AS
 WITH cont AS (
         SELECT igm_line_container.container_no,
            igm_line_container.igm_no
           FROM core.igm_line_container
        UNION
         SELECT oc.container_no,
            o.igm_no
           FROM core.ooc_item oc
             JOIN core.bill_of_entry_ooc o ON o.be_no = oc.be_no
        UNION
         SELECT sl.container_no,
            s.igm_no
           FROM core.smtp_container sl
             JOIN core.smtp_permit s ON s.smtp_no = sl.smtp_no
        UNION
         SELECT rms_scan_container.container_no,
            rms_scan_container.igm_no
           FROM core.rms_scan_container
        )
 SELECT c.container_no,
    max(c.igm_no) AS igm_no,
    (EXISTS ( SELECT 1
           FROM core.igm_line_container ic
          WHERE ic.container_no = c.container_no)) AS declared_igm,
    (EXISTS ( SELECT 1
           FROM core.rms_scan_container rc
          WHERE rc.container_no = c.container_no)) AS rms_selected,
    (EXISTS ( SELECT 1
           FROM core.ooc_item oc
          WHERE oc.container_no = c.container_no)) AS ooc_cleared,
    (EXISTS ( SELECT 1
           FROM core.smtp_container sl
          WHERE sl.container_no = c.container_no)) AS smtp_bonded
   FROM cont c
  GROUP BY c.container_no;
CREATE OR REPLACE VIEW mart.v_shipping_line_container AS
 WITH ac AS (
         SELECT DISTINCT ON (a.container_no) a.container_no,
            CASE a.direction WHEN 'E' THEN 'EAL' ELSE 'IAL' END AS list_type,
            t.code AS terminal,
            a.line_code AS shipping_line_code,
            a.category,
            CASE a.load_status WHEN 'F' THEN 'FULL' WHEN 'E' THEN 'EMPTY'
                 ELSE a.load_status::text END AS freight_kind,
            a.gross_weight_kg,
            a.weight_source_uom,
            a.pol, a.pod, a.destination,
            a.bl_no AS bill_of_lading,
            a.vessel_visit, a.voyage, a.iso_code,
            a.seal1 AS seal_no,
            a.reefer_status, a.reefer_temp, a.id
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
    edo.gate_pass_no, edo.gate_pass_ts, edo.vehicle_no, edo.delivery_mode,
    edo.shipping_agent_code, edo.equipment_status,
    ac.container_no IS NOT NULL AS in_advance_list,
    edo.container_no IS NOT NULL AS has_delivery_order
   FROM ac
     FULL JOIN edo ON edo.container_no = ac.container_no;
