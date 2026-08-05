-- 0201  Backfill ported tables (column-order exact copies)
SET session_replication_role = replica;

TRUNCATE core.accident_event CASCADE;
INSERT INTO core.accident_event (id, accident_id, action, old_status, new_status, note, actor, created_at)
  SELECT id, accident_id, action, old_status, new_status, note, actor, created_at FROM jnpa.accident_events;
SELECT setval('core.accident_event_id_seq', coalesce((SELECT max(id) FROM core.accident_event), 0) + 1, false);
TRUNCATE core.accident CASCADE;
INSERT INTO core.accident (id, accident_ref, occurred_at, accident_type, severity, lat, lon, location, vehicle_id, plate, driver_id, description, status, investigation_status, resolution, reported_by, source, created_at, updated_at)
  SELECT id, accident_ref, occurred_at, accident_type, severity, lat, lon, location, vehicle_id, plate, driver_id, description, status, investigation_status, resolution, reported_by, source, created_at, updated_at FROM jnpa.accidents;
SELECT setval('core.accident_id_seq', coalesce((SELECT max(id) FROM core.accident), 0) + 1, false);
TRUNCATE core.alert CASCADE;
INSERT INTO core.alert (id, ts, kind, severity, gate_id, plate, payload, ack)
  SELECT id, ts, kind, severity, gate_id, plate, payload, ack FROM jnpa.alerts;
TRUNCATE core.anpr_read CASCADE;
INSERT INTO core.anpr_read (ts, camera_id, plate, conf, vehicle_class, image_url, weather, degraded)
  SELECT ts, camera_id, plate, conf, vehicle_class, image_url, weather, degraded FROM jnpa.anpr_reads;
TRUNCATE core.api_audit_log CASCADE;
INSERT INTO core.api_audit_log (id, service_name, endpoint, method, request_payload, response_payload, status_code, latency_ms, error, transaction_id, created_at)
  SELECT id, service_name, endpoint, method, request_payload, response_payload, status_code, latency_ms, error, transaction_id, created_at FROM jnpa.api_audit_log;
SELECT setval('core.api_audit_log_id_seq', coalesce((SELECT max(id) FROM core.api_audit_log), 0) + 1, false);
TRUNCATE core.automation_execution CASCADE;
INSERT INTO core.automation_execution (id, ts, event, results, matched_count)
  SELECT id, ts, event, results, matched_count FROM jnpa.automation_executions;
SELECT setval('core.automation_execution_id_seq', coalesce((SELECT max(id) FROM core.automation_execution), 0) + 1, false);
TRUNCATE core.automation_rule CASCADE;
INSERT INTO core.automation_rule (id, name, enabled, field, op, value, actions, created_at, updated_at)
  SELECT id, name, enabled, field, op, value, actions, created_at, updated_at FROM jnpa.automation_rules;
TRUNCATE core.berthing_record_event CASCADE;
INSERT INTO core.berthing_record_event (id, berthing_id, event_type, event_time, created_by, created_at)
  SELECT id, berthing_id, event_type, event_time, created_by, created_at FROM jnpa.berthing_events;
SELECT setval('core.berthing_record_event_id_seq', coalesce((SELECT max(id) FROM core.berthing_record_event), 0) + 1, false);
TRUNCATE core.berthing_import_error CASCADE;
INSERT INTO core.berthing_import_error (id, import_file_id, row_number, error_message, raw_data, created_at)
  SELECT id, import_file_id, row_number, error_message, raw_data, created_at FROM jnpa.berthing_import_errors;
SELECT setval('core.berthing_import_error_id_seq', coalesce((SELECT max(id) FROM core.berthing_import_error), 0) + 1, false);
TRUNCATE core.berthing_import_file CASCADE;
INSERT INTO core.berthing_import_file (id, filename, file_hash, terminal, physical_format, uploaded_by, status, total_rows, success_rows, failed_rows, duplicate_rows, source, error_detail, created_at, updated_at)
  SELECT id, filename, file_hash, terminal, physical_format, uploaded_by, status, total_rows, success_rows, failed_rows, duplicate_rows, source, error_detail, created_at, updated_at FROM jnpa.berthing_import_files;
SELECT setval('core.berthing_import_file_id_seq', coalesce((SELECT max(id) FROM core.berthing_import_file), 0) + 1, false);
TRUNCATE core.berthing_report_document CASCADE;
INSERT INTO core.berthing_report_document (id, file_name, terminal, report_date, pdf_hash, page_count, table_count, row_count, uploaded_by, created_at)
  SELECT id, file_name, terminal, report_date, pdf_hash, page_count, table_count, row_count, uploaded_by, created_at FROM jnpa.berthing_report_documents;
SELECT setval('core.berthing_report_document_id_seq', coalesce((SELECT max(id) FROM core.berthing_report_document), 0) + 1, false);
TRUNCATE core.berthing_report_table CASCADE;
INSERT INTO core.berthing_report_table (id, document_id, terminal, table_name, panel_index, page_number, original_columns, rows, row_count, extraction_note, created_at)
  SELECT id, document_id, terminal, table_name, panel_index, page_number, original_columns, rows, row_count, extraction_note, created_at FROM jnpa.berthing_report_tables;
SELECT setval('core.berthing_report_table_id_seq', coalesce((SELECT max(id) FROM core.berthing_report_table), 0) + 1, false);
TRUNCATE core.berthing_record CASCADE;
INSERT INTO core.berthing_record (id, terminal, vessel_name, imo_number, voyage_number, shipping_line, berth_number, eta, ata, berthing_time, departure_time, cargo_operation_start, cargo_operation_end, status, source_file, import_file_id, created_at, updated_at)
  SELECT id, terminal, vessel_name, imo_number, voyage_number, shipping_line, berth_number, eta, ata, berthing_time, departure_time, cargo_operation_start, cargo_operation_end, status, source_file, import_file_id, created_at, updated_at FROM jnpa.berthing_reports;
SELECT setval('core.berthing_record_id_seq', coalesce((SELECT max(id) FROM core.berthing_record), 0) + 1, false);
TRUNCATE core.bottleneck_snapshot CASCADE;
INSERT INTO core.bottleneck_snapshot (id, ts, rank, segment_id, name, jam_factor, speed_kmh, free_flow_kmh, avg_delay_min, lat, lon, detail)
  SELECT id, ts, rank, segment_id, name, jam_factor, speed_kmh, free_flow_kmh, avg_delay_min, lat, lon, detail FROM jnpa.bottleneck_snapshots;
SELECT setval('core.bottleneck_snapshot_id_seq', coalesce((SELECT max(id) FROM core.bottleneck_snapshot), 0) + 1, false);
TRUNCATE core.camera_ai_count CASCADE;
INSERT INTO core.camera_ai_count (id, ts, camera_id, gate_id, vehicle_count, queue_count, class_counts, congestion_level, confidence, source, detail)
  SELECT id, ts, camera_id, gate_id, vehicle_count, queue_count, class_counts, congestion_level, confidence, source, detail FROM jnpa.camera_ai_counts;
SELECT setval('core.camera_ai_count_id_seq', coalesce((SELECT max(id) FROM core.camera_ai_count), 0) + 1, false);
TRUNCATE core.camera CASCADE;
INSERT INTO core.camera (id, gate_id, name, lat, lon, role, installed_at)
  SELECT id, gate_id, name, lat, lon, role, installed_at FROM jnpa.cameras;
TRUNCATE core.carbon_emission CASCADE;
INSERT INTO core.carbon_emission (id, vehicle_id, vehicle_type, distance_km, fuel_consumed_litre, idle_time_minutes, co2_kg, source, calculation_method, created_at)
  SELECT id, vehicle_id, vehicle_type, distance_km, fuel_consumed_litre, idle_time_minutes, co2_kg, source, calculation_method, created_at FROM jnpa.carbon_emission;
SELECT setval('core.carbon_emission_id_seq', coalesce((SELECT max(id) FROM core.carbon_emission), 0) + 1, false);
TRUNCATE core.cargo CASCADE;
INSERT INTO core.cargo (container_number, vessel_name, customs_status, yard_block, is_released, vehicle_number, gate, camera_id, eta, created_at, updated_at, eseal_status, eseal_number, pre_document_status, origin_stream, workflow_status, lifecycle_status)
  SELECT container_number, vessel_name, customs_status, yard_block, is_released, vehicle_number, gate, camera_id, eta, created_at, updated_at, eseal_status, eseal_number, pre_document_status, origin_stream, workflow_status, lifecycle_status FROM jnpa.cargo;
TRUNCATE core.cargo_event CASCADE;
INSERT INTO core.cargo_event (id, event, container_number, payload, created_at)
  SELECT id, event, container_number, payload, created_at FROM jnpa.cargo_events;
SELECT setval('core.cargo_event_id_seq', coalesce((SELECT max(id) FROM core.cargo_event), 0) + 1, false);
TRUNCATE core.cargo_lifecycle_event CASCADE;
INSERT INTO core.cargo_lifecycle_event (id, container_number, action, old_status, new_status, actor_role, note, created_at)
  SELECT id, container_number, action, old_status, new_status, actor_role, note, created_at FROM jnpa.cargo_lifecycle_events;
SELECT setval('core.cargo_lifecycle_event_id_seq', coalesce((SELECT max(id) FROM core.cargo_lifecycle_event), 0) + 1, false);
TRUNCATE core.cargo_notification CASCADE;
INSERT INTO core.cargo_notification (id, container_number, notification_type, severity, message, stakeholders, status, created_at)
  SELECT id, container_number, notification_type, severity, message, stakeholders, status, created_at FROM jnpa.cargo_notifications;
SELECT setval('core.cargo_notification_id_seq', coalesce((SELECT max(id) FROM core.cargo_notification), 0) + 1, false);
TRUNCATE core.cargo_rake_plan CASCADE;
INSERT INTO core.cargo_rake_plan (id, rake_id, containers, planned_containers, status, created_at)
  SELECT id, rake_id, containers, planned_containers, status, created_at FROM jnpa.cargo_rake_plans;
SELECT setval('core.cargo_rake_plan_id_seq', coalesce((SELECT max(id) FROM core.cargo_rake_plan), 0) + 1, false);
TRUNCATE core.cargo_reefer_plan CASCADE;
INSERT INTO core.cargo_reefer_plan (id, container_number, temperature, power_required, slot, status, created_at)
  SELECT id, container_number, temperature, power_required, slot, status, created_at FROM jnpa.cargo_reefer_plans;
SELECT setval('core.cargo_reefer_plan_id_seq', coalesce((SELECT max(id) FROM core.cargo_reefer_plan), 0) + 1, false);
TRUNCATE core.cargo_scan_verification CASCADE;
INSERT INTO core.cargo_scan_verification (id, container_number, verified, remarks, actor_role, created_at)
  SELECT id, container_number, verified, remarks, actor_role, created_at FROM jnpa.cargo_scan_verifications;
SELECT setval('core.cargo_scan_verification_id_seq', coalesce((SELECT max(id) FROM core.cargo_scan_verification), 0) + 1, false);
TRUNCATE core.cargo_workflow_event CASCADE;
INSERT INTO core.cargo_workflow_event (id, container_number, action, old_status, new_status, comment, created_at)
  SELECT id, container_number, action, old_status, new_status, comment, created_at FROM jnpa.cargo_workflow_events;
SELECT setval('core.cargo_workflow_event_id_seq', coalesce((SELECT max(id) FROM core.cargo_workflow_event), 0) + 1, false);
TRUNCATE core.cargo_yard_plan CASCADE;
INSERT INTO core.cargo_yard_plan (id, container_number, preferred_block, assigned_block, priority, status, created_at, yard_row, yard_slot, yard_position)
  SELECT id, container_number, preferred_block, assigned_block, priority, status, created_at, yard_row, yard_slot, yard_position FROM jnpa.cargo_yard_plans;
SELECT setval('core.cargo_yard_plan_id_seq', coalesce((SELECT max(id) FROM core.cargo_yard_plan), 0) + 1, false);
TRUNCATE core.case_audit CASCADE;
INSERT INTO core.case_audit (id, case_id, event, from_status, to_status, actor, detail, prev_hash, hash, ts)
  SELECT id, case_id, event, from_status, to_status, actor, detail, prev_hash, hash, ts FROM jnpa.case_audit;
SELECT setval('core.case_audit_id_seq', coalesce((SELECT max(id) FROM core.case_audit), 0) + 1, false);
TRUNCATE core.cfs_ecy_import_error CASCADE;
INSERT INTO core.cfs_ecy_import_error (id, import_file_id, record_ref, error_code, error_detail, created_at)
  SELECT id, import_file_id, record_ref, error_code, error_detail, created_at FROM jnpa.cfs_ecy_import_errors;
SELECT setval('core.cfs_ecy_import_error_id_seq', coalesce((SELECT max(id) FROM core.cfs_ecy_import_error), 0) + 1, false);
TRUNCATE core.cfs_ecy_import_file CASCADE;
INSERT INTO core.cfs_ecy_import_file (id, facility_type, physical_format, source_file, source_sha256, file_size_bytes, record_count, imported_count, error_count, duplicate_count, import_status, error_detail, uploaded_by, source, created_at, updated_at)
  SELECT id, facility_type, physical_format, source_file, source_sha256, file_size_bytes, record_count, imported_count, error_count, duplicate_count, import_status, error_detail, uploaded_by, source, created_at, updated_at FROM jnpa.cfs_ecy_import_files;
SELECT setval('core.cfs_ecy_import_file_id_seq', coalesce((SELECT max(id) FROM core.cfs_ecy_import_file), 0) + 1, false);
TRUNCATE core.cfs_ecy_movement CASCADE;
INSERT INTO core.cfs_ecy_movement (id, facility_type, container_number, iso_valid, event_ts, mode, source, source_file, created_at, import_file_id)
  SELECT id, facility_type, container_number, iso_valid, event_ts, mode, source, source_file, created_at, import_file_id FROM jnpa.cfs_ecy_movements;
SELECT setval('core.cfs_ecy_movement_id_seq', coalesce((SELECT max(id) FROM core.cfs_ecy_movement), 0) + 1, false);
TRUNCATE core.challan CASCADE;
INSERT INTO core.challan (challan_id, challan_no, case_id, vehicle_number, total_fine, status, mva_section, issued_at, payment_ref, pdf_url, evidence_sha256, created_by)
  SELECT challan_id, challan_no, case_id, vehicle_number, total_fine, status, mva_section, issued_at, payment_ref, pdf_url, evidence_sha256, created_by FROM jnpa.challans;
TRUNCATE core.container_movement_history CASCADE;
INSERT INTO core.container_movement_history (id, container_id, allocation_id, movement_type, location, detail, created_at)
  SELECT id, container_id, allocation_id, movement_type, location, detail, created_at FROM jnpa.container_movement_history;
SELECT setval('core.container_movement_history_id_seq', coalesce((SELECT max(id) FROM core.container_movement_history), 0) + 1, false);
TRUNCATE core.container_read CASCADE;
INSERT INTO core.container_read (id, ts, camera_id, gate_id, container_number, iso_type, check_digit_ok, valid, plate, vehicle_id, confidence, image_url, source, detail)
  SELECT id, ts, camera_id, gate_id, container_number, iso_type, check_digit_ok, valid, plate, vehicle_id, confidence, image_url, source, detail FROM jnpa.container_reads;
SELECT setval('core.container_read_id_seq', coalesce((SELECT max(id) FROM core.container_read), 0) + 1, false);
TRUNCATE core.customs_event CASCADE;
INSERT INTO core.customs_event (id, event, module, reference, container_no, payload, created_at)
  SELECT id, event, module, reference, container_no, payload, created_at FROM jnpa.customs_events;
SELECT setval('core.customs_event_id_seq', coalesce((SELECT max(id) FROM core.customs_event), 0) + 1, false);
TRUNCATE core.customs_import_error CASCADE;
INSERT INTO core.customs_import_error (id, message_id, record_ref, error_code, error_detail, created_at)
  SELECT id, message_id, record_ref, error_code, error_detail, created_at FROM jnpa.customs_import_errors;
SELECT setval('core.customs_import_error_id_seq', coalesce((SELECT max(id) FROM core.customs_import_error), 0) + 1, false);
TRUNCATE core.customs_message CASCADE;
INSERT INTO core.customs_message (id, message_type, module, control_number, sender_id, receiver_id, message_id_code, sent_ts, primary_ref, source_file, source_sha256, file_size_bytes, record_count, imported_count, error_count, import_status, error_detail, created_at, updated_at)
  SELECT id, message_type, module, control_number, sender_id, receiver_id, message_id_code, sent_ts, primary_ref, source_file, source_sha256, file_size_bytes, record_count, imported_count, error_count, import_status, error_detail, created_at, updated_at FROM jnpa.customs_messages;
SELECT setval('core.customs_message_id_seq', coalesce((SELECT max(id) FROM core.customs_message), 0) + 1, false);
TRUNCATE core.decision_audit CASCADE;
INSERT INTO core.decision_audit (id, request_id, input_data, rule_executed, decision, action_taken, created_at)
  SELECT id, request_id, input_data, rule_executed, decision, action_taken, created_at FROM jnpa.decision_audit;
SELECT setval('core.decision_audit_id_seq', coalesce((SELECT max(id) FROM core.decision_audit), 0) + 1, false);
TRUNCATE core.device_binding CASCADE;
INSERT INTO core.device_binding (device_id, mobile, driver_id, bound_at, last_seen, active)
  SELECT device_id, mobile, driver_id, bound_at, last_seen, active FROM jnpa.device_bindings;
TRUNCATE core.digital_twin_event CASCADE;
INSERT INTO core.digital_twin_event (id, event_type, vehicle_id, driver_id, location, payload, created_at)
  SELECT id, event_type, vehicle_id, driver_id, location, payload, created_at FROM jnpa.digital_twin_events;
SELECT setval('core.digital_twin_event_id_seq', coalesce((SELECT max(id) FROM core.digital_twin_event), 0) + 1, false);
TRUNCATE core.document_ocr CASCADE;
INSERT INTO core.document_ocr (id, ts, doc_type, source_ref, storage_url, raw_text, fields, confidence, status, source, created_at)
  SELECT id, ts, doc_type, source_ref, storage_url, raw_text, fields, confidence, status, source, created_at FROM jnpa.document_ocr;
SELECT setval('core.document_ocr_id_seq', coalesce((SELECT max(id) FROM core.document_ocr), 0) + 1, false);
TRUNCATE core.driver_enrollment CASCADE;
INSERT INTO core.driver_enrollment (driver_id, name, license_no, mobile, vehicle_no, aadhaar_masked, emergency_contact, status, consent, consent_at, face_images, reference_image, photo_url, documents, template_dim, provider, submitted_at, reviewed_at, reviewed_by, rejection_reason, updated_at, created_by, source)
  SELECT driver_id, name, license_no, mobile, vehicle_no, aadhaar_masked, emergency_contact, status, consent, consent_at, face_images, reference_image, photo_url, documents, template_dim, provider, submitted_at, reviewed_at, reviewed_by, rejection_reason, updated_at, created_by, source FROM jnpa.driver_enrollments;
TRUNCATE core.driver_face CASCADE;
INSERT INTO core.driver_face (driver_id, embedding, dim, provider, model_version, created_at, updated_at)
  SELECT driver_id, embedding, dim, provider, model_version, created_at, updated_at FROM jnpa.driver_faces;
TRUNCATE core.driver_license_lookup_history CASCADE;
INSERT INTO core.driver_license_lookup_history (id, dl_number, request_payload, response_payload, status, source, created_at)
  SELECT id, dl_number, request_payload, response_payload, status, source, created_at FROM jnpa.driver_license_lookup_history;
SELECT setval('core.driver_license_lookup_history_id_seq', coalesce((SELECT max(id) FROM core.driver_license_lookup_history), 0) + 1, false);
TRUNCATE core.driver_identity CASCADE;
INSERT INTO core.driver_identity (driver_id, name, license_no, mobile, vehicle_no, aadhaar_masked, emergency_contact, status, photo_url, reference_image, template_dim, provider, enrolled_at, approved_by, updated_at, created_by, vehicle_no_norm)
  SELECT driver_id, name, license_no, mobile, vehicle_no, aadhaar_masked, emergency_contact, status, photo_url, reference_image, template_dim, provider, enrolled_at, approved_by, updated_at, created_by, vehicle_no_norm FROM jnpa.drivers;
TRUNCATE core.empty_container_allocation CASCADE;
INSERT INTO core.empty_container_allocation (id, container_id, truck_id, trailer_id, driver_id, shipping_line, cfs, ecd, allocation_reason, allocated_at, status)
  SELECT id, container_id, truck_id, trailer_id, driver_id, shipping_line, cfs, ecd, allocation_reason, allocated_at, status FROM jnpa.empty_container_allocations;
SELECT setval('core.empty_container_allocation_id_seq', coalesce((SELECT max(id) FROM core.empty_container_allocation), 0) + 1, false);
TRUNCATE core.empty_container_inventory CASCADE;
INSERT INTO core.empty_container_inventory (container_id, container_type, location, owner, availability_status, updated_at)
  SELECT container_id, container_type, location, owner, availability_status, updated_at FROM jnpa.empty_container_inventory;
TRUNCATE core.enrollment_audit CASCADE;
INSERT INTO core.enrollment_audit (id, driver_id, event, actor, detail, ts)
  SELECT id, driver_id, event, actor, detail, ts FROM jnpa.enrollment_audit;
SELECT setval('core.enrollment_audit_id_seq', coalesce((SELECT max(id) FROM core.enrollment_audit), 0) + 1, false);
TRUNCATE core.fastag_balance CASCADE;
INSERT INTO core.fastag_balance (rc_number, tag_id, provider_name, provider_code, customer_name, available_recharge_limit, available_balance, tag_status, vehicle_class, vehicle_class_desc, model_name, updated_at)
  SELECT rc_number, tag_id, provider_name, provider_code, customer_name, available_recharge_limit, available_balance, tag_status, vehicle_class, vehicle_class_desc, model_name, updated_at FROM jnpa.fastag_balance;
TRUNCATE core.fastag_transaction CASCADE;
INSERT INTO core.fastag_transaction (id, tag_id, rc_number, seq_no, transaction_date_time, lane_direction, toll_plaza_name, toll_plaza_geocode, vehicle_type, created_at, bank_name, status)
  SELECT id, tag_id, rc_number, seq_no, transaction_date_time, lane_direction, toll_plaza_name, toll_plaza_geocode, vehicle_type, created_at, bank_name, status FROM jnpa.fastag_transactions;
TRUNCATE core.gate_capture CASCADE;
INSERT INTO core.gate_capture (id, capture_type, container_no, vehicle_plate, gate_id, source_mode, status, captured_at, payload, created_at)
  SELECT id, capture_type, container_no, vehicle_plate, gate_id, source_mode, status, captured_at, payload, created_at FROM jnpa.gate_captures;
SELECT setval('core.gate_capture_id_seq', coalesce((SELECT max(id) FROM core.gate_capture), 0) + 1, false);
TRUNCATE core.gate_event CASCADE;
INSERT INTO core.gate_event (id, ts, device_id, plate, gate_id, trip_id, event_type, lat, lon)
  SELECT id, ts, device_id, plate, gate_id, trip_id, event_type, lat, lon FROM jnpa.gate_events;
SELECT setval('core.gate_event_id_seq', coalesce((SELECT max(id) FROM core.gate_event), 0) + 1, false);
TRUNCATE core.gate CASCADE;
INSERT INTO core.gate (id, name, lat, lon, closed_at)
  SELECT id, name, lat, lon, closed_at FROM jnpa.gates;
TRUNCATE core.geofence_event CASCADE;
INSERT INTO core.geofence_event (id, vehicle_id, zone_id, entry_time, exit_time, violation_type, action_taken, created_at, driver_id, event_type, dwell_seconds)
  SELECT id, vehicle_id, zone_id, entry_time, exit_time, violation_type, action_taken, created_at, driver_id, event_type, dwell_seconds FROM jnpa.geofence_events;
SELECT setval('core.geofence_event_id_seq', coalesce((SELECT max(id) FROM core.geofence_event), 0) + 1, false);
TRUNCATE core.geofence_zone CASCADE;
INSERT INTO core.geofence_zone (id, name, kind, polygon, escalation, enabled, updated_at)
  SELECT id, name, kind, polygon, escalation, enabled, updated_at FROM jnpa.geofence_zones;
TRUNCATE core.integration_lookup CASCADE;
INSERT INTO core.integration_lookup (id, ts, system, op, ref, request, response, source, latency_ms, created_at)
  SELECT id, ts, system, op, ref, request, response, source, latency_ms, created_at FROM jnpa.integration_lookups;
SELECT setval('core.integration_lookup_id_seq', coalesce((SELECT max(id) FROM core.integration_lookup), 0) + 1, false);
TRUNCATE core.ldb_movement CASCADE;
INSERT INTO core.ldb_movement (id, ts, container_number, event, location, terminal, mode, source, detail)
  SELECT id, ts, container_number, event, location, terminal, mode, source, detail FROM jnpa.ldb_movements;
SELECT setval('core.ldb_movement_id_seq', coalesce((SELECT max(id) FROM core.ldb_movement), 0) + 1, false);
TRUNCATE core.leo_reconciliation CASCADE;
INSERT INTO core.leo_reconciliation (id, container_no, vehicle_plate, leo_ready, customs_flags, checks, source_mode, reconciled_at)
  SELECT id, container_no, vehicle_plate, leo_ready, customs_flags, checks, source_mode, reconciled_at FROM jnpa.leo_reconciliation;
SELECT setval('core.leo_reconciliation_id_seq', coalesce((SELECT max(id) FROM core.leo_reconciliation), 0) + 1, false);
TRUNCATE core.notification CASCADE;
INSERT INTO core.notification (id, event_id, channel, receiver, message, delivery_status, provider_response, created_at)
  SELECT id, event_id, channel, receiver, message, delivery_status, provider_response, created_at FROM jnpa.notifications;
SELECT setval('core.notification_id_seq', coalesce((SELECT max(id) FROM core.notification), 0) + 1, false);
TRUNCATE core.nvr_camera_map CASCADE;
INSERT INTO core.nvr_camera_map (id, nvr_id, channel, camera_id, stream_url, codec, resolution, fps, status, created_at)
  SELECT id, nvr_id, channel, camera_id, stream_url, codec, resolution, fps, status, created_at FROM jnpa.nvr_camera_map;
SELECT setval('core.nvr_camera_map_id_seq', coalesce((SELECT max(id) FROM core.nvr_camera_map), 0) + 1, false);
TRUNCATE core.nvr_device CASCADE;
INSERT INTO core.nvr_device (id, name, vendor, host, port, protocol, channels, location, status, source, created_at, updated_at)
  SELECT id, name, vendor, host, port, protocol, channels, location, status, source, created_at, updated_at FROM jnpa.nvr_devices;
TRUNCATE core.otp_request CASCADE;
INSERT INTO core.otp_request (id, mobile, device_id, code_hash, expires_at, verified, attempts, created_at)
  SELECT id, mobile, device_id, code_hash, expires_at, verified, attempts, created_at FROM jnpa.otp_requests;
SELECT setval('core.otp_request_id_seq', coalesce((SELECT max(id) FROM core.otp_request), 0) + 1, false);
TRUNCATE core.parking_event CASCADE;
INSERT INTO core.parking_event (id, event_type, vehicle_id, driver_id, facility_id, slot_id, detail, created_at)
  SELECT id, event_type, vehicle_id, driver_id, facility_id, slot_id, detail, created_at FROM jnpa.parking_events;
SELECT setval('core.parking_event_id_seq', coalesce((SELECT max(id) FROM core.parking_event), 0) + 1, false);
TRUNCATE core.parking_facility CASCADE;
INSERT INTO core.parking_facility (id, facility_name, location, capacity, status, created_at)
  SELECT id, facility_name, location, capacity, status, created_at FROM jnpa.parking_facilities;
TRUNCATE core.parking_slot CASCADE;
INSERT INTO core.parking_slot (id, facility_id, slot_number, availability_status, vehicle_id, updated_at)
  SELECT id, facility_id, slot_number, availability_status, vehicle_id, updated_at FROM jnpa.parking_slots;
SELECT setval('core.parking_slot_id_seq', coalesce((SELECT max(id) FROM core.parking_slot), 0) + 1, false);
TRUNCATE core.parking_transaction CASCADE;
INSERT INTO core.parking_transaction (id, vehicle_id, driver_id, facility_id, slot_id, entry_time, exit_time, duration, status, created_at)
  SELECT id, vehicle_id, driver_id, facility_id, slot_id, entry_time, exit_time, duration, status, created_at FROM jnpa.parking_transactions;
SELECT setval('core.parking_transaction_id_seq', coalesce((SELECT max(id) FROM core.parking_transaction), 0) + 1, false);
TRUNCATE core.perf_daily_snapshot CASCADE;
INSERT INTO core.perf_daily_snapshot (id, report_date, as_of_ts, source_file, created_at, upload_id, uploaded_at)
  SELECT id, report_date, as_of_ts, source_file, created_at, upload_id, uploaded_at FROM jnpa.perf_daily_snapshot;
SELECT setval('core.perf_daily_snapshot_id_seq', coalesce((SELECT max(id) FROM core.perf_daily_snapshot), 0) + 1, false);
TRUNCATE core.perf_daily_terminal_status CASCADE;
INSERT INTO core.perf_daily_terminal_status (id, report_date, terminal_code, icd_pendency_teus, cfs_pendency_teus, yard_import_teus, yard_export_teus, yard_transhipment_teus, yard_total_teus, yard_usable_capacity_teus, yard_occupancy_pct, gate_in_teus, gate_out_teus, gate_total_teus, reefer_total_slots, reefer_occupied_slots, reefer_available_slots, created_at, source_file, upload_id, uploaded_at)
  SELECT id, report_date, terminal_code, icd_pendency_teus, cfs_pendency_teus, yard_import_teus, yard_export_teus, yard_transhipment_teus, yard_total_teus, yard_usable_capacity_teus, yard_occupancy_pct, gate_in_teus, gate_out_teus, gate_total_teus, reefer_total_slots, reefer_occupied_slots, reefer_available_slots, created_at, source_file, upload_id, uploaded_at FROM jnpa.perf_daily_terminal_status;
SELECT setval('core.perf_daily_terminal_status_id_seq', coalesce((SELECT max(id) FROM core.perf_daily_terminal_status), 0) + 1, false);
TRUNCATE core.perf_daily_tonnage CASCADE;
INSERT INTO core.perf_daily_tonnage (id, report_date, category, period, vessels, liquid_tonnes, dry_bulk_tonnes, break_bulk_tonnes, total_tonnes, created_at, source_file, upload_id, uploaded_at)
  SELECT id, report_date, category, period, vessels, liquid_tonnes, dry_bulk_tonnes, break_bulk_tonnes, total_tonnes, created_at, source_file, upload_id, uploaded_at FROM jnpa.perf_daily_tonnage;
SELECT setval('core.perf_daily_tonnage_id_seq', coalesce((SELECT max(id) FROM core.perf_daily_tonnage), 0) + 1, false);
TRUNCATE core.perf_daily_traffic CASCADE;
INSERT INTO core.perf_daily_traffic (id, report_date, terminal_code, period, vessels, imp_teus, exp_teus, total_teus, rakes, rail_dis_teus, rail_ldg_teus, rail_total_teus, created_at, source_file, upload_id, uploaded_at)
  SELECT id, report_date, terminal_code, period, vessels, imp_teus, exp_teus, total_teus, rakes, rail_dis_teus, rail_ldg_teus, rail_total_teus, created_at, source_file, upload_id, uploaded_at FROM jnpa.perf_daily_traffic;
SELECT setval('core.perf_daily_traffic_id_seq', coalesce((SELECT max(id) FROM core.perf_daily_traffic), 0) + 1, false);
TRUNCATE core.perf_daily_vessel CASCADE;
INSERT INTO core.perf_daily_vessel (id, report_date, terminal_code, berth_no, via_no, vessel_name, cargo_commodity, berthed_on, expected_completion, created_at, source_file, upload_id, uploaded_at)
  SELECT id, report_date, terminal_code, berth_no, via_no, vessel_name, cargo_commodity, berthed_on, expected_completion, created_at, source_file, upload_id, uploaded_at FROM jnpa.perf_daily_vessels;
SELECT setval('core.perf_daily_vessel_id_seq', coalesce((SELECT max(id) FROM core.perf_daily_vessel), 0) + 1, false);
TRUNCATE core.perf_import_log CASCADE;
INSERT INTO core.perf_import_log (id, upload_id, phase, level, message, target_table, affected_rows, created_at)
  SELECT id, upload_id, phase, level, message, target_table, affected_rows, created_at FROM jnpa.perf_import_logs;
SELECT setval('core.perf_import_log_id_seq', coalesce((SELECT max(id) FROM core.perf_import_log), 0) + 1, false);
TRUNCATE core.perf_ldb_congestion CASCADE;
INSERT INTO core.perf_ldb_congestion (id, report_month, cycle, cluster_no, cluster_name, cfs_count, pct_containers, congestion_level, created_at, source_file, upload_id, uploaded_at)
  SELECT id, report_month, cycle, cluster_no, cluster_name, cfs_count, pct_containers, congestion_level, created_at, source_file, upload_id, uploaded_at FROM jnpa.perf_ldb_congestion;
SELECT setval('core.perf_ldb_congestion_id_seq', coalesce((SELECT max(id) FROM core.perf_ldb_congestion), 0) + 1, false);
TRUNCATE core.perf_ldb_facility_dwell CASCADE;
INSERT INTO core.perf_ldb_facility_dwell (id, report_month, facility_type, facility_name, facility_name_norm, dwell_hours, dwell_hours_prev, created_at, source_file, upload_id, uploaded_at)
  SELECT id, report_month, facility_type, facility_name, facility_name_norm, dwell_hours, dwell_hours_prev, created_at, source_file, upload_id, uploaded_at FROM jnpa.perf_ldb_facility_dwell;
SELECT setval('core.perf_ldb_facility_dwell_id_seq', coalesce((SELECT max(id) FROM core.perf_ldb_facility_dwell), 0) + 1, false);
TRUNCATE core.perf_ldb_port_dwell CASCADE;
INSERT INTO core.perf_ldb_port_dwell (id, report_month, terminal_code, cycle, segment, dwell_hours, dwell_hours_prev, created_at, source_file, upload_id, uploaded_at)
  SELECT id, report_month, terminal_code, cycle, segment, dwell_hours, dwell_hours_prev, created_at, source_file, upload_id, uploaded_at FROM jnpa.perf_ldb_port_dwell;
SELECT setval('core.perf_ldb_port_dwell_id_seq', coalesce((SELECT max(id) FROM core.perf_ldb_port_dwell), 0) + 1, false);
TRUNCATE core.perf_ldb_route_movement CASCADE;
INSERT INTO core.perf_ldb_route_movement (id, report_month, cycle, transport_mode, route_name, pct_share, created_at, source_file, upload_id, uploaded_at)
  SELECT id, report_month, cycle, transport_mode, route_name, pct_share, created_at, source_file, upload_id, uploaded_at FROM jnpa.perf_ldb_route_movement;
SELECT setval('core.perf_ldb_route_movement_id_seq', coalesce((SELECT max(id) FROM core.perf_ldb_route_movement), 0) + 1, false);
TRUNCATE core.perf_ldb_weather CASCADE;
INSERT INTO core.perf_ldb_weather (id, report_month, terminal_code, cycle, weather, dwell_hours, created_at, source_file, upload_id, uploaded_at)
  SELECT id, report_month, terminal_code, cycle, weather, dwell_hours, created_at, source_file, upload_id, uploaded_at FROM jnpa.perf_ldb_weather;
SELECT setval('core.perf_ldb_weather_id_seq', coalesce((SELECT max(id) FROM core.perf_ldb_weather), 0) + 1, false);
TRUNCATE core.perf_monthly_teu CASCADE;
INSERT INTO core.perf_monthly_teu (id, fiscal_year, month_date, year_label, month_label, terminal_code, vessel_calls, discharge_teus, load_teus, total_teus, created_at, source_file, upload_id, uploaded_at)
  SELECT id, fiscal_year, month_date, year_label, month_label, terminal_code, vessel_calls, discharge_teus, load_teus, total_teus, created_at, source_file, upload_id, uploaded_at FROM jnpa.perf_monthly_teu;
SELECT setval('core.perf_monthly_teu_id_seq', coalesce((SELECT max(id) FROM core.perf_monthly_teu), 0) + 1, false);
TRUNCATE core.perf_terminal CASCADE;
INSERT INTO core.perf_terminal (id, code, full_name, operator, terminal_type, is_container, aliases, sort_order, created_at)
  SELECT id, code, full_name, operator, terminal_type, is_container, aliases, sort_order, created_at FROM jnpa.perf_terminals;
SELECT setval('core.perf_terminal_id_seq', coalesce((SELECT max(id) FROM core.perf_terminal), 0) + 1, false);
TRUNCATE core.perf_upload_error CASCADE;
INSERT INTO core.perf_upload_error (id, upload_id, row_number, column_name, error_code, error_detail, raw_value, created_at)
  SELECT id, upload_id, row_number, column_name, error_code, error_detail, raw_value, created_at FROM jnpa.perf_upload_errors;
SELECT setval('core.perf_upload_error_id_seq', coalesce((SELECT max(id) FROM core.perf_upload_error), 0) + 1, false);
TRUNCATE core.perf_upload CASCADE;
INSERT INTO core.perf_upload (id, upload_id, report_type, original_filename, file_size_bytes, status, uploaded_by, row_count, inserted_count, skipped_count, error_count, notes, created_at, completed_at, file_format, updated_count)
  SELECT id, upload_id, report_type, original_filename, file_size_bytes, status, uploaded_by, row_count, inserted_count, skipped_count, error_count, notes, created_at, completed_at, file_format, updated_count FROM jnpa.perf_uploads;
SELECT setval('core.perf_upload_id_seq', coalesce((SELECT max(id) FROM core.perf_upload), 0) + 1, false);
TRUNCATE core.push_subscription CASCADE;
INSERT INTO core.push_subscription (device_id, driver_id, vehicle_id, webpush, fcm_token, platform, created_at, updated_at)
  SELECT device_id, driver_id, vehicle_id, webpush, fcm_token, platform, created_at, updated_at FROM jnpa.push_subscriptions;
TRUNCATE core.reefer_slot CASCADE;
INSERT INTO core.reefer_slot (id, facility_id, slot_code, powered, status, container_number, set_temperature, current_temperature, updated_at)
  SELECT id, facility_id, slot_code, powered, status, container_number, set_temperature, current_temperature, updated_at FROM jnpa.reefer_slots;
SELECT setval('core.reefer_slot_id_seq', coalesce((SELECT max(id) FROM core.reefer_slot), 0) + 1, false);
TRUNCATE core.scenario_handle CASCADE;
INSERT INTO core.scenario_handle (handle_id, name, status, params, trace_id, started_at, ended_at)
  SELECT handle_id, name, status, params, trace_id, started_at, ended_at FROM jnpa.scenario_handles;
TRUNCATE core.scenario_step CASCADE;
INSERT INTO core.scenario_step (id, handle_id, step_no, ts, title, status, trigger, detail)
  SELECT id, handle_id, step_no, ts, title, status, trigger, detail FROM jnpa.scenario_steps;
SELECT setval('core.scenario_step_id_seq', coalesce((SELECT max(id) FROM core.scenario_step), 0) + 1, false);
TRUNCATE core.scenario CASCADE;
INSERT INTO core.scenario (id, name, started_at, ended_at, params)
  SELECT id, name, started_at, ended_at, params FROM jnpa.scenarios;
TRUNCATE core.ulip_service CASCADE;
INSERT INTO core.ulip_service (name, kind, base_url, healthy, enabled, registered_at, meta)
  SELECT name, kind, base_url, healthy, enabled, registered_at, meta FROM jnpa.services;
TRUNCATE core.sl_event CASCADE;
INSERT INTO core.sl_event (id, event, module, reference, container_no, payload, created_at)
  SELECT id, event, module, reference, container_no, payload, created_at FROM jnpa.sl_events;
SELECT setval('core.sl_event_id_seq', coalesce((SELECT max(id) FROM core.sl_event), 0) + 1, false);
TRUNCATE core.sl_import_error CASCADE;
INSERT INTO core.sl_import_error (id, import_file_id, record_ref, error_code, error_detail, created_at)
  SELECT id, import_file_id, record_ref, error_code, error_detail, created_at FROM jnpa.sl_import_errors;
SELECT setval('core.sl_import_error_id_seq', coalesce((SELECT max(id) FROM core.sl_import_error), 0) + 1, false);
TRUNCATE core.sl_import_file CASCADE;
INSERT INTO core.sl_import_file (id, list_type, terminal, physical_format, source_file, source_sha256, file_size_bytes, vessel_visit, voyage, line_code, direction, record_count, imported_count, error_count, import_status, error_detail, created_at, updated_at, uploaded_by, source)
  SELECT id, list_type, terminal, physical_format, source_file, source_sha256, file_size_bytes, vessel_visit, voyage, line_code, direction, record_count, imported_count, error_count, import_status, error_detail, created_at, updated_at, uploaded_by, source FROM jnpa.sl_import_files;
SELECT setval('core.sl_import_file_id_seq', coalesce((SELECT max(id) FROM core.sl_import_file), 0) + 1, false);
TRUNCATE core.tas_appointment CASCADE;
INSERT INTO core.tas_appointment (id, slot_code, gate_id, window_start, window_end, capacity, booked, status, source, created_at, updated_at)
  SELECT id, slot_code, gate_id, window_start, window_end, capacity, booked, status, source, created_at, updated_at FROM jnpa.tas_appointments;
SELECT setval('core.tas_appointment_id_seq', coalesce((SELECT max(id) FROM core.tas_appointment), 0) + 1, false);
TRUNCATE core.tas_booking CASCADE;
INSERT INTO core.tas_booking (id, appointment_id, slot_code, vehicle_id, driver_id, status, booked_at, updated_at)
  SELECT id, appointment_id, slot_code, vehicle_id, driver_id, status, booked_at, updated_at FROM jnpa.tas_bookings;
SELECT setval('core.tas_booking_id_seq', coalesce((SELECT max(id) FROM core.tas_booking), 0) + 1, false);
TRUNCATE core.td_import_error CASCADE;
INSERT INTO core.td_import_error (id, import_file_id, record_ref, error_code, error_detail, created_at)
  SELECT id, import_file_id, record_ref, error_code, error_detail, created_at FROM jnpa.td_import_errors;
SELECT setval('core.td_import_error_id_seq', coalesce((SELECT max(id) FROM core.td_import_error), 0) + 1, false);
TRUNCATE core.td_import_file CASCADE;
INSERT INTO core.td_import_file (id, entity_type, physical_format, source_file, source_sha256, file_size_bytes, record_count, imported_count, error_count, duplicate_count, import_status, error_detail, uploaded_by, source, created_at, updated_at)
  SELECT id, entity_type, physical_format, source_file, source_sha256, file_size_bytes, record_count, imported_count, error_count, duplicate_count, import_status, error_detail, uploaded_by, source, created_at, updated_at FROM jnpa.td_import_files;
SELECT setval('core.td_import_file_id_seq', coalesce((SELECT max(id) FROM core.td_import_file), 0) + 1, false);
TRUNCATE core.toll_enroute CASCADE;
INSERT INTO core.toll_enroute (id, client_id, source_state, source_name, destination_state, destination_name, vehicle_type, duration, distance, toll_plaza_details, created_at)
  SELECT id, client_id, source_state, source_name, destination_state, destination_name, vehicle_type, duration, distance, toll_plaza_details, created_at FROM jnpa.toll_enroute;
TRUNCATE core.traffic_snapshot CASCADE;
INSERT INTO core.traffic_snapshot (ts, segment_id, speed_kmh, jam_factor, source)
  SELECT ts, segment_id, speed_kmh, jam_factor, source FROM jnpa.traffic_snapshots;
TRUNCATE core.trailer_read CASCADE;
INSERT INTO core.trailer_read (id, ts, camera_id, gate_id, trailer_number, plate, vehicle_id, confidence, image_url, source, detail)
  SELECT id, ts, camera_id, gate_id, trailer_number, plate, vehicle_id, confidence, image_url, source, detail FROM jnpa.trailer_reads;
SELECT setval('core.trailer_read_id_seq', coalesce((SELECT max(id) FROM core.trailer_read), 0) + 1, false);
TRUNCATE core.transporter_blacklist CASCADE;
INSERT INTO core.transporter_blacklist (id, transporter_id, reason, severity, status, blacklisted_by, blacklisted_at, lifted_by, lifted_at, created_at)
  SELECT id, transporter_id, reason, severity, status, blacklisted_by, blacklisted_at, lifted_by, lifted_at, created_at FROM jnpa.transporter_blacklist;
SELECT setval('core.transporter_blacklist_id_seq', coalesce((SELECT max(id) FROM core.transporter_blacklist), 0) + 1, false);
TRUNCATE core.transporter_vehicle CASCADE;
INSERT INTO core.transporter_vehicle (id, transporter_id, vehicle_no, vehicle_no_norm, driver_id, created_at)
  SELECT id, transporter_id, vehicle_no, vehicle_no_norm, driver_id, created_at FROM jnpa.transporter_vehicles;
SELECT setval('core.transporter_vehicle_id_seq', coalesce((SELECT max(id) FROM core.transporter_vehicle), 0) + 1, false);
TRUNCATE core.trt_record CASCADE;
INSERT INTO core.trt_record (id, vehicle_id, plate, trip_id, gate_in_at, parking_at, loading_at, gate_out_at, gate_to_park_min, park_to_load_min, load_to_out_min, trt_min, status, source, detail, created_at, updated_at)
  SELECT id, vehicle_id, plate, trip_id, gate_in_at, parking_at, loading_at, gate_out_at, gate_to_park_min, park_to_load_min, load_to_out_min, trt_min, status, source, detail, created_at, updated_at FROM jnpa.trt_records;
SELECT setval('core.trt_record_id_seq', coalesce((SELECT max(id) FROM core.trt_record), 0) + 1, false);
TRUNCATE core.tt_trip CASCADE;
INSERT INTO core.tt_trip (id, cycle_id, vehicle_id, driver_id, trip_seq, direction, origin, destination, started_at, ended_at, laden, status, detail, created_at, updated_at)
  SELECT id, cycle_id, vehicle_id, driver_id, trip_seq, direction, origin, destination, started_at, ended_at, laden, status, detail, created_at, updated_at FROM jnpa.tt_trips;
SELECT setval('core.tt_trip_id_seq', coalesce((SELECT max(id) FROM core.tt_trip), 0) + 1, false);
TRUNCATE core.vehicle_rc CASCADE;
INSERT INTO core.vehicle_rc (plate, rc_type, owner_hash, fitness_valid_to, puc_valid_to, fastag_status, provisional, provisional_until, owner_name_masked, vehicle_class, fuel_type, insurance_valid_to, registration_date, state, rto_code, blacklist_status, updated_at)
  SELECT plate, rc_type, owner_hash, fitness_valid_to, puc_valid_to, fastag_status, provisional, provisional_until, owner_name_masked, vehicle_class, fuel_type, insurance_valid_to, registration_date, state, rto_code, blacklist_status, updated_at FROM jnpa.vehicle_master;
TRUNCATE core.vehicle_verification_history CASCADE;
INSERT INTO core.vehicle_verification_history (id, vehicle_number, request_payload, response_payload, verification_status, source, created_at)
  SELECT id, vehicle_number, request_payload, response_payload, verification_status, source, created_at FROM jnpa.vehicle_verification_history;
SELECT setval('core.vehicle_verification_history_id_seq', coalesce((SELECT max(id) FROM core.vehicle_verification_history), 0) + 1, false);
TRUNCATE core.verification_log CASCADE;
INSERT INTO core.verification_log (id, driver_id, decision, score, matched, provider, decision_path, actor, purpose, reason, ts)
  SELECT id, driver_id, decision, score, matched, provider, decision_path, actor, purpose, reason, ts FROM jnpa.verification_logs;
SELECT setval('core.verification_log_id_seq', coalesce((SELECT max(id) FROM core.verification_log), 0) + 1, false);
TRUNCATE core.violation_case CASCADE;
INSERT INTO core.violation_case (case_id, vehicle_number, driver_id, first_detected_at, last_updated_at, status, total_fine, evidence_url, evidence_sha256, gate_id, confidence)
  SELECT case_id, vehicle_number, driver_id, first_detected_at, last_updated_at, status, total_fine, evidence_url, evidence_sha256, gate_id, confidence FROM jnpa.violation_cases;
SELECT setval('core.challan_seq', (SELECT last_value FROM jnpa.challan_seq), true);
RESET session_replication_role;
