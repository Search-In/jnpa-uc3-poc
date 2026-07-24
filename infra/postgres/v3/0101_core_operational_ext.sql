-- ============================================================
-- 0101  JNPA UC3 operational extension: ported legacy tables
-- Generated from live jnpa catalog. Additive only.
-- Naming: singular, per architecture conventions.
-- ============================================================
BEGIN;

CREATE SEQUENCE IF NOT EXISTS core.accident_event_id_seq;
CREATE TABLE core.accident_event (
    id bigint DEFAULT nextval('core.accident_event_id_seq'::regclass) NOT NULL,
    accident_id bigint NOT NULL,
    action text NOT NULL,
    old_status text,
    new_status text,
    note text,
    actor text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT accident_event_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.accident_event_id_seq OWNED BY core.accident_event.id;
CREATE INDEX idx_accident_event_aid ON core.accident_event USING btree (accident_id, created_at);

CREATE SEQUENCE IF NOT EXISTS core.accident_id_seq;
CREATE TABLE core.accident (
    id bigint DEFAULT nextval('core.accident_id_seq'::regclass) NOT NULL,
    accident_ref text,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    accident_type text DEFAULT 'ENROUTE'::text NOT NULL,
    severity text DEFAULT 'MINOR'::text NOT NULL,
    lat double precision,
    lon double precision,
    location jsonb DEFAULT '{}'::jsonb NOT NULL,
    vehicle_id text,
    plate text,
    driver_id text,
    description text,
    status text DEFAULT 'REPORTED'::text NOT NULL,
    investigation_status text DEFAULT 'PENDING'::text NOT NULL,
    resolution text,
    reported_by text,
    source text DEFAULT 'MANUAL'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT accident_accident_type_check CHECK ((accident_type = ANY (ARRAY['PREMISES'::text, 'ENROUTE'::text]))),
    CONSTRAINT accident_investigation_status_check CHECK ((investigation_status = ANY (ARRAY['PENDING'::text, 'IN_PROGRESS'::text, 'COMPLETED'::text]))),
    CONSTRAINT accident_severity_check CHECK ((severity = ANY (ARRAY['MINOR'::text, 'MODERATE'::text, 'MAJOR'::text, 'FATAL'::text]))),
    CONSTRAINT accident_status_check CHECK ((status = ANY (ARRAY['REPORTED'::text, 'INVESTIGATING'::text, 'RESOLVED'::text, 'CLOSED'::text]))),
    CONSTRAINT accident_pkey PRIMARY KEY (id),
    CONSTRAINT accident_accident_ref_key UNIQUE (accident_ref)
);
ALTER SEQUENCE core.accident_id_seq OWNED BY core.accident.id;
CREATE INDEX idx_accident_occurred ON core.accident USING btree (occurred_at DESC);
CREATE INDEX idx_accident_status ON core.accident USING btree (status, occurred_at DESC);
CREATE INDEX idx_accident_vehicle ON core.accident USING btree (vehicle_id, occurred_at DESC);

CREATE TABLE core.alert (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    ts timestamp with time zone DEFAULT now(),
    kind text,
    severity text,
    gate_id text,
    plate text,
    payload jsonb,
    ack boolean DEFAULT false,
    CONSTRAINT alert_pkey PRIMARY KEY (id)
);
CREATE INDEX idx_alert_ts ON core.alert USING btree (ts DESC);
CREATE UNIQUE INDEX uq_alert_case_kind ON core.alert USING btree (((payload ->> 'case_id'::text)), kind) WHERE ((payload ->> 'source'::text) = 'violation-console'::text);

CREATE TABLE core.anpr_read (
    ts timestamp with time zone NOT NULL,
    camera_id text,
    plate text,
    conf real,
    vehicle_class text,
    image_url text,
    weather text,
    degraded boolean DEFAULT false
);
CREATE INDEX anpr_read_ts_idx ON core.anpr_read USING btree (ts DESC);
CREATE INDEX idx_anpr_plate_ts ON core.anpr_read USING btree (plate, ts DESC);

CREATE SEQUENCE IF NOT EXISTS core.api_audit_log_id_seq;
CREATE TABLE core.api_audit_log (
    id bigint DEFAULT nextval('core.api_audit_log_id_seq'::regclass) NOT NULL,
    service_name text NOT NULL,
    endpoint text,
    method text,
    request_payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    response_payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    status_code integer,
    latency_ms numeric(10,2),
    error text,
    transaction_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT api_audit_log_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.api_audit_log_id_seq OWNED BY core.api_audit_log.id;
CREATE INDEX idx_api_audit_service_ts ON core.api_audit_log USING btree (service_name, created_at DESC);
CREATE INDEX idx_api_audit_ts ON core.api_audit_log USING btree (created_at DESC);
CREATE INDEX idx_api_audit_txn ON core.api_audit_log USING btree (transaction_id);

CREATE SEQUENCE IF NOT EXISTS core.automation_execution_id_seq;
CREATE TABLE core.automation_execution (
    id bigint DEFAULT nextval('core.automation_execution_id_seq'::regclass) NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    event jsonb NOT NULL,
    results jsonb NOT NULL,
    matched_count integer DEFAULT 0 NOT NULL,
    CONSTRAINT automation_execution_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.automation_execution_id_seq OWNED BY core.automation_execution.id;

CREATE TABLE core.automation_rule (
    id text NOT NULL,
    name text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    field text NOT NULL,
    op text NOT NULL,
    value text NOT NULL,
    actions jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT automation_rule_pkey PRIMARY KEY (id)
);

CREATE SEQUENCE IF NOT EXISTS core.berthing_record_event_id_seq;
CREATE TABLE core.berthing_record_event (
    id bigint DEFAULT nextval('core.berthing_record_event_id_seq'::regclass) NOT NULL,
    berthing_id bigint NOT NULL,
    event_type text NOT NULL,
    event_time timestamp with time zone,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT berthing_record_event_pkey PRIMARY KEY (id),
    CONSTRAINT uq_berthing_event UNIQUE (berthing_id, event_type)
);
ALTER SEQUENCE core.berthing_record_event_id_seq OWNED BY core.berthing_record_event.id;
CREATE INDEX idx_berthing_event_call ON core.berthing_record_event USING btree (berthing_id, id);

CREATE SEQUENCE IF NOT EXISTS core.berthing_import_error_id_seq;
CREATE TABLE core.berthing_import_error (
    id bigint DEFAULT nextval('core.berthing_import_error_id_seq'::regclass) NOT NULL,
    import_file_id bigint NOT NULL,
    row_number integer,
    error_message text,
    raw_data text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT berthing_import_error_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.berthing_import_error_id_seq OWNED BY core.berthing_import_error.id;
CREATE INDEX idx_berthing_err_file ON core.berthing_import_error USING btree (import_file_id, id);

CREATE SEQUENCE IF NOT EXISTS core.berthing_import_file_id_seq;
CREATE TABLE core.berthing_import_file (
    id bigint DEFAULT nextval('core.berthing_import_file_id_seq'::regclass) NOT NULL,
    filename text,
    file_hash text,
    terminal text,
    physical_format text DEFAULT 'CSV'::text NOT NULL,
    uploaded_by text,
    status text DEFAULT 'PENDING'::text NOT NULL,
    total_rows integer DEFAULT 0 NOT NULL,
    success_rows integer DEFAULT 0 NOT NULL,
    failed_rows integer DEFAULT 0 NOT NULL,
    duplicate_rows integer DEFAULT 0 NOT NULL,
    source text DEFAULT 'UPLOAD'::text NOT NULL,
    error_detail text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT berthing_import_file_physical_format_check CHECK ((physical_format = ANY (ARRAY['CSV'::text, 'XLS'::text, 'XLSX'::text, 'PDF'::text]))),
    CONSTRAINT berthing_import_file_source_check CHECK ((source = ANY (ARRAY['DIRECTORY'::text, 'UPLOAD'::text]))),
    CONSTRAINT berthing_import_file_status_check CHECK ((status = ANY (ARRAY['PENDING'::text, 'SUCCESS'::text, 'PARTIAL'::text, 'FAILED'::text, 'SKIPPED_DUPLICATE'::text]))),
    CONSTRAINT berthing_import_file_pkey PRIMARY KEY (id),
    CONSTRAINT uq_berthing_import_file_hash UNIQUE (file_hash)
);
ALTER SEQUENCE core.berthing_import_file_id_seq OWNED BY core.berthing_import_file.id;
CREATE INDEX idx_berthing_file_source ON core.berthing_import_file USING btree (source, id DESC);
CREATE INDEX idx_berthing_file_status ON core.berthing_import_file USING btree (status, id DESC);
CREATE INDEX idx_berthing_file_terminal ON core.berthing_import_file USING btree (terminal, id DESC);

CREATE SEQUENCE IF NOT EXISTS core.berthing_report_document_id_seq;
CREATE TABLE core.berthing_report_document (
    id bigint DEFAULT nextval('core.berthing_report_document_id_seq'::regclass) NOT NULL,
    file_name text NOT NULL,
    terminal text,
    report_date date,
    pdf_hash text,
    page_count integer,
    table_count integer DEFAULT 0 NOT NULL,
    row_count integer DEFAULT 0 NOT NULL,
    uploaded_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT berthing_report_document_pkey PRIMARY KEY (id),
    CONSTRAINT uq_berthing_document_hash UNIQUE (pdf_hash)
);
ALTER SEQUENCE core.berthing_report_document_id_seq OWNED BY core.berthing_report_document.id;
CREATE INDEX idx_brdoc_created ON core.berthing_report_document USING btree (id DESC);
CREATE INDEX idx_brdoc_terminal ON core.berthing_report_document USING btree (terminal, id DESC);

CREATE SEQUENCE IF NOT EXISTS core.berthing_report_table_id_seq;
CREATE TABLE core.berthing_report_table (
    id bigint DEFAULT nextval('core.berthing_report_table_id_seq'::regclass) NOT NULL,
    document_id bigint NOT NULL,
    terminal text,
    table_name text NOT NULL,
    panel_index integer DEFAULT 0 NOT NULL,
    page_number integer DEFAULT 1 NOT NULL,
    original_columns jsonb NOT NULL,
    rows jsonb NOT NULL,
    row_count integer DEFAULT 0 NOT NULL,
    extraction_note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT berthing_report_table_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.berthing_report_table_id_seq OWNED BY core.berthing_report_table.id;
CREATE INDEX idx_brt_doc ON core.berthing_report_table USING btree (document_id, panel_index);
CREATE INDEX idx_brt_name ON core.berthing_report_table USING btree (terminal, table_name);

CREATE SEQUENCE IF NOT EXISTS core.berthing_record_id_seq;
CREATE TABLE core.berthing_record (
    id bigint DEFAULT nextval('core.berthing_record_id_seq'::regclass) NOT NULL,
    terminal text NOT NULL,
    vessel_name text NOT NULL,
    imo_number text,
    voyage_number text NOT NULL,
    shipping_line text,
    berth_number text,
    eta timestamp with time zone,
    ata timestamp with time zone,
    berthing_time timestamp with time zone,
    departure_time timestamp with time zone,
    cargo_operation_start timestamp with time zone,
    cargo_operation_end timestamp with time zone,
    status text DEFAULT 'EXPECTED'::text NOT NULL,
    source_file text,
    import_file_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT berthing_record_status_check CHECK ((status = ANY (ARRAY['EXPECTED'::text, 'ARRIVED'::text, 'BERTH_ASSIGNED'::text, 'BERTHING_STARTED'::text, 'CARGO_OPERATION'::text, 'COMPLETED'::text, 'DEPARTED'::text]))),
    CONSTRAINT berthing_record_pkey PRIMARY KEY (id),
    CONSTRAINT uq_berthing_call UNIQUE (terminal, voyage_number, vessel_name)
);
ALTER SEQUENCE core.berthing_record_id_seq OWNED BY core.berthing_record.id;
CREATE INDEX idx_berthing_eta ON core.berthing_record USING btree (eta DESC);
CREATE INDEX idx_berthing_import_file ON core.berthing_record USING btree (import_file_id);
CREATE INDEX idx_berthing_terminal_status ON core.berthing_record USING btree (terminal, status);
CREATE INDEX idx_berthing_vessel ON core.berthing_record USING btree (vessel_name);
CREATE INDEX idx_berthing_voyage ON core.berthing_record USING btree (voyage_number);

CREATE SEQUENCE IF NOT EXISTS core.bottleneck_snapshot_id_seq;
CREATE TABLE core.bottleneck_snapshot (
    id bigint DEFAULT nextval('core.bottleneck_snapshot_id_seq'::regclass) NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    rank integer NOT NULL,
    segment_id text NOT NULL,
    name text,
    jam_factor double precision DEFAULT 0.0 NOT NULL,
    speed_kmh double precision,
    free_flow_kmh double precision,
    avg_delay_min double precision,
    lat double precision,
    lon double precision,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT bottleneck_snapshot_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.bottleneck_snapshot_id_seq OWNED BY core.bottleneck_snapshot.id;
CREATE INDEX idx_bottleneck_seg ON core.bottleneck_snapshot USING btree (segment_id, ts DESC);
CREATE INDEX idx_bottleneck_ts ON core.bottleneck_snapshot USING btree (ts DESC, rank);

CREATE SEQUENCE IF NOT EXISTS core.camera_ai_count_id_seq;
CREATE TABLE core.camera_ai_count (
    id bigint DEFAULT nextval('core.camera_ai_count_id_seq'::regclass) NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    camera_id text,
    gate_id text,
    vehicle_count integer DEFAULT 0 NOT NULL,
    queue_count integer DEFAULT 0 NOT NULL,
    class_counts jsonb DEFAULT '{}'::jsonb NOT NULL,
    congestion_level text DEFAULT 'LOW'::text NOT NULL,
    confidence double precision DEFAULT 0.0 NOT NULL,
    source text DEFAULT 'CAMERA_AI'::text NOT NULL,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT camera_ai_count_congestion_level_check CHECK ((congestion_level = ANY (ARRAY['LOW'::text, 'MEDIUM'::text, 'HIGH'::text]))),
    CONSTRAINT camera_ai_count_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.camera_ai_count_id_seq OWNED BY core.camera_ai_count.id;
CREATE INDEX idx_cam_counts_cam_ts ON core.camera_ai_count USING btree (camera_id, ts DESC);
CREATE INDEX idx_cam_counts_gate_ts ON core.camera_ai_count USING btree (gate_id, ts DESC);

CREATE TABLE core.camera (
    id text NOT NULL,
    gate_id text,
    name text,
    lat double precision,
    lon double precision,
    role text,
    installed_at timestamp with time zone DEFAULT now(),
    CONSTRAINT camera_role_check CHECK ((role = ANY (ARRAY['entry'::text, 'exit'::text, 'overview'::text, 'ptz'::text, 'thermal'::text, 'anpr'::text]))),
    CONSTRAINT camera_pkey PRIMARY KEY (id)
);

CREATE SEQUENCE IF NOT EXISTS core.carbon_emission_id_seq;
CREATE TABLE core.carbon_emission (
    id bigint DEFAULT nextval('core.carbon_emission_id_seq'::regclass) NOT NULL,
    vehicle_id text NOT NULL,
    vehicle_type text,
    distance_km numeric,
    fuel_consumed_litre numeric,
    idle_time_minutes numeric,
    co2_kg numeric,
    source text,
    calculation_method text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT carbon_emission_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.carbon_emission_id_seq OWNED BY core.carbon_emission.id;
CREATE INDEX idx_carbon_emission_created ON core.carbon_emission USING btree (created_at DESC);
CREATE INDEX idx_carbon_emission_vehicle ON core.carbon_emission USING btree (vehicle_id, created_at DESC);

CREATE TABLE core.cargo (
    container_number text NOT NULL,
    vessel_name text,
    customs_status text DEFAULT 'PENDING'::text NOT NULL,
    yard_block text,
    is_released boolean DEFAULT false NOT NULL,
    vehicle_number text,
    gate text,
    camera_id text,
    eta timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    eseal_status text,
    eseal_number text,
    pre_document_status text,
    origin_stream text,
    workflow_status text,
    lifecycle_status text DEFAULT 'CREATED'::text,
    CONSTRAINT cargo_customs_status_check CHECK ((customs_status = ANY (ARRAY['PENDING'::text, 'CLEARED'::text, 'HELD'::text, 'UNDER_INSPECTION'::text]))),
    CONSTRAINT cargo_eseal_status_check CHECK ((eseal_status = ANY (ARRAY['ACTIVE'::text, 'ARMED'::text, 'TAMPERED'::text, 'REMOVED'::text, 'NONE'::text]))),
    CONSTRAINT cargo_lifecycle_status_check CHECK ((lifecycle_status = ANY (ARRAY['CREATED'::text, 'VESSEL_DISCHARGED'::text, 'YARD_ASSIGNED'::text, 'YARD_POSITION_ALLOCATED'::text, 'REEFER_PLANNED'::text, 'RAKE_ASSIGNED'::text, 'SCAN_PENDING'::text, 'VERIFIED'::text, 'RELEASED'::text]))),
    CONSTRAINT cargo_pre_document_status_check CHECK ((pre_document_status = ANY (ARRAY['NOT_STARTED'::text, 'PENDING'::text, 'IN_PROGRESS'::text, 'COMPLETED'::text]))),
    CONSTRAINT cargo_workflow_status_check CHECK ((workflow_status = ANY (ARRAY['TRIGGERED'::text, 'APPROVED'::text, 'REJECTED'::text]))),
    CONSTRAINT cargo_pkey PRIMARY KEY (container_number)
);
CREATE INDEX idx_cargo_customs_status ON core.cargo USING btree (customs_status);
CREATE INDEX idx_cargo_eseal_status ON core.cargo USING btree (eseal_status);
CREATE INDEX idx_cargo_eta ON core.cargo USING btree (eta DESC NULLS LAST);
CREATE INDEX idx_cargo_is_released ON core.cargo USING btree (is_released);
CREATE INDEX idx_cargo_lifecycle_status ON core.cargo USING btree (lifecycle_status);
CREATE INDEX idx_cargo_origin_stream ON core.cargo USING btree (origin_stream);
CREATE INDEX idx_cargo_pre_document_status ON core.cargo USING btree (pre_document_status);
CREATE INDEX idx_cargo_vehicle ON core.cargo USING btree (vehicle_number) WHERE (vehicle_number IS NOT NULL);
CREATE INDEX idx_cargo_yard_block ON core.cargo USING btree (yard_block);

CREATE SEQUENCE IF NOT EXISTS core.cargo_event_id_seq;
CREATE TABLE core.cargo_event (
    id bigint DEFAULT nextval('core.cargo_event_id_seq'::regclass) NOT NULL,
    event text NOT NULL,
    container_number text NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cargo_event_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.cargo_event_id_seq OWNED BY core.cargo_event.id;
CREATE INDEX idx_cargo_event_container ON core.cargo_event USING btree (container_number);
CREATE INDEX idx_cargo_event_created ON core.cargo_event USING btree (id DESC);
CREATE INDEX idx_cargo_event_event ON core.cargo_event USING btree (event);

CREATE SEQUENCE IF NOT EXISTS core.cargo_lifecycle_event_id_seq;
CREATE TABLE core.cargo_lifecycle_event (
    id bigint DEFAULT nextval('core.cargo_lifecycle_event_id_seq'::regclass) NOT NULL,
    container_number text NOT NULL,
    action text NOT NULL,
    old_status text,
    new_status text NOT NULL,
    actor_role text,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cargo_lifecycle_event_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.cargo_lifecycle_event_id_seq OWNED BY core.cargo_lifecycle_event.id;
CREATE INDEX idx_cargo_lifecycle_ev_container ON core.cargo_lifecycle_event USING btree (container_number);
CREATE INDEX idx_cargo_lifecycle_ev_created ON core.cargo_lifecycle_event USING btree (id DESC);

CREATE SEQUENCE IF NOT EXISTS core.cargo_notification_id_seq;
CREATE TABLE core.cargo_notification (
    id bigint DEFAULT nextval('core.cargo_notification_id_seq'::regclass) NOT NULL,
    container_number text NOT NULL,
    notification_type text NOT NULL,
    severity text DEFAULT 'MEDIUM'::text NOT NULL,
    message text,
    stakeholders jsonb DEFAULT '[]'::jsonb NOT NULL,
    status text DEFAULT 'CREATED'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cargo_notification_severity_check CHECK ((severity = ANY (ARRAY['LOW'::text, 'MEDIUM'::text, 'HIGH'::text, 'CRITICAL'::text]))),
    CONSTRAINT cargo_notification_status_check CHECK ((status = ANY (ARRAY['CREATED'::text, 'ACKNOWLEDGED'::text, 'RESOLVED'::text]))),
    CONSTRAINT cargo_notification_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.cargo_notification_id_seq OWNED BY core.cargo_notification.id;
CREATE INDEX idx_cargo_notif_container ON core.cargo_notification USING btree (container_number);
CREATE INDEX idx_cargo_notif_created ON core.cargo_notification USING btree (id DESC);
CREATE INDEX idx_cargo_notif_severity ON core.cargo_notification USING btree (severity);
CREATE INDEX idx_cargo_notif_status ON core.cargo_notification USING btree (status);
CREATE INDEX idx_cargo_notif_type ON core.cargo_notification USING btree (notification_type);

CREATE SEQUENCE IF NOT EXISTS core.cargo_rake_plan_id_seq;
CREATE TABLE core.cargo_rake_plan (
    id bigint DEFAULT nextval('core.cargo_rake_plan_id_seq'::regclass) NOT NULL,
    rake_id text NOT NULL,
    containers jsonb DEFAULT '[]'::jsonb NOT NULL,
    planned_containers integer DEFAULT 0 NOT NULL,
    status text DEFAULT 'PLANNED'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cargo_rake_plan_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.cargo_rake_plan_id_seq OWNED BY core.cargo_rake_plan.id;
CREATE INDEX idx_cargo_rake_plan_created ON core.cargo_rake_plan USING btree (id DESC);
CREATE INDEX idx_cargo_rake_plan_rake ON core.cargo_rake_plan USING btree (rake_id);

CREATE SEQUENCE IF NOT EXISTS core.cargo_reefer_plan_id_seq;
CREATE TABLE core.cargo_reefer_plan (
    id bigint DEFAULT nextval('core.cargo_reefer_plan_id_seq'::regclass) NOT NULL,
    container_number text NOT NULL,
    temperature numeric,
    power_required boolean DEFAULT true NOT NULL,
    slot text NOT NULL,
    status text DEFAULT 'ALLOCATED'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cargo_reefer_plan_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.cargo_reefer_plan_id_seq OWNED BY core.cargo_reefer_plan.id;
CREATE INDEX idx_cargo_reefer_plan_container ON core.cargo_reefer_plan USING btree (container_number);
CREATE INDEX idx_cargo_reefer_plan_created ON core.cargo_reefer_plan USING btree (id DESC);

CREATE SEQUENCE IF NOT EXISTS core.cargo_scan_verification_id_seq;
CREATE TABLE core.cargo_scan_verification (
    id bigint DEFAULT nextval('core.cargo_scan_verification_id_seq'::regclass) NOT NULL,
    container_number text NOT NULL,
    verified boolean DEFAULT true NOT NULL,
    remarks text,
    actor_role text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cargo_scan_verification_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.cargo_scan_verification_id_seq OWNED BY core.cargo_scan_verification.id;
CREATE INDEX idx_cargo_scan_verif_container ON core.cargo_scan_verification USING btree (container_number);
CREATE INDEX idx_cargo_scan_verif_created ON core.cargo_scan_verification USING btree (id DESC);

CREATE SEQUENCE IF NOT EXISTS core.cargo_workflow_event_id_seq;
CREATE TABLE core.cargo_workflow_event (
    id bigint DEFAULT nextval('core.cargo_workflow_event_id_seq'::regclass) NOT NULL,
    container_number text NOT NULL,
    action text NOT NULL,
    old_status text,
    new_status text,
    comment text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cargo_workflow_event_action_check CHECK ((action = ANY (ARRAY['TRIGGER'::text, 'APPROVE'::text, 'REJECT'::text]))),
    CONSTRAINT cargo_workflow_event_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.cargo_workflow_event_id_seq OWNED BY core.cargo_workflow_event.id;
CREATE INDEX idx_cargo_workflow_container ON core.cargo_workflow_event USING btree (container_number);
CREATE INDEX idx_cargo_workflow_created ON core.cargo_workflow_event USING btree (id DESC);

CREATE SEQUENCE IF NOT EXISTS core.cargo_yard_plan_id_seq;
CREATE TABLE core.cargo_yard_plan (
    id bigint DEFAULT nextval('core.cargo_yard_plan_id_seq'::regclass) NOT NULL,
    container_number text NOT NULL,
    preferred_block text,
    assigned_block text NOT NULL,
    priority text DEFAULT 'MEDIUM'::text NOT NULL,
    status text DEFAULT 'PLANNED'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    yard_row text,
    yard_slot text,
    yard_position text,
    CONSTRAINT cargo_yard_plan_priority_check CHECK ((priority = ANY (ARRAY['LOW'::text, 'MEDIUM'::text, 'HIGH'::text, 'CRITICAL'::text]))),
    CONSTRAINT cargo_yard_plan_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.cargo_yard_plan_id_seq OWNED BY core.cargo_yard_plan.id;
CREATE INDEX idx_cargo_yard_plan_block ON core.cargo_yard_plan USING btree (assigned_block);
CREATE INDEX idx_cargo_yard_plan_container ON core.cargo_yard_plan USING btree (container_number);
CREATE INDEX idx_cargo_yard_plan_created ON core.cargo_yard_plan USING btree (id DESC);

CREATE SEQUENCE IF NOT EXISTS core.case_audit_id_seq;
CREATE TABLE core.case_audit (
    id bigint DEFAULT nextval('core.case_audit_id_seq'::regclass) NOT NULL,
    case_id uuid NOT NULL,
    event text NOT NULL,
    from_status text,
    to_status text,
    actor text,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    prev_hash text,
    hash text,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT case_audit_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.case_audit_id_seq OWNED BY core.case_audit.id;
CREATE INDEX idx_case_audit_case ON core.case_audit USING btree (case_id, id);

CREATE SEQUENCE IF NOT EXISTS core.cfs_ecy_import_error_id_seq;
CREATE TABLE core.cfs_ecy_import_error (
    id bigint DEFAULT nextval('core.cfs_ecy_import_error_id_seq'::regclass) NOT NULL,
    import_file_id bigint NOT NULL,
    record_ref text,
    error_code text NOT NULL,
    error_detail text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cfs_ecy_import_error_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.cfs_ecy_import_error_id_seq OWNED BY core.cfs_ecy_import_error.id;
CREATE INDEX idx_cfsecy_err_file ON core.cfs_ecy_import_error USING btree (import_file_id, id);

CREATE SEQUENCE IF NOT EXISTS core.cfs_ecy_import_file_id_seq;
CREATE TABLE core.cfs_ecy_import_file (
    id bigint DEFAULT nextval('core.cfs_ecy_import_file_id_seq'::regclass) NOT NULL,
    facility_type text,
    physical_format text NOT NULL,
    source_file text,
    source_sha256 text,
    file_size_bytes bigint,
    record_count integer DEFAULT 0 NOT NULL,
    imported_count integer DEFAULT 0 NOT NULL,
    error_count integer DEFAULT 0 NOT NULL,
    duplicate_count integer DEFAULT 0 NOT NULL,
    import_status text DEFAULT 'PENDING'::text NOT NULL,
    error_detail text,
    uploaded_by text,
    source text DEFAULT 'UPLOAD'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cfs_ecy_import_file_facility_type_check CHECK ((facility_type = ANY (ARRAY['CFS'::text, 'ECY'::text]))),
    CONSTRAINT cfs_ecy_import_file_import_status_check CHECK ((import_status = ANY (ARRAY['PENDING'::text, 'SUCCESS'::text, 'PARTIAL'::text, 'FAILED'::text, 'SKIPPED_DUPLICATE'::text]))),
    CONSTRAINT cfs_ecy_import_file_physical_format_check CHECK ((physical_format = ANY (ARRAY['CSV'::text, 'XLS'::text, 'XLSX'::text]))),
    CONSTRAINT cfs_ecy_import_file_source_check CHECK ((source = ANY (ARRAY['DIRECTORY'::text, 'UPLOAD'::text]))),
    CONSTRAINT cfs_ecy_import_file_pkey PRIMARY KEY (id),
    CONSTRAINT uq_cfs_ecy_import_file_sha UNIQUE (source_sha256)
);
ALTER SEQUENCE core.cfs_ecy_import_file_id_seq OWNED BY core.cfs_ecy_import_file.id;
CREATE INDEX idx_cfsecy_file_facility ON core.cfs_ecy_import_file USING btree (facility_type, id DESC);
CREATE INDEX idx_cfsecy_file_source ON core.cfs_ecy_import_file USING btree (source, id DESC);
CREATE INDEX idx_cfsecy_file_status ON core.cfs_ecy_import_file USING btree (import_status, id DESC);

CREATE SEQUENCE IF NOT EXISTS core.cfs_ecy_movement_id_seq;
CREATE TABLE core.cfs_ecy_movement (
    id bigint DEFAULT nextval('core.cfs_ecy_movement_id_seq'::regclass) NOT NULL,
    facility_type text NOT NULL,
    container_number text NOT NULL,
    iso_valid boolean DEFAULT true NOT NULL,
    event_ts timestamp with time zone NOT NULL,
    mode text NOT NULL,
    source text DEFAULT 'CODECO'::text NOT NULL,
    source_file text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    import_file_id bigint,
    CONSTRAINT cfs_ecy_movement_facility_type_check CHECK ((facility_type = ANY (ARRAY['CFS'::text, 'ECY'::text]))),
    CONSTRAINT cfs_ecy_movement_mode_check CHECK ((mode = ANY (ARRAY['IN'::text, 'OUT'::text]))),
    CONSTRAINT cfs_ecy_movement_pkey PRIMARY KEY (id),
    CONSTRAINT uq_cfs_ecy_movement UNIQUE (facility_type, container_number, event_ts, mode)
);
ALTER SEQUENCE core.cfs_ecy_movement_id_seq OWNED BY core.cfs_ecy_movement.id;
CREATE INDEX idx_cfsecy_container ON core.cfs_ecy_movement USING btree (container_number, event_ts DESC);
CREATE INDEX idx_cfsecy_facility_mode_ts ON core.cfs_ecy_movement USING btree (facility_type, mode, event_ts DESC);
CREATE INDEX idx_cfsecy_facility_ts ON core.cfs_ecy_movement USING btree (facility_type, event_ts DESC);
CREATE INDEX idx_cfsecy_import_file ON core.cfs_ecy_movement USING btree (import_file_id);

CREATE TABLE core.challan (
    challan_id uuid NOT NULL,
    challan_no text,
    case_id uuid NOT NULL,
    vehicle_number text,
    total_fine integer DEFAULT 0 NOT NULL,
    status text DEFAULT 'ISSUED'::text NOT NULL,
    mva_section text,
    issued_at timestamp with time zone DEFAULT now() NOT NULL,
    payment_ref text,
    pdf_url text,
    evidence_sha256 text,
    created_by text,
    CONSTRAINT challan_status_check CHECK ((status = ANY (ARRAY['ISSUED'::text, 'PAID'::text, 'DISPUTED'::text, 'CLOSED'::text]))),
    CONSTRAINT challan_pkey PRIMARY KEY (challan_id),
    CONSTRAINT challan_case_id_key UNIQUE (case_id),
    CONSTRAINT challan_challan_no_key UNIQUE (challan_no)
);
CREATE INDEX idx_challan_case ON core.challan USING btree (case_id);

CREATE SEQUENCE IF NOT EXISTS core.container_movement_history_id_seq;
CREATE TABLE core.container_movement_history (
    id bigint DEFAULT nextval('core.container_movement_history_id_seq'::regclass) NOT NULL,
    container_id text,
    allocation_id bigint,
    movement_type text NOT NULL,
    location text,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT container_movement_history_movement_type_check CHECK ((movement_type = ANY (ARRAY['PICKUP'::text, 'ALLOCATION'::text, 'TRANSFER'::text, 'DELIVERY'::text, 'COMPLETION'::text]))),
    CONSTRAINT container_movement_history_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.container_movement_history_id_seq OWNED BY core.container_movement_history.id;
CREATE INDEX idx_container_move_container ON core.container_movement_history USING btree (container_id, created_at DESC);
CREATE INDEX idx_container_move_type ON core.container_movement_history USING btree (movement_type, created_at DESC);

CREATE SEQUENCE IF NOT EXISTS core.container_read_id_seq;
CREATE TABLE core.container_read (
    id bigint DEFAULT nextval('core.container_read_id_seq'::regclass) NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    camera_id text,
    gate_id text,
    container_number text,
    iso_type text,
    check_digit_ok boolean,
    valid boolean DEFAULT false NOT NULL,
    plate text,
    vehicle_id text,
    confidence double precision DEFAULT 0.0 NOT NULL,
    image_url text,
    source text DEFAULT 'OCR'::text NOT NULL,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT container_read_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.container_read_id_seq OWNED BY core.container_read.id;
CREATE INDEX idx_container_read_num ON core.container_read USING btree (container_number, ts DESC);
CREATE INDEX idx_container_read_ts ON core.container_read USING btree (ts DESC);

CREATE SEQUENCE IF NOT EXISTS core.customs_event_id_seq;
CREATE TABLE core.customs_event (
    id bigint DEFAULT nextval('core.customs_event_id_seq'::regclass) NOT NULL,
    event text NOT NULL,
    module text,
    reference text,
    container_no text,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT customs_event_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.customs_event_id_seq OWNED BY core.customs_event.id;
CREATE INDEX idx_customs_event_cont ON core.customs_event USING btree (container_no, id DESC);
CREATE INDEX idx_customs_event_event ON core.customs_event USING btree (event, id DESC);
CREATE INDEX idx_customs_event_id ON core.customs_event USING btree (id DESC);
CREATE INDEX idx_customs_event_module ON core.customs_event USING btree (module, id DESC);

CREATE SEQUENCE IF NOT EXISTS core.customs_import_error_id_seq;
CREATE TABLE core.customs_import_error (
    id bigint DEFAULT nextval('core.customs_import_error_id_seq'::regclass) NOT NULL,
    message_id bigint NOT NULL,
    record_ref text,
    error_code text NOT NULL,
    error_detail text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT customs_import_error_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.customs_import_error_id_seq OWNED BY core.customs_import_error.id;
CREATE INDEX idx_customs_import_err_msg ON core.customs_import_error USING btree (message_id, id);

CREATE SEQUENCE IF NOT EXISTS core.customs_message_id_seq;
CREATE TABLE core.customs_message (
    id bigint DEFAULT nextval('core.customs_message_id_seq'::regclass) NOT NULL,
    message_type text NOT NULL,
    module text NOT NULL,
    control_number text,
    sender_id text,
    receiver_id text,
    message_id_code text,
    sent_ts timestamp with time zone,
    primary_ref text,
    source_file text NOT NULL,
    source_sha256 text NOT NULL,
    file_size_bytes bigint,
    record_count integer DEFAULT 0 NOT NULL,
    imported_count integer DEFAULT 0 NOT NULL,
    error_count integer DEFAULT 0 NOT NULL,
    import_status text DEFAULT 'PENDING'::text NOT NULL,
    error_detail text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT customs_message_import_status_check CHECK ((import_status = ANY (ARRAY['PENDING'::text, 'SUCCESS'::text, 'PARTIAL'::text, 'FAILED'::text, 'SKIPPED_DUPLICATE'::text]))),
    CONSTRAINT customs_message_message_type_check CHECK ((message_type = ANY (ARRAY['CHPOI03'::text, 'CHPOI10'::text, 'CHPOI13'::text, 'RMS'::text, 'LEO'::text, 'SHIPPING_BILL'::text]))),
    CONSTRAINT customs_message_module_check CHECK ((module = ANY (ARRAY['IGM'::text, 'OOC'::text, 'SMTP'::text, 'RMS'::text, 'LEO'::text, 'SHIPPING_BILL'::text]))),
    CONSTRAINT customs_message_pkey PRIMARY KEY (id),
    CONSTRAINT uq_customs_message_sha UNIQUE (source_sha256)
);
ALTER SEQUENCE core.customs_message_id_seq OWNED BY core.customs_message.id;
CREATE INDEX idx_customs_msg_module ON core.customs_message USING btree (module, id DESC);
CREATE INDEX idx_customs_msg_ref ON core.customs_message USING btree (primary_ref);
CREATE INDEX idx_customs_msg_status ON core.customs_message USING btree (import_status, id DESC);
CREATE INDEX idx_customs_msg_type ON core.customs_message USING btree (message_type, id DESC);

CREATE SEQUENCE IF NOT EXISTS core.decision_audit_id_seq;
CREATE TABLE core.decision_audit (
    id bigint DEFAULT nextval('core.decision_audit_id_seq'::regclass) NOT NULL,
    request_id text,
    input_data jsonb DEFAULT '{}'::jsonb NOT NULL,
    rule_executed text,
    decision text,
    action_taken text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT decision_audit_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.decision_audit_id_seq OWNED BY core.decision_audit.id;
CREATE INDEX idx_decision_audit_request ON core.decision_audit USING btree (request_id);
CREATE INDEX idx_decision_audit_rule ON core.decision_audit USING btree (rule_executed, created_at DESC);
CREATE INDEX idx_decision_audit_ts ON core.decision_audit USING btree (created_at DESC);

CREATE TABLE core.device_binding (
    device_id text NOT NULL,
    mobile text NOT NULL,
    driver_id text,
    bound_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL,
    active boolean DEFAULT true NOT NULL,
    CONSTRAINT device_binding_pkey PRIMARY KEY (device_id)
);
CREATE INDEX idx_device_binding_mobile ON core.device_binding USING btree (mobile);

CREATE SEQUENCE IF NOT EXISTS core.digital_twin_event_id_seq;
CREATE TABLE core.digital_twin_event (
    id bigint DEFAULT nextval('core.digital_twin_event_id_seq'::regclass) NOT NULL,
    event_type text NOT NULL,
    vehicle_id text,
    driver_id text,
    location jsonb DEFAULT '{}'::jsonb NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT digital_twin_event_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.digital_twin_event_id_seq OWNED BY core.digital_twin_event.id;
CREATE INDEX idx_dt_events_driver_ts ON core.digital_twin_event USING btree (driver_id, created_at DESC);
CREATE INDEX idx_dt_events_ts ON core.digital_twin_event USING btree (created_at DESC);
CREATE INDEX idx_dt_events_type_ts ON core.digital_twin_event USING btree (event_type, created_at DESC);
CREATE INDEX idx_dt_events_vehicle_ts ON core.digital_twin_event USING btree (vehicle_id, created_at DESC);

CREATE SEQUENCE IF NOT EXISTS core.document_ocr_id_seq;
CREATE TABLE core.document_ocr (
    id bigint DEFAULT nextval('core.document_ocr_id_seq'::regclass) NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    doc_type text DEFAULT 'UNKNOWN'::text NOT NULL,
    source_ref text,
    storage_url text,
    raw_text text,
    fields jsonb DEFAULT '{}'::jsonb NOT NULL,
    confidence double precision DEFAULT 0.0 NOT NULL,
    status text DEFAULT 'EXTRACTED'::text NOT NULL,
    source text DEFAULT 'OCR'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT document_ocr_status_check CHECK ((status = ANY (ARRAY['UPLOADED'::text, 'EXTRACTED'::text, 'VERIFIED'::text, 'FAILED'::text]))),
    CONSTRAINT document_ocr_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.document_ocr_id_seq OWNED BY core.document_ocr.id;
CREATE INDEX idx_document_ocr_ts ON core.document_ocr USING btree (ts DESC);
CREATE INDEX idx_document_ocr_type ON core.document_ocr USING btree (doc_type, ts DESC);

CREATE TABLE core.driver_enrollment (
    driver_id text NOT NULL,
    name text NOT NULL,
    license_no text,
    mobile text,
    vehicle_no text,
    aadhaar_masked text,
    emergency_contact text,
    status text DEFAULT 'PENDING'::text NOT NULL,
    consent boolean DEFAULT false NOT NULL,
    consent_at timestamp with time zone,
    face_images jsonb DEFAULT '[]'::jsonb NOT NULL,
    reference_image text,
    photo_url text,
    documents jsonb DEFAULT '[]'::jsonb NOT NULL,
    template_dim integer,
    provider text,
    submitted_at timestamp with time zone DEFAULT now() NOT NULL,
    reviewed_at timestamp with time zone,
    reviewed_by text,
    rejection_reason text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text,
    source text DEFAULT 'PWA'::text NOT NULL,
    CONSTRAINT driver_enrollment_status_check CHECK ((status = ANY (ARRAY['PENDING'::text, 'ACTIVE'::text, 'REJECTED'::text, 'REENROLL'::text]))),
    CONSTRAINT driver_enrollment_pkey PRIMARY KEY (driver_id)
);
CREATE INDEX idx_driver_enrol_status ON core.driver_enrollment USING btree (status, submitted_at DESC);

CREATE TABLE core.driver_face (
    driver_id text NOT NULL,
    embedding jsonb NOT NULL,
    dim integer NOT NULL,
    provider text,
    model_version text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT driver_face_pkey PRIMARY KEY (driver_id)
);

CREATE SEQUENCE IF NOT EXISTS core.driver_license_lookup_history_id_seq;
CREATE TABLE core.driver_license_lookup_history (
    id bigint DEFAULT nextval('core.driver_license_lookup_history_id_seq'::regclass) NOT NULL,
    dl_number text,
    request_payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    response_payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text,
    source text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT driver_license_lookup_history_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.driver_license_lookup_history_id_seq OWNED BY core.driver_license_lookup_history.id;
CREATE INDEX idx_dl_lookup_number ON core.driver_license_lookup_history USING btree (dl_number, created_at DESC);
CREATE INDEX idx_dl_lookup_ts ON core.driver_license_lookup_history USING btree (created_at DESC);

CREATE TABLE core.driver_identity (
    driver_id text NOT NULL,
    name text NOT NULL,
    license_no text,
    mobile text,
    vehicle_no text,
    aadhaar_masked text,
    emergency_contact text,
    status text DEFAULT 'ACTIVE'::text NOT NULL,
    photo_url text,
    reference_image text,
    template_dim integer,
    provider text,
    enrolled_at timestamp with time zone DEFAULT now() NOT NULL,
    approved_by text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text,
    vehicle_no_norm text,
    CONSTRAINT driver_identity_status_check CHECK ((status = ANY (ARRAY['ACTIVE'::text, 'SUSPENDED'::text]))),
    CONSTRAINT driver_identity_pkey PRIMARY KEY (driver_id)
);
CREATE INDEX idx_driver_identity_vehicle_no ON core.driver_identity USING btree (vehicle_no);
CREATE INDEX idx_driver_identity_vehicle_no_norm ON core.driver_identity USING btree (vehicle_no_norm);
CREATE UNIQUE INDEX uq_driver_identity_vehicle_active ON core.driver_identity USING btree (vehicle_no_norm) WHERE ((status = 'ACTIVE'::text) AND (vehicle_no_norm IS NOT NULL));

CREATE SEQUENCE IF NOT EXISTS core.empty_container_allocation_id_seq;
CREATE TABLE core.empty_container_allocation (
    id bigint DEFAULT nextval('core.empty_container_allocation_id_seq'::regclass) NOT NULL,
    container_id text,
    truck_id text,
    trailer_id text,
    driver_id text,
    shipping_line text,
    cfs text,
    ecd text,
    allocation_reason text,
    allocated_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'ALLOCATED'::text NOT NULL,
    CONSTRAINT empty_container_allocation_status_check CHECK ((status = ANY (ARRAY['ALLOCATED'::text, 'PICKED_UP'::text, 'IN_TRANSIT'::text, 'DELIVERED'::text, 'COMPLETED'::text, 'CANCELLED'::text]))),
    CONSTRAINT empty_container_allocation_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.empty_container_allocation_id_seq OWNED BY core.empty_container_allocation.id;
CREATE INDEX idx_ec_alloc_container ON core.empty_container_allocation USING btree (container_id, allocated_at DESC);
CREATE INDEX idx_ec_alloc_status ON core.empty_container_allocation USING btree (status, allocated_at DESC);
CREATE INDEX idx_ec_alloc_ts ON core.empty_container_allocation USING btree (allocated_at DESC);

CREATE TABLE core.empty_container_inventory (
    container_id text NOT NULL,
    container_type text,
    location text,
    owner text,
    availability_status text DEFAULT 'AVAILABLE'::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT empty_container_inventory_availability_status_check CHECK ((availability_status = ANY (ARRAY['AVAILABLE'::text, 'ALLOCATED'::text, 'IN_TRANSIT'::text, 'DELIVERED'::text]))),
    CONSTRAINT empty_container_inventory_pkey PRIMARY KEY (container_id)
);
CREATE INDEX idx_ec_inventory_location ON core.empty_container_inventory USING btree (location);
CREATE INDEX idx_ec_inventory_status ON core.empty_container_inventory USING btree (availability_status, container_type);

CREATE SEQUENCE IF NOT EXISTS core.enrollment_audit_id_seq;
CREATE TABLE core.enrollment_audit (
    id bigint DEFAULT nextval('core.enrollment_audit_id_seq'::regclass) NOT NULL,
    driver_id text NOT NULL,
    event text NOT NULL,
    actor text,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT enrollment_audit_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.enrollment_audit_id_seq OWNED BY core.enrollment_audit.id;
CREATE INDEX idx_enrollment_audit_driver ON core.enrollment_audit USING btree (driver_id, ts DESC);

CREATE TABLE core.fastag_balance (
    rc_number text NOT NULL,
    tag_id text,
    provider_name text,
    provider_code text,
    customer_name text,
    available_recharge_limit numeric(10,2),
    available_balance numeric(10,2),
    tag_status text,
    vehicle_class text,
    vehicle_class_desc text,
    model_name text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT fastag_balance_pkey PRIMARY KEY (rc_number)
);
CREATE INDEX idx_fastag_balance_tag ON core.fastag_balance USING btree (tag_id);

CREATE TABLE core.fastag_transaction (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tag_id text,
    rc_number text,
    seq_no text,
    transaction_date_time timestamp with time zone,
    lane_direction text,
    toll_plaza_name text,
    toll_plaza_geocode text,
    vehicle_type text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    bank_name text,
    status text,
    CONSTRAINT fastag_transaction_pkey PRIMARY KEY (id),
    CONSTRAINT fastag_transaction_seq_no_key UNIQUE (seq_no)
);
CREATE INDEX idx_fastag_txn_rc ON core.fastag_transaction USING btree (rc_number, transaction_date_time DESC);
CREATE INDEX idx_fastag_txn_tag ON core.fastag_transaction USING btree (tag_id, transaction_date_time DESC);

CREATE SEQUENCE IF NOT EXISTS core.gate_capture_id_seq;
CREATE TABLE core.gate_capture (
    id bigint DEFAULT nextval('core.gate_capture_id_seq'::regclass) NOT NULL,
    capture_type text NOT NULL,
    container_no text,
    vehicle_plate text,
    gate_id text,
    source_mode text DEFAULT 'sim'::text NOT NULL,
    status text,
    captured_at timestamp with time zone,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT gate_capture_capture_type_check CHECK ((capture_type = ANY (ARRAY['ESEAL'::text, 'FORM13'::text, 'WEIGHBRIDGE'::text, 'ICEGATE'::text]))),
    CONSTRAINT gate_capture_pkey PRIMARY KEY (id),
    CONSTRAINT gate_capture_container_no_capture_type_captured_at_key UNIQUE (container_no, capture_type, captured_at)
);
ALTER SEQUENCE core.gate_capture_id_seq OWNED BY core.gate_capture.id;
CREATE INDEX idx_gate_capture_container ON core.gate_capture USING btree (container_no);
CREATE INDEX idx_gate_capture_plate ON core.gate_capture USING btree (vehicle_plate);
CREATE INDEX idx_gate_capture_ts ON core.gate_capture USING btree (created_at DESC);
CREATE INDEX idx_gate_capture_type_ts ON core.gate_capture USING btree (capture_type, created_at DESC);

CREATE SEQUENCE IF NOT EXISTS core.gate_event_id_seq;
CREATE TABLE core.gate_event (
    id bigint DEFAULT nextval('core.gate_event_id_seq'::regclass) NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    device_id text NOT NULL,
    plate text,
    gate_id text,
    trip_id text NOT NULL,
    event_type text NOT NULL,
    lat double precision,
    lon double precision,
    CONSTRAINT gate_event_event_type_check CHECK ((event_type = ANY (ARRAY['GATE_ARRIVAL'::text, 'GATE_TXN_START'::text, 'GATE_IN'::text, 'GATE_OUT'::text]))),
    CONSTRAINT gate_event_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.gate_event_id_seq OWNED BY core.gate_event.id;
CREATE INDEX idx_gate_event_trip ON core.gate_event USING btree (trip_id);
CREATE INDEX idx_gate_event_ts ON core.gate_event USING btree (ts DESC);
CREATE INDEX idx_gate_event_type_ts ON core.gate_event USING btree (event_type, ts DESC);

CREATE TABLE core.gate (
    id text NOT NULL,
    name text,
    lat double precision,
    lon double precision,
    closed_at timestamp with time zone,
    CONSTRAINT gate_pkey PRIMARY KEY (id)
);

CREATE SEQUENCE IF NOT EXISTS core.geofence_event_id_seq;
CREATE TABLE core.geofence_event (
    id bigint DEFAULT nextval('core.geofence_event_id_seq'::regclass) NOT NULL,
    vehicle_id text,
    zone_id text,
    entry_time timestamp with time zone,
    exit_time timestamp with time zone,
    violation_type text,
    action_taken text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    driver_id text,
    event_type text NOT NULL,
    dwell_seconds integer,
    CONSTRAINT geofence_event_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.geofence_event_id_seq OWNED BY core.geofence_event.id;
CREATE INDEX idx_geofence_event_driver ON core.geofence_event USING btree (driver_id, created_at DESC);
CREATE INDEX idx_geofence_event_ts ON core.geofence_event USING btree (created_at DESC);
CREATE INDEX idx_geofence_event_type ON core.geofence_event USING btree (event_type, created_at DESC);
CREATE INDEX idx_geofence_event_vehicle ON core.geofence_event USING btree (vehicle_id, entry_time DESC);
CREATE INDEX idx_geofence_event_zone ON core.geofence_event USING btree (zone_id, entry_time DESC);

CREATE TABLE core.geofence_zone (
    id text NOT NULL,
    name text NOT NULL,
    kind text DEFAULT 'no_parking'::text NOT NULL,
    polygon jsonb NOT NULL,
    escalation jsonb DEFAULT '{"warn_min": 5, "notice_min": 15, "challan_min": 30}'::jsonb NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT geofence_zone_kind_check CHECK ((kind = ANY (ARRAY['no_parking'::text, 'restricted'::text]))),
    CONSTRAINT geofence_zone_pkey PRIMARY KEY (id)
);

CREATE SEQUENCE IF NOT EXISTS core.integration_lookup_id_seq;
CREATE TABLE core.integration_lookup (
    id bigint DEFAULT nextval('core.integration_lookup_id_seq'::regclass) NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    system text NOT NULL,
    op text NOT NULL,
    ref text,
    request jsonb DEFAULT '{}'::jsonb NOT NULL,
    response jsonb DEFAULT '{}'::jsonb NOT NULL,
    source text DEFAULT 'MOCK'::text NOT NULL,
    latency_ms integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT integration_lookup_source_check CHECK ((source = ANY (ARRAY['LIVE'::text, 'MOCK'::text, 'ERROR'::text]))),
    CONSTRAINT integration_lookup_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.integration_lookup_id_seq OWNED BY core.integration_lookup.id;
CREATE INDEX idx_integration_sys_ts ON core.integration_lookup USING btree (system, ts DESC);

CREATE SEQUENCE IF NOT EXISTS core.ldb_movement_id_seq;
CREATE TABLE core.ldb_movement (
    id bigint DEFAULT nextval('core.ldb_movement_id_seq'::regclass) NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    container_number text NOT NULL,
    event text NOT NULL,
    location text,
    terminal text,
    mode text,
    source text DEFAULT 'MOCK'::text NOT NULL,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT ldb_movement_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.ldb_movement_id_seq OWNED BY core.ldb_movement.id;
CREATE INDEX idx_ldb_container_ts ON core.ldb_movement USING btree (container_number, ts DESC);

CREATE SEQUENCE IF NOT EXISTS core.leo_reconciliation_id_seq;
CREATE TABLE core.leo_reconciliation (
    id bigint DEFAULT nextval('core.leo_reconciliation_id_seq'::regclass) NOT NULL,
    container_no text,
    vehicle_plate text,
    leo_ready boolean DEFAULT false NOT NULL,
    customs_flags jsonb DEFAULT '[]'::jsonb NOT NULL,
    checks jsonb DEFAULT '{}'::jsonb NOT NULL,
    source_mode text DEFAULT 'sim'::text NOT NULL,
    reconciled_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT leo_reconciliation_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.leo_reconciliation_id_seq OWNED BY core.leo_reconciliation.id;
CREATE INDEX idx_leo_recon_container ON core.leo_reconciliation USING btree (container_no, reconciled_at DESC);
CREATE INDEX idx_leo_recon_ready ON core.leo_reconciliation USING btree (leo_ready, reconciled_at DESC);
CREATE INDEX idx_leo_recon_ts ON core.leo_reconciliation USING btree (reconciled_at DESC);

CREATE SEQUENCE IF NOT EXISTS core.notification_id_seq;
CREATE TABLE core.notification (
    id bigint DEFAULT nextval('core.notification_id_seq'::regclass) NOT NULL,
    event_id text,
    channel text NOT NULL,
    receiver text,
    message text,
    delivery_status text DEFAULT 'PENDING'::text NOT NULL,
    provider_response jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT notification_delivery_status_check CHECK ((delivery_status = ANY (ARRAY['PENDING'::text, 'SENT'::text, 'DELIVERED'::text, 'FAILED'::text, 'SKIPPED'::text, 'NO_SUBSCRIPTION'::text]))),
    CONSTRAINT notification_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.notification_id_seq OWNED BY core.notification.id;
CREATE INDEX idx_notification_event ON core.notification USING btree (event_id);
CREATE INDEX idx_notification_receiver ON core.notification USING btree (receiver, created_at DESC);
CREATE INDEX idx_notification_status ON core.notification USING btree (delivery_status, created_at DESC);
CREATE INDEX idx_notification_ts ON core.notification USING btree (created_at DESC);

CREATE SEQUENCE IF NOT EXISTS core.nvr_camera_map_id_seq;
CREATE TABLE core.nvr_camera_map (
    id bigint DEFAULT nextval('core.nvr_camera_map_id_seq'::regclass) NOT NULL,
    nvr_id text NOT NULL,
    channel integer NOT NULL,
    camera_id text,
    stream_url text,
    codec text DEFAULT 'H264'::text NOT NULL,
    resolution text DEFAULT '1920x1080'::text NOT NULL,
    fps integer DEFAULT 25 NOT NULL,
    status text DEFAULT 'UNKNOWN'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT nvr_camera_map_pkey PRIMARY KEY (id),
    CONSTRAINT nvr_camera_map_nvr_id_channel_key UNIQUE (nvr_id, channel)
);
ALTER SEQUENCE core.nvr_camera_map_id_seq OWNED BY core.nvr_camera_map.id;
CREATE INDEX idx_nvr_map_camera ON core.nvr_camera_map USING btree (camera_id);

CREATE TABLE core.nvr_device (
    id text NOT NULL,
    name text NOT NULL,
    vendor text,
    host text,
    port integer DEFAULT 554 NOT NULL,
    protocol text DEFAULT 'RTSP'::text NOT NULL,
    channels integer DEFAULT 0 NOT NULL,
    location jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'UNKNOWN'::text NOT NULL,
    source text DEFAULT 'CONFIG'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT nvr_device_protocol_check CHECK ((protocol = ANY (ARRAY['RTSP'::text, 'ONVIF'::text, 'HTTP'::text]))),
    CONSTRAINT nvr_device_status_check CHECK ((status = ANY (ARRAY['ONLINE'::text, 'OFFLINE'::text, 'DEGRADED'::text, 'UNKNOWN'::text]))),
    CONSTRAINT nvr_device_pkey PRIMARY KEY (id)
);

CREATE SEQUENCE IF NOT EXISTS core.otp_request_id_seq;
CREATE TABLE core.otp_request (
    id bigint DEFAULT nextval('core.otp_request_id_seq'::regclass) NOT NULL,
    mobile text NOT NULL,
    device_id text,
    code_hash text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    verified boolean DEFAULT false NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT otp_request_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.otp_request_id_seq OWNED BY core.otp_request.id;
CREATE INDEX idx_otp_mobile ON core.otp_request USING btree (mobile, created_at DESC);

CREATE SEQUENCE IF NOT EXISTS core.parking_event_id_seq;
CREATE TABLE core.parking_event (
    id bigint DEFAULT nextval('core.parking_event_id_seq'::regclass) NOT NULL,
    event_type text NOT NULL,
    vehicle_id text,
    driver_id text,
    facility_id text,
    slot_id bigint,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT parking_event_event_type_check CHECK ((event_type = ANY (ARRAY['ALLOCATION'::text, 'RELEASE'::text, 'OVERFLOW'::text, 'ILLEGAL_PARKING'::text, 'NO_PARKING_VIOLATION'::text]))),
    CONSTRAINT parking_event_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.parking_event_id_seq OWNED BY core.parking_event.id;
CREATE INDEX idx_parking_event_ts ON core.parking_event USING btree (created_at DESC);
CREATE INDEX idx_parking_event_type ON core.parking_event USING btree (event_type, created_at DESC);
CREATE INDEX idx_parking_event_vehicle ON core.parking_event USING btree (vehicle_id, created_at DESC);

CREATE TABLE core.parking_facility (
    id text NOT NULL,
    facility_name text NOT NULL,
    location jsonb DEFAULT '{}'::jsonb NOT NULL,
    capacity integer DEFAULT 0 NOT NULL,
    status text DEFAULT 'OPEN'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT parking_facility_pkey PRIMARY KEY (id)
);

CREATE SEQUENCE IF NOT EXISTS core.parking_slot_id_seq;
CREATE TABLE core.parking_slot (
    id bigint DEFAULT nextval('core.parking_slot_id_seq'::regclass) NOT NULL,
    facility_id text NOT NULL,
    slot_number text NOT NULL,
    availability_status text DEFAULT 'AVAILABLE'::text NOT NULL,
    vehicle_id text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT parking_slot_availability_status_check CHECK ((availability_status = ANY (ARRAY['AVAILABLE'::text, 'OCCUPIED'::text, 'RESERVED'::text, 'OUT_OF_SERVICE'::text]))),
    CONSTRAINT parking_slot_pkey PRIMARY KEY (id),
    CONSTRAINT parking_slot_facility_id_slot_number_key UNIQUE (facility_id, slot_number)
);
ALTER SEQUENCE core.parking_slot_id_seq OWNED BY core.parking_slot.id;
CREATE INDEX idx_parking_slot_facility ON core.parking_slot USING btree (facility_id, availability_status);
CREATE INDEX idx_parking_slot_vehicle ON core.parking_slot USING btree (vehicle_id);

CREATE SEQUENCE IF NOT EXISTS core.parking_transaction_id_seq;
CREATE TABLE core.parking_transaction (
    id bigint DEFAULT nextval('core.parking_transaction_id_seq'::regclass) NOT NULL,
    vehicle_id text,
    driver_id text,
    facility_id text,
    slot_id bigint,
    entry_time timestamp with time zone DEFAULT now() NOT NULL,
    exit_time timestamp with time zone,
    duration interval,
    status text DEFAULT 'ACTIVE'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT parking_transaction_status_check CHECK ((status = ANY (ARRAY['ACTIVE'::text, 'COMPLETED'::text, 'EXPIRED'::text]))),
    CONSTRAINT parking_transaction_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.parking_transaction_id_seq OWNED BY core.parking_transaction.id;
CREATE INDEX idx_parking_txn_facility ON core.parking_transaction USING btree (facility_id, entry_time DESC);
CREATE INDEX idx_parking_txn_status ON core.parking_transaction USING btree (status, entry_time DESC);
CREATE INDEX idx_parking_txn_vehicle ON core.parking_transaction USING btree (vehicle_id, entry_time DESC);

CREATE SEQUENCE IF NOT EXISTS core.perf_daily_snapshot_id_seq;
CREATE TABLE core.perf_daily_snapshot (
    id bigint DEFAULT nextval('core.perf_daily_snapshot_id_seq'::regclass) NOT NULL,
    report_date date NOT NULL,
    as_of_ts timestamp with time zone,
    source_file text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    upload_id uuid,
    uploaded_at timestamp with time zone,
    CONSTRAINT perf_daily_snapshot_pkey PRIMARY KEY (id),
    CONSTRAINT uq_perf_daily_snapshot UNIQUE (report_date)
);
ALTER SEQUENCE core.perf_daily_snapshot_id_seq OWNED BY core.perf_daily_snapshot.id;

CREATE SEQUENCE IF NOT EXISTS core.perf_daily_terminal_status_id_seq;
CREATE TABLE core.perf_daily_terminal_status (
    id bigint DEFAULT nextval('core.perf_daily_terminal_status_id_seq'::regclass) NOT NULL,
    report_date date NOT NULL,
    terminal_code text NOT NULL,
    icd_pendency_teus numeric(16,2),
    cfs_pendency_teus numeric(16,2),
    yard_import_teus numeric(16,2),
    yard_export_teus numeric(16,2),
    yard_transhipment_teus numeric(16,2),
    yard_total_teus numeric(16,2),
    yard_usable_capacity_teus numeric(16,2),
    yard_occupancy_pct numeric(6,2),
    gate_in_teus numeric(16,2),
    gate_out_teus numeric(16,2),
    gate_total_teus numeric(16,2),
    reefer_total_slots integer,
    reefer_occupied_slots integer,
    reefer_available_slots integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    source_file text,
    upload_id uuid,
    uploaded_at timestamp with time zone,
    CONSTRAINT perf_daily_terminal_status_pkey PRIMARY KEY (id),
    CONSTRAINT uq_perf_daily_status UNIQUE (report_date, terminal_code)
);
ALTER SEQUENCE core.perf_daily_terminal_status_id_seq OWNED BY core.perf_daily_terminal_status.id;
CREATE INDEX idx_perf_status_date ON core.perf_daily_terminal_status USING btree (report_date);
CREATE INDEX ix_perf_daily_terminal_status_upload ON core.perf_daily_terminal_status USING btree (upload_id);

CREATE SEQUENCE IF NOT EXISTS core.perf_daily_tonnage_id_seq;
CREATE TABLE core.perf_daily_tonnage (
    id bigint DEFAULT nextval('core.perf_daily_tonnage_id_seq'::regclass) NOT NULL,
    report_date date NOT NULL,
    category text NOT NULL,
    period text NOT NULL,
    vessels integer,
    liquid_tonnes numeric(16,2),
    dry_bulk_tonnes numeric(16,2),
    break_bulk_tonnes numeric(16,2),
    total_tonnes numeric(16,2),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    source_file text,
    upload_id uuid,
    uploaded_at timestamp with time zone,
    CONSTRAINT perf_daily_tonnage_category_check CHECK ((category = ANY (ARRAY['BPCL'::text, 'NSDT'::text, 'JJLTPL'::text, 'OTHER'::text, 'BULK_TOTAL'::text, 'CONTAINER_TOTAL'::text, 'JNPA_TOTAL'::text]))),
    CONSTRAINT perf_daily_tonnage_period_check CHECK ((period = ANY (ARRAY['DAY'::text, 'MONTH'::text, 'YEAR'::text]))),
    CONSTRAINT perf_daily_tonnage_pkey PRIMARY KEY (id),
    CONSTRAINT uq_perf_daily_tonnage UNIQUE (report_date, category, period)
);
ALTER SEQUENCE core.perf_daily_tonnage_id_seq OWNED BY core.perf_daily_tonnage.id;
CREATE INDEX idx_perf_tonnage_date ON core.perf_daily_tonnage USING btree (report_date);
CREATE INDEX ix_perf_daily_tonnage_upload ON core.perf_daily_tonnage USING btree (upload_id);

CREATE SEQUENCE IF NOT EXISTS core.perf_daily_traffic_id_seq;
CREATE TABLE core.perf_daily_traffic (
    id bigint DEFAULT nextval('core.perf_daily_traffic_id_seq'::regclass) NOT NULL,
    report_date date NOT NULL,
    terminal_code text NOT NULL,
    period text NOT NULL,
    vessels integer,
    imp_teus numeric(16,2),
    exp_teus numeric(16,2),
    total_teus numeric(16,2),
    rakes integer,
    rail_dis_teus numeric(16,2),
    rail_ldg_teus numeric(16,2),
    rail_total_teus numeric(16,2),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    source_file text,
    upload_id uuid,
    uploaded_at timestamp with time zone,
    CONSTRAINT perf_daily_traffic_period_check CHECK ((period = ANY (ARRAY['DAY'::text, 'MONTH'::text, 'YEAR'::text]))),
    CONSTRAINT perf_daily_traffic_pkey PRIMARY KEY (id),
    CONSTRAINT uq_perf_daily_traffic UNIQUE (report_date, terminal_code, period)
);
ALTER SEQUENCE core.perf_daily_traffic_id_seq OWNED BY core.perf_daily_traffic.id;
CREATE INDEX idx_perf_traffic_date_term ON core.perf_daily_traffic USING btree (report_date, terminal_code);
CREATE INDEX idx_perf_traffic_term_period ON core.perf_daily_traffic USING btree (terminal_code, period);
CREATE INDEX ix_perf_daily_traffic_upload ON core.perf_daily_traffic USING btree (upload_id);

CREATE SEQUENCE IF NOT EXISTS core.perf_daily_vessel_id_seq;
CREATE TABLE core.perf_daily_vessel (
    id bigint DEFAULT nextval('core.perf_daily_vessel_id_seq'::regclass) NOT NULL,
    report_date date NOT NULL,
    terminal_code text NOT NULL,
    berth_no text NOT NULL,
    via_no text,
    vessel_name text,
    cargo_commodity text,
    berthed_on timestamp with time zone,
    expected_completion timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    source_file text,
    upload_id uuid,
    uploaded_at timestamp with time zone,
    CONSTRAINT perf_daily_vessel_pkey PRIMARY KEY (id),
    CONSTRAINT uq_perf_daily_vessel UNIQUE (report_date, terminal_code, berth_no, via_no)
);
ALTER SEQUENCE core.perf_daily_vessel_id_seq OWNED BY core.perf_daily_vessel.id;
CREATE INDEX idx_perf_vessels_date ON core.perf_daily_vessel USING btree (report_date);
CREATE INDEX ix_perf_daily_vessel_upload ON core.perf_daily_vessel USING btree (upload_id);

CREATE SEQUENCE IF NOT EXISTS core.perf_import_log_id_seq;
CREATE TABLE core.perf_import_log (
    id bigint DEFAULT nextval('core.perf_import_log_id_seq'::regclass) NOT NULL,
    upload_id uuid NOT NULL,
    phase text NOT NULL,
    level text DEFAULT 'INFO'::text NOT NULL,
    message text,
    target_table text,
    affected_rows integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT perf_import_log_level_check CHECK ((level = ANY (ARRAY['INFO'::text, 'WARN'::text, 'ERROR'::text]))),
    CONSTRAINT perf_import_log_phase_check CHECK ((phase = ANY (ARRAY['VALIDATE'::text, 'IMPORT'::text]))),
    CONSTRAINT perf_import_log_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.perf_import_log_id_seq OWNED BY core.perf_import_log.id;
CREATE INDEX idx_perf_import_log_upload ON core.perf_import_log USING btree (upload_id, created_at);

CREATE SEQUENCE IF NOT EXISTS core.perf_ldb_congestion_id_seq;
CREATE TABLE core.perf_ldb_congestion (
    id bigint DEFAULT nextval('core.perf_ldb_congestion_id_seq'::regclass) NOT NULL,
    report_month date NOT NULL,
    cycle text NOT NULL,
    cluster_no integer NOT NULL,
    cluster_name text,
    cfs_count integer,
    pct_containers numeric(6,2),
    congestion_level text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    source_file text,
    upload_id uuid,
    uploaded_at timestamp with time zone,
    CONSTRAINT perf_ldb_congestion_congestion_level_check CHECK ((congestion_level = ANY (ARRAY['HIGH'::text, 'MEDIUM'::text, 'LOW'::text]))),
    CONSTRAINT perf_ldb_congestion_cycle_check CHECK ((cycle = ANY (ARRAY['IMPORT'::text, 'EXPORT'::text]))),
    CONSTRAINT perf_ldb_congestion_pkey PRIMARY KEY (id),
    CONSTRAINT uq_perf_ldb_congestion UNIQUE (report_month, cycle, cluster_no)
);
ALTER SEQUENCE core.perf_ldb_congestion_id_seq OWNED BY core.perf_ldb_congestion.id;
CREATE INDEX ix_perf_ldb_congestion_upload ON core.perf_ldb_congestion USING btree (upload_id);

CREATE SEQUENCE IF NOT EXISTS core.perf_ldb_facility_dwell_id_seq;
CREATE TABLE core.perf_ldb_facility_dwell (
    id bigint DEFAULT nextval('core.perf_ldb_facility_dwell_id_seq'::regclass) NOT NULL,
    report_month date NOT NULL,
    facility_type text NOT NULL,
    facility_name text NOT NULL,
    facility_name_norm text NOT NULL,
    dwell_hours numeric(8,2),
    dwell_hours_prev numeric(8,2),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    source_file text,
    upload_id uuid,
    uploaded_at timestamp with time zone,
    CONSTRAINT perf_ldb_facility_dwell_facility_type_check CHECK ((facility_type = ANY (ARRAY['CFS'::text, 'ICD'::text]))),
    CONSTRAINT perf_ldb_facility_dwell_pkey PRIMARY KEY (id),
    CONSTRAINT uq_perf_ldb_facility_dwell UNIQUE (report_month, facility_type, facility_name_norm)
);
ALTER SEQUENCE core.perf_ldb_facility_dwell_id_seq OWNED BY core.perf_ldb_facility_dwell.id;
CREATE INDEX idx_perf_ldb_facility_month ON core.perf_ldb_facility_dwell USING btree (report_month, facility_type);
CREATE INDEX ix_perf_ldb_facility_dwell_upload ON core.perf_ldb_facility_dwell USING btree (upload_id);

CREATE SEQUENCE IF NOT EXISTS core.perf_ldb_port_dwell_id_seq;
CREATE TABLE core.perf_ldb_port_dwell (
    id bigint DEFAULT nextval('core.perf_ldb_port_dwell_id_seq'::regclass) NOT NULL,
    report_month date NOT NULL,
    terminal_code text NOT NULL,
    cycle text NOT NULL,
    segment text NOT NULL,
    dwell_hours numeric(8,2),
    dwell_hours_prev numeric(8,2),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    source_file text,
    upload_id uuid,
    uploaded_at timestamp with time zone,
    CONSTRAINT perf_ldb_port_dwell_cycle_check CHECK ((cycle = ANY (ARRAY['IMPORT'::text, 'EXPORT'::text]))),
    CONSTRAINT perf_ldb_port_dwell_segment_check CHECK ((segment = ANY (ARRAY['OVERALL'::text, 'TRUCK'::text, 'TRAIN'::text]))),
    CONSTRAINT perf_ldb_port_dwell_pkey PRIMARY KEY (id),
    CONSTRAINT uq_perf_ldb_port_dwell UNIQUE (report_month, terminal_code, cycle, segment)
);
ALTER SEQUENCE core.perf_ldb_port_dwell_id_seq OWNED BY core.perf_ldb_port_dwell.id;
CREATE INDEX idx_perf_ldb_dwell_month ON core.perf_ldb_port_dwell USING btree (report_month, cycle);
CREATE INDEX ix_perf_ldb_port_dwell_upload ON core.perf_ldb_port_dwell USING btree (upload_id);

CREATE SEQUENCE IF NOT EXISTS core.perf_ldb_route_movement_id_seq;
CREATE TABLE core.perf_ldb_route_movement (
    id bigint DEFAULT nextval('core.perf_ldb_route_movement_id_seq'::regclass) NOT NULL,
    report_month date NOT NULL,
    cycle text NOT NULL,
    transport_mode text NOT NULL,
    route_name text NOT NULL,
    pct_share numeric(6,2),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    source_file text,
    upload_id uuid,
    uploaded_at timestamp with time zone,
    CONSTRAINT perf_ldb_route_movement_cycle_check CHECK ((cycle = ANY (ARRAY['IMPORT'::text, 'EXPORT'::text]))),
    CONSTRAINT perf_ldb_route_movement_transport_mode_check CHECK ((transport_mode = ANY (ARRAY['TRAIN'::text, 'TRUCK'::text]))),
    CONSTRAINT perf_ldb_route_movement_pkey PRIMARY KEY (id),
    CONSTRAINT uq_perf_ldb_route UNIQUE (report_month, cycle, transport_mode, route_name)
);
ALTER SEQUENCE core.perf_ldb_route_movement_id_seq OWNED BY core.perf_ldb_route_movement.id;
CREATE INDEX ix_perf_ldb_route_movement_upload ON core.perf_ldb_route_movement USING btree (upload_id);

CREATE SEQUENCE IF NOT EXISTS core.perf_ldb_weather_id_seq;
CREATE TABLE core.perf_ldb_weather (
    id bigint DEFAULT nextval('core.perf_ldb_weather_id_seq'::regclass) NOT NULL,
    report_month date NOT NULL,
    terminal_code text NOT NULL,
    cycle text NOT NULL,
    weather text NOT NULL,
    dwell_hours numeric(8,2),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    source_file text,
    upload_id uuid,
    uploaded_at timestamp with time zone,
    CONSTRAINT perf_ldb_weather_cycle_check CHECK ((cycle = ANY (ARRAY['IMPORT'::text, 'EXPORT'::text]))),
    CONSTRAINT perf_ldb_weather_weather_check CHECK ((weather = ANY (ARRAY['NORMAL'::text, 'ABNORMAL'::text]))),
    CONSTRAINT perf_ldb_weather_pkey PRIMARY KEY (id),
    CONSTRAINT uq_perf_ldb_weather UNIQUE (report_month, terminal_code, cycle, weather)
);
ALTER SEQUENCE core.perf_ldb_weather_id_seq OWNED BY core.perf_ldb_weather.id;
CREATE INDEX ix_perf_ldb_weather_upload ON core.perf_ldb_weather USING btree (upload_id);

CREATE SEQUENCE IF NOT EXISTS core.perf_monthly_teu_id_seq;
CREATE TABLE core.perf_monthly_teu (
    id bigint DEFAULT nextval('core.perf_monthly_teu_id_seq'::regclass) NOT NULL,
    fiscal_year text NOT NULL,
    month_date date NOT NULL,
    year_label text,
    month_label text,
    terminal_code text NOT NULL,
    vessel_calls integer,
    discharge_teus numeric(16,2),
    load_teus numeric(16,2),
    total_teus numeric(16,2),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    source_file text,
    upload_id uuid,
    uploaded_at timestamp with time zone,
    CONSTRAINT perf_monthly_teu_pkey PRIMARY KEY (id),
    CONSTRAINT uq_perf_monthly_teu UNIQUE (month_date, terminal_code)
);
ALTER SEQUENCE core.perf_monthly_teu_id_seq OWNED BY core.perf_monthly_teu.id;
CREATE INDEX idx_perf_monthly_term ON core.perf_monthly_teu USING btree (terminal_code, month_date);
CREATE INDEX ix_perf_monthly_teu_upload ON core.perf_monthly_teu USING btree (upload_id);

CREATE SEQUENCE IF NOT EXISTS core.perf_terminal_id_seq;
CREATE TABLE core.perf_terminal (
    id bigint DEFAULT nextval('core.perf_terminal_id_seq'::regclass) NOT NULL,
    code text NOT NULL,
    full_name text,
    operator text,
    terminal_type text DEFAULT 'CONTAINER'::text NOT NULL,
    is_container boolean DEFAULT true NOT NULL,
    aliases text[] DEFAULT '{}'::text[] NOT NULL,
    sort_order integer DEFAULT 100 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT perf_terminal_terminal_type_check CHECK ((terminal_type = ANY (ARRAY['CONTAINER'::text, 'MULTIPURPOSE'::text, 'LIQUID'::text, 'TOTAL'::text]))),
    CONSTRAINT perf_terminal_pkey PRIMARY KEY (id),
    CONSTRAINT uq_perf_terminal_code UNIQUE (code)
);
ALTER SEQUENCE core.perf_terminal_id_seq OWNED BY core.perf_terminal.id;

CREATE SEQUENCE IF NOT EXISTS core.perf_upload_error_id_seq;
CREATE TABLE core.perf_upload_error (
    id bigint DEFAULT nextval('core.perf_upload_error_id_seq'::regclass) NOT NULL,
    upload_id uuid NOT NULL,
    row_number integer,
    column_name text,
    error_code text,
    error_detail text,
    raw_value text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT perf_upload_error_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.perf_upload_error_id_seq OWNED BY core.perf_upload_error.id;
CREATE INDEX idx_perf_upload_error_upload ON core.perf_upload_error USING btree (upload_id);

CREATE SEQUENCE IF NOT EXISTS core.perf_upload_id_seq;
CREATE TABLE core.perf_upload (
    id bigint DEFAULT nextval('core.perf_upload_id_seq'::regclass) NOT NULL,
    upload_id uuid DEFAULT gen_random_uuid() NOT NULL,
    report_type text NOT NULL,
    original_filename text,
    file_size_bytes integer,
    status text DEFAULT 'VALIDATED'::text NOT NULL,
    uploaded_by text,
    row_count integer DEFAULT 0 NOT NULL,
    inserted_count integer DEFAULT 0 NOT NULL,
    skipped_count integer DEFAULT 0 NOT NULL,
    error_count integer DEFAULT 0 NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    file_format text,
    updated_count integer DEFAULT 0 NOT NULL,
    CONSTRAINT perf_upload_report_type_check CHECK ((report_type = ANY (ARRAY['daily_status'::text, 'monthly_teu'::text, 'ldb_report'::text]))),
    CONSTRAINT perf_upload_status_check CHECK ((status = ANY (ARRAY['VALIDATED'::text, 'REJECTED'::text, 'IMPORTED'::text, 'FAILED'::text]))),
    CONSTRAINT perf_upload_pkey PRIMARY KEY (id),
    CONSTRAINT uq_perf_upload_upload_id UNIQUE (upload_id)
);
ALTER SEQUENCE core.perf_upload_id_seq OWNED BY core.perf_upload.id;
CREATE INDEX idx_perf_upload_created ON core.perf_upload USING btree (created_at DESC);
CREATE INDEX idx_perf_upload_type_status ON core.perf_upload USING btree (report_type, status);

CREATE TABLE core.push_subscription (
    device_id text NOT NULL,
    driver_id text,
    vehicle_id text,
    webpush jsonb,
    fcm_token text,
    platform text DEFAULT 'web'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT push_subscription_pkey PRIMARY KEY (device_id)
);
CREATE INDEX idx_push_subs_driver ON core.push_subscription USING btree (driver_id);
CREATE INDEX idx_push_subs_fcm ON core.push_subscription USING btree (fcm_token) WHERE (fcm_token IS NOT NULL);

CREATE SEQUENCE IF NOT EXISTS core.reefer_slot_id_seq;
CREATE TABLE core.reefer_slot (
    id bigint DEFAULT nextval('core.reefer_slot_id_seq'::regclass) NOT NULL,
    facility_id text DEFAULT 'PK-CPP'::text NOT NULL,
    slot_code text NOT NULL,
    powered boolean DEFAULT true NOT NULL,
    status text DEFAULT 'AVAILABLE'::text NOT NULL,
    container_number text,
    set_temperature double precision,
    current_temperature double precision,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT reefer_slot_status_check CHECK ((status = ANY (ARRAY['AVAILABLE'::text, 'OCCUPIED'::text, 'RESERVED'::text, 'FAULT'::text]))),
    CONSTRAINT reefer_slot_pkey PRIMARY KEY (id),
    CONSTRAINT reefer_slot_facility_id_slot_code_key UNIQUE (facility_id, slot_code)
);
ALTER SEQUENCE core.reefer_slot_id_seq OWNED BY core.reefer_slot.id;
CREATE INDEX idx_reefer_slot_fac ON core.reefer_slot USING btree (facility_id, status);

CREATE TABLE core.rfid_read (
    ts timestamp with time zone NOT NULL,
    reader_id text,
    tag_id text,
    rssi real
);
CREATE INDEX rfid_read_ts_idx ON core.rfid_read USING btree (ts DESC);

CREATE TABLE core.scenario_handle (
    handle_id text NOT NULL,
    name text NOT NULL,
    status text DEFAULT 'RUNNING'::text NOT NULL,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    trace_id text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_at timestamp with time zone,
    CONSTRAINT scenario_handle_pkey PRIMARY KEY (handle_id)
);

CREATE SEQUENCE IF NOT EXISTS core.scenario_step_id_seq;
CREATE TABLE core.scenario_step (
    id bigint DEFAULT nextval('core.scenario_step_id_seq'::regclass) NOT NULL,
    handle_id text NOT NULL,
    step_no integer NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    title text NOT NULL,
    status text DEFAULT 'ok'::text NOT NULL,
    trigger text,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT scenario_step_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.scenario_step_id_seq OWNED BY core.scenario_step.id;
CREATE INDEX idx_scenario_step_handle ON core.scenario_step USING btree (handle_id, step_no);

CREATE TABLE core.scenario (
    id text NOT NULL,
    name text,
    started_at timestamp with time zone,
    ended_at timestamp with time zone,
    params jsonb,
    CONSTRAINT scenario_pkey PRIMARY KEY (id)
);

CREATE TABLE core.ulip_service (
    name text NOT NULL,
    kind text NOT NULL,
    base_url text NOT NULL,
    healthy boolean DEFAULT true,
    enabled boolean DEFAULT true,
    registered_at timestamp with time zone DEFAULT now(),
    meta jsonb DEFAULT '{}'::jsonb,
    CONSTRAINT ulip_service_pkey PRIMARY KEY (name, kind)
);

CREATE SEQUENCE IF NOT EXISTS core.sl_event_id_seq;
CREATE TABLE core.sl_event (
    id bigint DEFAULT nextval('core.sl_event_id_seq'::regclass) NOT NULL,
    event text NOT NULL,
    module text,
    reference text,
    container_no text,
    payload jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT sl_event_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.sl_event_id_seq OWNED BY core.sl_event.id;
CREATE INDEX idx_sl_event_cont ON core.sl_event USING btree (container_no);
CREATE INDEX idx_sl_event_mod ON core.sl_event USING btree (module, id DESC);

CREATE SEQUENCE IF NOT EXISTS core.sl_import_error_id_seq;
CREATE TABLE core.sl_import_error (
    id bigint DEFAULT nextval('core.sl_import_error_id_seq'::regclass) NOT NULL,
    import_file_id bigint NOT NULL,
    record_ref text,
    error_code text NOT NULL,
    error_detail text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT sl_import_error_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.sl_import_error_id_seq OWNED BY core.sl_import_error.id;
CREATE INDEX idx_sl_err_file ON core.sl_import_error USING btree (import_file_id, id);

CREATE SEQUENCE IF NOT EXISTS core.sl_import_file_id_seq;
CREATE TABLE core.sl_import_file (
    id bigint DEFAULT nextval('core.sl_import_file_id_seq'::regclass) NOT NULL,
    list_type text NOT NULL,
    terminal text NOT NULL,
    physical_format text NOT NULL,
    source_file text NOT NULL,
    source_sha256 text NOT NULL,
    file_size_bytes bigint,
    vessel_visit text,
    voyage text,
    line_code text,
    direction text,
    record_count integer DEFAULT 0 NOT NULL,
    imported_count integer DEFAULT 0 NOT NULL,
    error_count integer DEFAULT 0 NOT NULL,
    import_status text DEFAULT 'PENDING'::text NOT NULL,
    error_detail text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    uploaded_by text,
    source text DEFAULT 'DIRECTORY'::text NOT NULL,
    CONSTRAINT chk_sl_import_file_source CHECK ((source = ANY (ARRAY['DIRECTORY'::text, 'UPLOAD'::text]))),
    CONSTRAINT sl_import_file_import_status_check CHECK ((import_status = ANY (ARRAY['PENDING'::text, 'SUCCESS'::text, 'PARTIAL'::text, 'FAILED'::text, 'SKIPPED_DUPLICATE'::text]))),
    CONSTRAINT sl_import_file_list_type_check CHECK ((list_type = ANY (ARRAY['IAL'::text, 'EAL'::text, 'EDO'::text]))),
    CONSTRAINT sl_import_file_physical_format_check CHECK ((physical_format = ANY (ARRAY['CSV'::text, 'XLS'::text, 'XLSX'::text, 'CODECO_XML'::text]))),
    CONSTRAINT sl_import_file_terminal_check CHECK ((terminal = ANY (ARRAY['APMT'::text, 'BMCT'::text, 'GTI'::text, 'NSFT'::text, 'NSICT'::text, 'NSIGT'::text, 'OTHER'::text]))),
    CONSTRAINT sl_import_file_pkey PRIMARY KEY (id),
    CONSTRAINT uq_sl_import_file_sha UNIQUE (source_sha256)
);
ALTER SEQUENCE core.sl_import_file_id_seq OWNED BY core.sl_import_file.id;
CREATE INDEX idx_sl_file_list ON core.sl_import_file USING btree (list_type, id DESC);
CREATE INDEX idx_sl_file_source ON core.sl_import_file USING btree (source, id DESC);
CREATE INDEX idx_sl_file_stat ON core.sl_import_file USING btree (import_status, id DESC);
CREATE INDEX idx_sl_file_term ON core.sl_import_file USING btree (terminal, id DESC);

CREATE SEQUENCE IF NOT EXISTS core.tas_appointment_id_seq;
CREATE TABLE core.tas_appointment (
    id bigint DEFAULT nextval('core.tas_appointment_id_seq'::regclass) NOT NULL,
    slot_code text NOT NULL,
    gate_id text NOT NULL,
    window_start timestamp with time zone NOT NULL,
    window_end timestamp with time zone NOT NULL,
    capacity integer DEFAULT 10 NOT NULL,
    booked integer DEFAULT 0 NOT NULL,
    status text DEFAULT 'OPEN'::text NOT NULL,
    source text DEFAULT 'LOCAL'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT tas_appointment_status_check CHECK ((status = ANY (ARRAY['OPEN'::text, 'FULL'::text, 'CLOSED'::text]))),
    CONSTRAINT tas_appointment_pkey PRIMARY KEY (id),
    CONSTRAINT tas_appointment_slot_code_key UNIQUE (slot_code)
);
ALTER SEQUENCE core.tas_appointment_id_seq OWNED BY core.tas_appointment.id;
CREATE INDEX idx_tas_gate_window ON core.tas_appointment USING btree (gate_id, window_start);
CREATE INDEX idx_tas_status ON core.tas_appointment USING btree (status, window_start);

CREATE SEQUENCE IF NOT EXISTS core.tas_booking_id_seq;
CREATE TABLE core.tas_booking (
    id bigint DEFAULT nextval('core.tas_booking_id_seq'::regclass) NOT NULL,
    appointment_id bigint NOT NULL,
    slot_code text NOT NULL,
    vehicle_id text,
    driver_id text,
    status text DEFAULT 'BOOKED'::text NOT NULL,
    booked_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT tas_booking_status_check CHECK ((status = ANY (ARRAY['BOOKED'::text, 'CANCELLED'::text, 'COMPLETED'::text, 'NO_SHOW'::text]))),
    CONSTRAINT tas_booking_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.tas_booking_id_seq OWNED BY core.tas_booking.id;
CREATE INDEX idx_tas_booking_appt ON core.tas_booking USING btree (appointment_id);
CREATE INDEX idx_tas_booking_veh ON core.tas_booking USING btree (vehicle_id, booked_at DESC);

CREATE SEQUENCE IF NOT EXISTS core.td_import_error_id_seq;
CREATE TABLE core.td_import_error (
    id bigint DEFAULT nextval('core.td_import_error_id_seq'::regclass) NOT NULL,
    import_file_id bigint NOT NULL,
    record_ref text,
    error_code text NOT NULL,
    error_detail text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT td_import_error_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.td_import_error_id_seq OWNED BY core.td_import_error.id;
CREATE INDEX idx_td_err_file ON core.td_import_error USING btree (import_file_id, id);

CREATE SEQUENCE IF NOT EXISTS core.td_import_file_id_seq;
CREATE TABLE core.td_import_file (
    id bigint DEFAULT nextval('core.td_import_file_id_seq'::regclass) NOT NULL,
    entity_type text NOT NULL,
    physical_format text NOT NULL,
    source_file text,
    source_sha256 text,
    file_size_bytes bigint,
    record_count integer DEFAULT 0 NOT NULL,
    imported_count integer DEFAULT 0 NOT NULL,
    error_count integer DEFAULT 0 NOT NULL,
    duplicate_count integer DEFAULT 0 NOT NULL,
    import_status text DEFAULT 'PENDING'::text NOT NULL,
    error_detail text,
    uploaded_by text,
    source text DEFAULT 'UPLOAD'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT td_import_file_entity_type_check CHECK ((entity_type = ANY (ARRAY['TRANSPORTER'::text, 'DRIVER'::text]))),
    CONSTRAINT td_import_file_import_status_check CHECK ((import_status = ANY (ARRAY['PENDING'::text, 'SUCCESS'::text, 'PARTIAL'::text, 'FAILED'::text, 'SKIPPED_DUPLICATE'::text]))),
    CONSTRAINT td_import_file_physical_format_check CHECK ((physical_format = ANY (ARRAY['CSV'::text, 'XLS'::text, 'XLSX'::text]))),
    CONSTRAINT td_import_file_source_check CHECK ((source = ANY (ARRAY['DIRECTORY'::text, 'UPLOAD'::text]))),
    CONSTRAINT td_import_file_pkey PRIMARY KEY (id),
    CONSTRAINT uq_td_import_file_sha UNIQUE (source_sha256)
);
ALTER SEQUENCE core.td_import_file_id_seq OWNED BY core.td_import_file.id;
CREATE INDEX idx_td_file_entity ON core.td_import_file USING btree (entity_type, id DESC);
CREATE INDEX idx_td_file_source ON core.td_import_file USING btree (source, id DESC);
CREATE INDEX idx_td_file_status ON core.td_import_file USING btree (import_status, id DESC);

CREATE TABLE core.toll_enroute (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    client_id text,
    source_state text,
    source_name text,
    destination_state text,
    destination_name text,
    vehicle_type text,
    duration text,
    distance numeric(10,2),
    toll_plaza_details jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT toll_enroute_pkey PRIMARY KEY (id)
);
CREATE INDEX idx_toll_enroute_route ON core.toll_enroute USING btree (source_name, destination_name, created_at DESC);

CREATE TABLE core.traffic_snapshot (
    ts timestamp with time zone NOT NULL,
    segment_id text,
    speed_kmh real,
    jam_factor real,
    source text
);
CREATE INDEX traffic_snapshot_ts_idx ON core.traffic_snapshot USING btree (ts DESC);

CREATE SEQUENCE IF NOT EXISTS core.trailer_read_id_seq;
CREATE TABLE core.trailer_read (
    id bigint DEFAULT nextval('core.trailer_read_id_seq'::regclass) NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    camera_id text,
    gate_id text,
    trailer_number text,
    plate text,
    vehicle_id text,
    confidence double precision DEFAULT 0.0 NOT NULL,
    image_url text,
    source text DEFAULT 'CAMERA_AI'::text NOT NULL,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT trailer_read_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.trailer_read_id_seq OWNED BY core.trailer_read.id;
CREATE INDEX idx_trailer_read_num ON core.trailer_read USING btree (trailer_number, ts DESC);
CREATE INDEX idx_trailer_read_plate ON core.trailer_read USING btree (plate, ts DESC);
CREATE INDEX idx_trailer_read_ts ON core.trailer_read USING btree (ts DESC);

CREATE SEQUENCE IF NOT EXISTS core.transporter_blacklist_id_seq;
CREATE TABLE core.transporter_blacklist (
    id bigint DEFAULT nextval('core.transporter_blacklist_id_seq'::regclass) NOT NULL,
    transporter_id bigint NOT NULL,
    reason text NOT NULL,
    severity text DEFAULT 'HIGH'::text NOT NULL,
    status text DEFAULT 'ACTIVE'::text NOT NULL,
    blacklisted_by text,
    blacklisted_at timestamp with time zone DEFAULT now() NOT NULL,
    lifted_by text,
    lifted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT transporter_blacklist_severity_check CHECK ((severity = ANY (ARRAY['LOW'::text, 'MEDIUM'::text, 'HIGH'::text, 'CRITICAL'::text]))),
    CONSTRAINT transporter_blacklist_status_check CHECK ((status = ANY (ARRAY['ACTIVE'::text, 'LIFTED'::text]))),
    CONSTRAINT transporter_blacklist_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.transporter_blacklist_id_seq OWNED BY core.transporter_blacklist.id;
CREATE INDEX idx_blacklist_status ON core.transporter_blacklist USING btree (status, blacklisted_at DESC);
CREATE INDEX idx_blacklist_transporter ON core.transporter_blacklist USING btree (transporter_id, status);

CREATE SEQUENCE IF NOT EXISTS core.transporter_vehicle_id_seq;
CREATE TABLE core.transporter_vehicle (
    id bigint DEFAULT nextval('core.transporter_vehicle_id_seq'::regclass) NOT NULL,
    transporter_id bigint NOT NULL,
    vehicle_no text NOT NULL,
    vehicle_no_norm text NOT NULL,
    driver_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT transporter_vehicle_pkey PRIMARY KEY (id),
    CONSTRAINT transporter_vehicle_transporter_id_vehicle_no_norm_key UNIQUE (transporter_id, vehicle_no_norm)
);
ALTER SEQUENCE core.transporter_vehicle_id_seq OWNED BY core.transporter_vehicle.id;
CREATE INDEX idx_transporter_veh_driver ON core.transporter_vehicle USING btree (driver_id);
CREATE INDEX idx_transporter_veh_norm ON core.transporter_vehicle USING btree (vehicle_no_norm);

CREATE SEQUENCE IF NOT EXISTS core.trt_record_id_seq;
CREATE TABLE core.trt_record (
    id bigint DEFAULT nextval('core.trt_record_id_seq'::regclass) NOT NULL,
    vehicle_id text,
    plate text,
    trip_id text,
    gate_in_at timestamp with time zone,
    parking_at timestamp with time zone,
    loading_at timestamp with time zone,
    gate_out_at timestamp with time zone,
    gate_to_park_min double precision,
    park_to_load_min double precision,
    load_to_out_min double precision,
    trt_min double precision,
    status text DEFAULT 'OPEN'::text NOT NULL,
    source text DEFAULT 'COMPUTED'::text NOT NULL,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT trt_record_status_check CHECK ((status = ANY (ARRAY['OPEN'::text, 'GATE_IN'::text, 'PARKED'::text, 'LOADING'::text, 'COMPLETED'::text]))),
    CONSTRAINT trt_record_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.trt_record_id_seq OWNED BY core.trt_record.id;
CREATE INDEX idx_trt_out ON core.trt_record USING btree (gate_out_at DESC);
CREATE INDEX idx_trt_status ON core.trt_record USING btree (status, created_at DESC);
CREATE INDEX idx_trt_vehicle ON core.trt_record USING btree (vehicle_id, created_at DESC);

CREATE TABLE core.truck_telemetry (
    ts timestamp with time zone NOT NULL,
    device_id text,
    plate text,
    lat double precision,
    lon double precision,
    speed_kmh real,
    heading real,
    battery real,
    accuracy_m real
);
CREATE INDEX idx_telemetry_plate_ts ON core.truck_telemetry USING btree (plate, ts DESC);
CREATE INDEX truck_telemetry_ts_idx ON core.truck_telemetry USING btree (ts DESC);

CREATE SEQUENCE IF NOT EXISTS core.tt_trip_id_seq;
CREATE TABLE core.tt_trip (
    id bigint DEFAULT nextval('core.tt_trip_id_seq'::regclass) NOT NULL,
    cycle_id text NOT NULL,
    vehicle_id text NOT NULL,
    driver_id text,
    trip_seq integer DEFAULT 1 NOT NULL,
    direction text DEFAULT 'INBOUND'::text NOT NULL,
    origin text,
    destination text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_at timestamp with time zone,
    laden boolean,
    status text DEFAULT 'IN_PROGRESS'::text NOT NULL,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT tt_trip_direction_check CHECK ((direction = ANY (ARRAY['INBOUND'::text, 'OUTBOUND'::text, 'RETURN'::text]))),
    CONSTRAINT tt_trip_status_check CHECK ((status = ANY (ARRAY['IN_PROGRESS'::text, 'COMPLETED'::text, 'ABORTED'::text]))),
    CONSTRAINT tt_trip_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.tt_trip_id_seq OWNED BY core.tt_trip.id;
CREATE INDEX idx_tt_trip_cycle ON core.tt_trip USING btree (cycle_id, trip_seq);
CREATE INDEX idx_tt_trip_status ON core.tt_trip USING btree (status, started_at DESC);
CREATE INDEX idx_tt_trip_vehicle ON core.tt_trip USING btree (vehicle_id, started_at DESC);

CREATE TABLE core.vehicle_rc (
    plate text NOT NULL,
    rc_type text,
    owner_hash text,
    fitness_valid_to date,
    puc_valid_to date,
    fastag_status text,
    provisional boolean DEFAULT false,
    provisional_until timestamp with time zone,
    owner_name_masked text,
    vehicle_class text,
    fuel_type text,
    insurance_valid_to date,
    registration_date date,
    state text,
    rto_code text,
    blacklist_status text DEFAULT 'CLEAR'::text,
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT vehicle_rc_pkey PRIMARY KEY (plate)
);

CREATE SEQUENCE IF NOT EXISTS core.vehicle_verification_history_id_seq;
CREATE TABLE core.vehicle_verification_history (
    id bigint DEFAULT nextval('core.vehicle_verification_history_id_seq'::regclass) NOT NULL,
    vehicle_number text,
    request_payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    response_payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    verification_status text,
    source text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT vehicle_verification_history_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.vehicle_verification_history_id_seq OWNED BY core.vehicle_verification_history.id;
CREATE INDEX idx_veh_verif_number ON core.vehicle_verification_history USING btree (vehicle_number, created_at DESC);
CREATE INDEX idx_veh_verif_ts ON core.vehicle_verification_history USING btree (created_at DESC);

CREATE SEQUENCE IF NOT EXISTS core.verification_log_id_seq;
CREATE TABLE core.verification_log (
    id bigint DEFAULT nextval('core.verification_log_id_seq'::regclass) NOT NULL,
    driver_id text NOT NULL,
    decision text NOT NULL,
    score double precision,
    matched boolean,
    provider text,
    decision_path text,
    actor text,
    purpose text,
    reason text,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT verification_log_pkey PRIMARY KEY (id)
);
ALTER SEQUENCE core.verification_log_id_seq OWNED BY core.verification_log.id;
CREATE INDEX idx_verification_log_driver ON core.verification_log USING btree (driver_id, ts DESC);

CREATE TABLE core.violation_case (
    case_id uuid NOT NULL,
    vehicle_number text,
    driver_id text,
    first_detected_at timestamp with time zone DEFAULT now() NOT NULL,
    last_updated_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'DETECTED'::text NOT NULL,
    total_fine integer DEFAULT 0 NOT NULL,
    evidence_url text,
    evidence_sha256 text,
    gate_id text,
    confidence double precision,
    CONSTRAINT violation_case_status_check CHECK ((status = ANY (ARRAY['DETECTED'::text, 'REVIEWED'::text, 'CONFIRMED'::text, 'CHALLAN_ISSUED'::text, 'PAID'::text, 'CLOSED'::text]))),
    CONSTRAINT violation_case_pkey PRIMARY KEY (case_id)
);
CREATE INDEX idx_violation_case_plate ON core.violation_case USING btree (vehicle_number, first_detected_at DESC);
CREATE INDEX idx_violation_case_status ON core.violation_case USING btree (status, last_updated_at DESC);

-- ---- foreign keys (remapped to ported names) ----
ALTER TABLE core.accident_event ADD CONSTRAINT accident_event_accident_id_fkey FOREIGN KEY (accident_id) REFERENCES core.accident(id) ON DELETE CASCADE;
ALTER TABLE core.berthing_record_event ADD CONSTRAINT berthing_record_event_berthing_id_fkey FOREIGN KEY (berthing_id) REFERENCES core.berthing_record(id) ON DELETE CASCADE;
ALTER TABLE core.berthing_import_error ADD CONSTRAINT berthing_import_error_import_file_id_fkey FOREIGN KEY (import_file_id) REFERENCES core.berthing_import_file(id) ON DELETE CASCADE;
ALTER TABLE core.berthing_report_table ADD CONSTRAINT berthing_report_table_document_id_fkey FOREIGN KEY (document_id) REFERENCES core.berthing_report_document(id) ON DELETE CASCADE;
ALTER TABLE core.camera ADD CONSTRAINT camera_gate_id_fkey FOREIGN KEY (gate_id) REFERENCES core.gate(id);
ALTER TABLE core.cfs_ecy_import_error ADD CONSTRAINT cfs_ecy_import_error_import_file_id_fkey FOREIGN KEY (import_file_id) REFERENCES core.cfs_ecy_import_file(id) ON DELETE CASCADE;
ALTER TABLE core.customs_import_error ADD CONSTRAINT customs_import_error_message_id_fkey FOREIGN KEY (message_id) REFERENCES core.customs_message(id) ON DELETE CASCADE;
ALTER TABLE core.nvr_camera_map ADD CONSTRAINT nvr_camera_map_nvr_id_fkey FOREIGN KEY (nvr_id) REFERENCES core.nvr_device(id) ON DELETE CASCADE;
ALTER TABLE core.parking_slot ADD CONSTRAINT parking_slot_facility_id_fkey FOREIGN KEY (facility_id) REFERENCES core.parking_facility(id) ON DELETE CASCADE;
ALTER TABLE core.perf_import_log ADD CONSTRAINT perf_import_log_upload_id_fkey FOREIGN KEY (upload_id) REFERENCES core.perf_upload(upload_id) ON DELETE CASCADE;
ALTER TABLE core.perf_upload_error ADD CONSTRAINT perf_upload_error_upload_id_fkey FOREIGN KEY (upload_id) REFERENCES core.perf_upload(upload_id) ON DELETE CASCADE;
ALTER TABLE core.scenario_step ADD CONSTRAINT scenario_step_handle_id_fkey FOREIGN KEY (handle_id) REFERENCES core.scenario_handle(handle_id) ON DELETE CASCADE;
ALTER TABLE core.sl_import_error ADD CONSTRAINT sl_import_error_import_file_id_fkey FOREIGN KEY (import_file_id) REFERENCES core.sl_import_file(id) ON DELETE CASCADE;
ALTER TABLE core.tas_booking ADD CONSTRAINT tas_booking_appointment_id_fkey FOREIGN KEY (appointment_id) REFERENCES core.tas_appointment(id) ON DELETE CASCADE;
ALTER TABLE core.td_import_error ADD CONSTRAINT td_import_error_import_file_id_fkey FOREIGN KEY (import_file_id) REFERENCES core.td_import_file(id) ON DELETE CASCADE;

-- ---- trigger functions + triggers ----
CREATE OR REPLACE FUNCTION core.geofence_events_default_event_type()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.event_type IS NULL OR NEW.event_type = '' THEN
        NEW.event_type := CASE
            WHEN NEW.violation_type IS NOT NULL AND NEW.violation_type <> '' THEN NEW.violation_type
            WHEN NEW.exit_time IS NOT NULL                                   THEN 'EXIT'
            WHEN COALESCE(NEW.dwell_seconds, 0) > 0                          THEN 'DWELL'
            WHEN NEW.entry_time IS NOT NULL                                  THEN 'ENTER'
            ELSE 'ENTER'
        END;
    END IF;
    RETURN NEW;
END;
$function$;
CREATE OR REPLACE FUNCTION core.set_updated_at()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$function$;
CREATE TRIGGER trg_cargo_updated_at BEFORE UPDATE ON core.cargo FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();
CREATE TRIGGER trg_geofence_events_event_type BEFORE INSERT OR UPDATE ON core.geofence_event FOR EACH ROW EXECUTE FUNCTION core.geofence_events_default_event_type();

-- ---- standalone sequences ----
CREATE SEQUENCE IF NOT EXISTS core.challan_seq;
COMMIT;
