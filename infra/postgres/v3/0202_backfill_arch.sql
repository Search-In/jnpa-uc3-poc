-- ============================================================================
-- 0202  Transform backfill: jnpa arch-mapped tables -> architecture tables.
-- Seeded rows are never modified except to fill NULL extension columns.
-- Every dropped/unmappable row is logged to core.dq_issue.
-- ============================================================================

-- Pre-pass: push sequence-default ext ids out of the legacy id range so the
-- legacy-id remap below can never collide. Idempotent.
UPDATE core.transporter SET id = id + 100000000 WHERE id < 100000000;
UPDATE core.driver SET id = id + 100000000 WHERE id < 100000000;
UPDATE core.igm SET id = id + 100000000 WHERE id < 100000000;
UPDATE core.igm_line SET id = id + 100000000 WHERE id < 100000000;
UPDATE core.igm_line_container SET id = id + 100000000 WHERE id < 100000000;
UPDATE core.bill_of_entry_ooc SET id = id + 100000000 WHERE id < 100000000;
UPDATE core.ooc_item SET id = id + 100000000 WHERE id < 100000000;
UPDATE core.smtp_permit SET id = id + 100000000 WHERE id < 100000000;
UPDATE core.smtp_container SET id = id + 100000000 WHERE id < 100000000;
UPDATE core.leo SET id = id + 100000000 WHERE id < 100000000;
UPDATE core.shipping_bill SET id = id + 100000000 WHERE id < 100000000;
UPDATE core.rms_scan_report SET id = id + 100000000 WHERE id < 100000000;
UPDATE core.rms_scan_container SET id = id + 100000000 WHERE id < 100000000;
UPDATE core.advance_list_container SET id = id + 100000000 WHERE id < 100000000;
UPDATE core.delivery_order SET id = id + 100000000 WHERE id < 100000000;
UPDATE core.delivery_order_line SET id = id + 100000000 WHERE id < 100000000;

-- --------------------------- transporter -----------------------------------
-- 1) enrich seeded rows matched on company_id
UPDATE core.transporter t SET
    id = j.id, code = j.code, gstin = j.gstin, contact = j.contact,
    status = coalesce(j.status,'ACTIVE'),
    created_at = coalesce(j.created_at, t.created_at),
    updated_at = coalesce(j.updated_at, t.updated_at)
FROM jnpa.transporters j
WHERE j.source_company_id = t.company_id;

-- 2) rows unknown to the architecture load (API-created; no source_company_id match)
INSERT INTO core.transporter (company_name, email, contact_person, address,
                              mobile_number, designation, document_type,
                              document_file, user_id,
                              id, code, gstin, contact, status, created_at, updated_at)
SELECT j.name, j.email, j.contact_person, j.address, j.mobile, j.designation,
       j.doc_type, j.doc_file, j.source_user_id,
       j.id, j.code, j.gstin, j.contact, coalesce(j.status,'ACTIVE'),
       coalesce(j.created_at, now()), coalesce(j.updated_at, now())
FROM jnpa.transporters j
WHERE NOT EXISTS (SELECT 1 FROM core.transporter t WHERE t.id = j.id);

SELECT setval('core.transporter_id_seq',
              coalesce((SELECT max(id) FROM core.transporter),0)+1, false);
SELECT setval('core.transporter_company_id_seq',
              coalesce((SELECT max(company_id) FROM core.transporter),0)+1, false);

-- --------------------------- driver ----------------------------------------
-- 1) enrich seeded rows matched on normalized licence (keep first master row per licence)
UPDATE core.driver d SET
    id = j.id, transporter_id = j.transporter_id, status = j.status,
    photo_url = j.photo_url, enrolled_driver_id = j.enrolled_driver_id,
    licence_valid_to = j.licence_valid_to, source_srno = j.source_srno,
    created_at = j.created_at, updated_at = j.updated_at
FROM (SELECT DISTINCT ON (licence_no_norm) *
      FROM jnpa.driver_master ORDER BY licence_no_norm, id) j
WHERE d.licence_no_norm = j.licence_no_norm
  AND d.licence_no_norm <> ''
  AND d.id >= 100000000
  AND d.driver_id = (SELECT min(d2.driver_id) FROM core.driver d2
                     WHERE d2.licence_no_norm = d.licence_no_norm);

-- 2) master rows with no seeded counterpart
INSERT INTO core.driver (driver_name, licence_number, licence_type,
                         date_of_birth, photo_file, company_name,
                         latest_pdp_number,
                         id, transporter_id, status, photo_url,
                         enrolled_driver_id, licence_valid_to, source_srno,
                         created_at, updated_at)
SELECT j.name, j.licence_no, j.licence_type, j.dob, j.photo_file,
       j.company_name, j.latest_pdp_number,
       j.id, j.transporter_id, j.status, j.photo_url, j.enrolled_driver_id,
       j.licence_valid_to, j.source_srno, j.created_at, j.updated_at
FROM jnpa.driver_master j
WHERE NOT EXISTS (SELECT 1 FROM core.driver d WHERE d.id = j.id);

SELECT setval('core.driver_id_seq',
              coalesce((SELECT max(id) FROM core.driver),0)+1, false);

INSERT INTO core.dq_issue (source_table, record_ref, issue_type, severity, description)
SELECT 'core.driver', j.licence_no_norm, 'duplicate', 'warn',
       'driver_master licence duplicated; first row kept as enrichment source'
FROM (SELECT licence_no_norm FROM jnpa.driver_master
      GROUP BY licence_no_norm HAVING count(*) > 1) j;

-- --------------------------- pdp -------------------------------------------
UPDATE core.pdp p SET
    cancellation_time = j.cancellation_time, created_at = j.created_at
FROM jnpa.driver_pdp_history j
WHERE p.pdp_id = j.pdp_id;

INSERT INTO core.pdp (pdp_id, appl_number, pdp_number, accepted_at, active,
                      valid_until, remarks, cancelled_by, cancellation_date,
                      cancellation_time, created_at)
SELECT j.pdp_id, j.appl_number, j.pdp_number, j.acceptance_time_stamp, j.active,
       j.validity, j.remarks, j.pdp_cancelled_by, j.cancellation_time::date,
       j.cancellation_time, j.created_at
FROM jnpa.driver_pdp_history j
WHERE NOT EXISTS (SELECT 1 FROM core.pdp p WHERE p.pdp_id = j.pdp_id);

-- --------------------------- vehicle ---------------------------------------
UPDATE core.vehicle v SET
    id = j.id, vehicle_id = j.vehicle_id, vehicle_type = j.vehicle_type,
    chassis_number = j.chassis_number, rfid_fastag_id = j.rfid_fastag_id,
    status = coalesce(j.status,'ACTIVE'), created_by = j.created_by,
    created_at = j.created_at, updated_at = j.updated_at
FROM jnpa.fleet_vehicles j
WHERE upper(replace(j.vehicle_number,' ','')) = upper(replace(v.vehicle_no,' ',''));

INSERT INTO core.vehicle (vehicle_no, first_seen, last_seen,
                          id, vehicle_id, vehicle_type, chassis_number,
                          rfid_fastag_id, status, created_by, created_at, updated_at)
SELECT j.vehicle_number, j.created_at, j.updated_at,
       j.id, j.vehicle_id, j.vehicle_type, j.chassis_number, j.rfid_fastag_id,
       coalesce(j.status,'ACTIVE'), j.created_by, j.created_at, j.updated_at
FROM jnpa.fleet_vehicles j
WHERE NOT EXISTS (SELECT 1 FROM core.vehicle v WHERE v.id = j.id);



-- --------------------------- customs: IGM ----------------------------------
INSERT INTO core.igm (igm_no, igm_date, customs_house, imo_no, vessel_code,
                      voyage_no, line_code, shipping_agent, master_name,
                      port_of_arrival, vessel_type, declared_lines, cargo_brief,
                      eta, entry_inward_ts, terminal_code,
                      id, message_id, created_at)
SELECT j.igm_no::bigint, j.igm_date, j.customs_house_code, j.imo_code, j.vessel_code,
       j.voyage_no, j.shipping_line_code, j.shipping_agent_code, j.master_name,
       j.port_of_arrival, j.vessel_type, j.total_no_of_lines, j.brief_cargo_desc,
       j.expected_arrival, j.entry_inward, j.terminal_operator_code,
       j.id, j.message_id, j.created_at
FROM jnpa.customs_igm_vessel j
WHERE j.igm_no IS NOT NULL
ON CONFLICT (igm_no) DO NOTHING;

INSERT INTO core.igm_line (igm_no, line_no, subline_no, bl_no, bl_date,
                           house_bl_no, house_bl_date, pol, pod,
                           port_of_discharge, importer_name, importer_addr,
                           notify_party, nature_of_cargo, item_type,
                           cargo_movement, packages, package_type, gross_weight,
                           weight_unit, goods_desc, mlo_code,
                           id, created_at, importer_state, be_regularised)
SELECT j.igm_no::bigint, j.line_no, coalesce(j.subline_no,0), j.bl_no, j.bl_date,
       j.house_bl_no, j.house_bl_date, j.port_of_loading, j.port_of_destination,
       j.port_of_discharge, j.importer_name, j.importer_address,
       j.notified_party, j.nature_of_cargo, j.item_type, j.cargo_movement,
       j.no_of_packages, j.type_of_packages, j.gross_weight, j.unit_of_weight,
       j.goods_description, j.mlo_code,
       j.id, j.created_at, j.importer_state, j.be_regularised
FROM jnpa.customs_igm_cargo_line j
WHERE j.igm_no IS NOT NULL AND j.line_no IS NOT NULL
ON CONFLICT (igm_no, line_no, subline_no) DO NOTHING;

INSERT INTO core.igm_line_container (igm_no, line_no, subline_no, container_no,
                                     seal_no, agent_code, status, packages,
                                     weight, iso_code,
                                     id, iso_valid, created_at)
SELECT j.igm_no::bigint, j.line_no, coalesce(j.subline_no,0), j.container_no,
       j.seal_no, j.container_agent_code, j.container_status, j.no_of_packages,
       j.container_weight, j.iso_size_type,
       j.id, j.iso_valid, j.created_at
FROM jnpa.customs_igm_container j
WHERE j.igm_no IS NOT NULL AND j.line_no IS NOT NULL AND j.container_no IS NOT NULL
ON CONFLICT (igm_no, line_no, subline_no, container_no) DO NOTHING;

INSERT INTO core.dq_issue (source_table, record_ref, issue_type, severity, description)
SELECT 'core.igm_line_container', j.id::text, 'missing_key', 'warn',
       'igm container row lacked igm_no/line_no/container_no; not migrated'
FROM jnpa.customs_igm_container j
WHERE j.igm_no::bigint IS NULL OR j.line_no IS NULL OR j.container_no IS NULL;

-- --------------------------- customs: OOC ----------------------------------
INSERT INTO core.bill_of_entry_ooc (be_no, be_date, document_type, igm_no,
                                    igm_line_no, igm_subline_no, iec_code,
                                    importer_name, importer_addr, importer_city,
                                    pincode, cha_code, ooc_no, ooc_date,
                                    nature_of_cargo, quantity, quantity_unit,
                                    packages, country_of_origin,
                                    assessable_value, cif_value, duty_paid,
                                    id, message_id, ooc_type, created_at)
SELECT j.bill_of_entry_no::bigint, j.bill_of_entry_date, j.document_type, j.igm_no::bigint,
       j.line_no, j.subline_no, j.ie_code, j.importer_name, j.importer_address,
       j.importer_city, j.pin_code, j.cha_code, j.out_of_charge_no,
       j.out_of_charge_date, j.nature_of_cargo, j.quantity_out_of_charged,
       j.unit_of_quantity, j.no_of_packages, j.country_of_origin,
       j.assessable_value, j.cif_value, j.total_customs_duty,
       j.id, j.message_id, j.out_of_charge_type, j.created_at
FROM jnpa.customs_ooc j
WHERE j.bill_of_entry_no IS NOT NULL
ON CONFLICT (be_no) DO NOTHING;

-- flatten container level into ooc_item (arch design: container_no on the item)
INSERT INTO core.ooc_item (be_no, container_no, invoice_no, item_sr_no,
                           item_desc, hs_code, cif_value, assessable_value,
                           id, iso_valid, created_at)
SELECT DISTINCT ON (c.bill_of_entry_no::bigint, c.container_no,
                    coalesce(i.invoice_number,''), coalesce(i.item_sr_no,0))
       c.bill_of_entry_no::bigint, c.container_no, coalesce(i.invoice_number,''),
       coalesce(i.item_sr_no,0), i.item_description, i.hs_classification,
       i.cif_value, i.assessable_value,
       i.id, c.iso_valid, i.created_at
FROM jnpa.customs_ooc_item i
JOIN jnpa.customs_ooc_container c ON c.id = i.ooc_container_id
WHERE c.bill_of_entry_no IS NOT NULL AND c.container_no IS NOT NULL
ORDER BY c.bill_of_entry_no::bigint, c.container_no,
         coalesce(i.invoice_number,''), coalesce(i.item_sr_no,0), i.id
ON CONFLICT (be_no, container_no, invoice_no, item_sr_no) DO NOTHING;

-- OOC container rows with no items still matter for container counts
INSERT INTO core.ooc_item (be_no, container_no, invoice_no, item_sr_no,
                           id, iso_valid, created_at)
SELECT c.bill_of_entry_no::bigint, c.container_no, '', 0,
       c.id + 900000000, c.iso_valid, c.created_at
FROM jnpa.customs_ooc_container c
WHERE c.bill_of_entry_no IS NOT NULL AND c.container_no IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM jnpa.customs_ooc_item i
                  WHERE i.ooc_container_id = c.id)
ON CONFLICT (be_no, container_no, invoice_no, item_sr_no) DO NOTHING;

-- --------------------------- customs: SMTP ---------------------------------
INSERT INTO core.smtp_permit (smtp_no, smtp_date, igm_no, igm_date,
                              destination_icd, carrier_code, bond_no,
                              terminal_code, id, message_id, created_at)
SELECT j.smtp_no::bigint, j.smtp_date, j.igm_no::bigint, j.igm_date, j.destination_code,
       j.carrier_code, j.bond_no, j.terminal_operator_code,
       j.id, j.message_id, j.created_at
FROM jnpa.customs_smtp j
WHERE j.smtp_no IS NOT NULL
ON CONFLICT (smtp_no) DO NOTHING;

INSERT INTO core.smtp_container (smtp_no, container_no, igm_line_no,
                                 igm_subline_no, consignee, cargo_desc,
                                 container_type, seal_no, packages,
                                 package_unit, gross_qty, qty_unit,
                                 id, line_no, subline_no, iso_valid, created_at)
SELECT j.smtp_no::bigint, j.container_no, j.line_no, j.subline_no, j.consignee_name,
       j.cargo_desc, j.container_type, j.seal_no, j.no_of_packages,
       j.unit_of_packages, j.gross_qty, j.unit_of_qty,
       j.id, j.line_no, j.subline_no, j.iso_valid, j.created_at
FROM jnpa.customs_smtp_line j
WHERE j.smtp_no IS NOT NULL AND j.container_no IS NOT NULL
ON CONFLICT (smtp_no, container_no) DO NOTHING;

-- --------------------------- customs: LEO / SB -----------------------------
INSERT INTO core.leo (sb_no, sb_date, site_id, rotation_no, leo_date,
                      id, message_id, action, created_at)
SELECT j.sb_no::bigint, j.sb_date, j.site_id, j.rotation_no, j.leo_date,
       j.id, j.message_id, j.action, j.created_at
FROM jnpa.customs_leo j
WHERE j.sb_no IS NOT NULL
ON CONFLICT (sb_no) DO NOTHING;

INSERT INTO core.shipping_bill (sb_no, sb_date, site_id,
                                id, message_id, action, created_at)
SELECT j.sb_no::bigint, j.sb_date, j.site_id, j.id, j.message_id, j.action, j.created_at
FROM jnpa.customs_shipping_bill j
WHERE j.sb_no IS NOT NULL
ON CONFLICT (sb_no) DO NOTHING;

-- --------------------------- customs: RMS ----------------------------------
INSERT INTO core.rms_scan_report (igm_no, shipping_line, agent_pan, vessel_name,
                                  processing_end,
                                  id, message_id, customs_house, igm_date,
                                  igm_date_raw, subject, any_selected,
                                  selected_count, created_at)
SELECT j.igm_no::bigint, j.shipping_line, j.shipping_agent, j.vessel_name,
       j.processing_end_date,
       j.id, j.message_id, j.customs_house, j.igm_date, j.igm_date_raw,
       j.subject, j.any_selected, j.selected_count, j.created_at
FROM jnpa.customs_rms_scanlist j;

INSERT INTO core.rms_scan_container (report_id, sl_no, container_no,
                                     machine_type, scan_location, cfs_name,
                                     goods_desc,
                                     id, igm_no, iso_valid, created_at)
SELECT r.report_id, coalesce(j.sl_no, j.id::int), j.container_no,
       j.scan_machine, j.scan_location, j.cfs_name, j.goods_desc,
       j.id, j.igm_no::bigint, j.iso_valid, j.created_at
FROM jnpa.customs_rms_container j
JOIN core.rms_scan_report r ON r.id = j.scanlist_id
ON CONFLICT (report_id, sl_no) DO NOTHING;

SELECT setval('core.igm_id_seq', coalesce((SELECT max(id) FROM core.igm),0)+1, false);
SELECT setval('core.igm_line_id_seq', coalesce((SELECT max(id) FROM core.igm_line),0)+1, false);
SELECT setval('core.igm_line_container_id_seq', coalesce((SELECT max(id) FROM core.igm_line_container),0)+1, false);
SELECT setval('core.bill_of_entry_ooc_id_seq', coalesce((SELECT max(id) FROM core.bill_of_entry_ooc),0)+1, false);
SELECT setval('core.ooc_item_id_seq', coalesce((SELECT max(id) FROM core.ooc_item),0)+1, false);
SELECT setval('core.smtp_permit_id_seq', coalesce((SELECT max(id) FROM core.smtp_permit),0)+1, false);
SELECT setval('core.smtp_container_id_seq', coalesce((SELECT max(id) FROM core.smtp_container),0)+1, false);
SELECT setval('core.leo_id_seq', coalesce((SELECT max(id) FROM core.leo),0)+1, false);
SELECT setval('core.shipping_bill_id_seq', coalesce((SELECT max(id) FROM core.shipping_bill),0)+1, false);
SELECT setval('core.rms_scan_report_id_ext_seq', coalesce((SELECT max(id) FROM core.rms_scan_report),0)+1, false);
SELECT setval('core.rms_scan_container_id_seq', coalesce((SELECT max(id) FROM core.rms_scan_container),0)+1, false);

-- --------------------------- shipping lines --------------------------------
INSERT INTO core.ref_shipping_line (line_code, name, source, first_seen, last_seen)
SELECT j.line_code, j.line_name, j.source, j.first_seen, j.last_seen
FROM jnpa.shipping_lines j
ON CONFLICT (line_code) DO UPDATE
    SET source = EXCLUDED.source,
        first_seen = EXCLUDED.first_seen,
        last_seen = EXCLUDED.last_seen,
        name = coalesce(core.ref_shipping_line.name, EXCLUDED.name);

INSERT INTO core.advance_list_container
      (direction, terminal_id, vessel_visit, container_no, iso_code,
       load_status, category, line_code, gross_weight_kg, pol, pod,
       departure_mode, group_code, client_code, nominated_cfs, iec_code,
       gst_no, bl_no, seal1, commodity_code, reefer_temp, reefer_temp_unit,
       extras,
       id, import_file_id, row_sha256, container_valid_iso, weight_source_uom,
       destination, voyage, reefer_status, created_at)
SELECT CASE j.list_type WHEN 'EAL' THEN 'E' ELSE 'I' END,
       t.terminal_id, j.vessel_visit, j.container_no, j.iso_code,
       CASE j.freight_kind WHEN 'FULL' THEN 'F' WHEN 'EMPTY' THEN 'E'
            ELSE left(j.freight_kind,1) END,
       j.category, j.shipping_line_code, j.gross_weight_kg, j.pol, j.pod,
       left(j.departure_mode,1), j.group_code, j.client_code, j.nominated_cfs,
       j.iec_code, j.gst_no, j.bill_of_lading, j.seal_no, j.commodity_code,
       j.reefer_temp,
       left(j.reefer_uom,1), j.raw,
       j.id, j.import_file_id, j.row_sha256, j.container_valid_iso,
       j.weight_source_uom, j.destination, j.voyage, j.reefer_status,
       j.created_at
FROM jnpa.sl_advance_containers j
JOIN core.ref_terminal t ON t.code = upper(j.terminal);

INSERT INTO core.dq_issue (source_table, record_ref, issue_type, severity, description)
SELECT 'core.advance_list_container', j.id::text, 'missing_key', 'warn',
       'terminal "'||coalesce(j.terminal,'∅')||'" not in ref_terminal; row not migrated'
FROM jnpa.sl_advance_containers j
LEFT JOIN core.ref_terminal t ON t.code = upper(j.terminal)
WHERE t.terminal_id IS NULL;

INSERT INTO core.advance_list_dg (al_id, slot, imdg_class, un_number)
SELECT a.al_id, 1, j.imdg_code, j.un_number
FROM jnpa.sl_advance_containers j
JOIN core.advance_list_container a ON a.id = j.id
WHERE coalesce(j.imdg_code,'') <> '' OR coalesce(j.un_number,'') <> ''
ON CONFLICT (al_id, slot) DO NOTHING;

SELECT setval('core.advance_list_container_id_seq',
              coalesce((SELECT max(id) FROM core.advance_list_container),0)+1, false);

-- delivery orders: flat legacy rows -> header + lines
INSERT INTO core.delivery_order
      (do_number, do_date, vcn, imo_no, agency_name, custodian_code,
       delivery_type, payload,
       id, import_file_id, message_type, sender_id, receiving_party,
       call_sign, stuff_destuff_flag, shipping_agent_code, vessel_country,
       total_containers, raw_xml, created_at)
SELECT DISTINCT ON (j.document_number)
       j.document_number, j.issued_ts::date, j.vcn, j.imo_number,
       j.shipping_agent_code, j.ca_code, j.delivery_mode,
       jsonb_build_object('common_ref_number', j.common_ref_number),
       j.id, j.import_file_id, j.message_type, j.sender_id, j.receiving_party,
       j.call_sign, j.stuff_destuff_flag, j.shipping_agent_code,
       j.vessel_country, j.total_containers, j.raw_xml, j.created_at
FROM jnpa.sl_delivery_orders j
ORDER BY j.document_number, j.id
ON CONFLICT (do_number) DO NOTHING;

INSERT INTO core.delivery_order_line
      (do_number, line_no, container_no, seal_no, iso_code, cargo_desc,
       pol, pod,
       id, equipment_status, cargo_type, arrival_ts, receipt_date,
       delivery_mode, gate_pass_no, gate_pass_ts, vehicle_no, gate_number,
       ca_code, con_seal_status, issued_ts, created_at)
SELECT j.document_number,
       row_number() OVER (PARTITION BY j.document_number ORDER BY j.id),
       j.container_no, NULL, j.iso_code, j.cargo_type,
       j.loading_port, j.dest_port,
       j.id, j.equipment_status, j.cargo_type, j.arrival_ts, j.receipt_date,
       j.delivery_mode, j.gate_pass_no, j.gate_pass_ts, j.vehicle_no,
       j.gate_number, j.ca_code, j.con_seal_status, j.issued_ts, j.created_at
FROM jnpa.sl_delivery_orders j
ON CONFLICT (do_number, line_no) DO NOTHING;

SELECT setval('core.delivery_order_id_seq',
              coalesce((SELECT max(id) FROM core.delivery_order),0)+1, false);
SELECT setval('core.delivery_order_line_id_seq',
              coalesce((SELECT max(id) FROM core.delivery_order_line),0)+1, false);

-- --------------------------- deferred FKs ----------------------------------
DO $$ BEGIN
    ALTER TABLE core.transporter_vehicle
        ADD CONSTRAINT transporter_vehicle_transporter_id_fkey
        FOREIGN KEY (transporter_id) REFERENCES core.transporter(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE core.transporter_blacklist
        ADD CONSTRAINT transporter_blacklist_transporter_id_fkey
        FOREIGN KEY (transporter_id) REFERENCES core.transporter(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
