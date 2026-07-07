// Wire types mirroring the gateway's JSON contracts (gateway/routers/*). Kept
// deliberately loose where the backend payloads are open-ended (Alert.payload,
// KPI view rows) so the UI never fights the schema during a live demo.

export type Severity = "info" | "warning" | "critical" | "REPORT_TO_POLICE" | string;

export interface Alert {
  id: string;
  ts: string;
  kind: string;
  severity: Severity;
  gate_id?: string | null;
  plate?: string | null;
  payload?: Record<string, any>;
  ack?: boolean;
}

export interface Gate {
  id: string;
  name: string;
  lat: number;
  lon: number;
  target_vph: number;
  throughput_60min: number;
  utilisation: number | null;
}

export interface CorridorSegment {
  id: string;
  start: [number, number]; // [lon, lat]
  end: [number, number];
  length_km: number;
}

export interface CorridorGeometry {
  name: string;
  polyline: [number, number][]; // [lon, lat]
  segments: CorridorSegment[];
  length_km: number;
  segment_count: number;
}

export interface TrafficSnapshot {
  segment_id: string;
  ts: string;
  speed_kmh: number;
  jam_factor: number;
  source: string;
}

export interface TruckDevice {
  device_id: string;
  plate?: string | null;
  gate_id?: string | null;
  state: string;
  position: { lat: number; lon: number };
  speed_kmh: number;
  heading: number;
  remaining_km: number;
  eta_s: number | null;
  segment_id?: string | null;
}

export interface SourceHealth {
  source: string;
  state: string; // LIVE | DEGRADED | DOWN
  last_ok: string | null;
  latency_p95_ms: number | null;
  last_decision_path: string | null;
}

export interface CameraHealth {
  camera_id: string;
  decision_path: string; // LIVE | CACHED | SYNTHETIC
  frame_age_s: number | null;
}

export interface Decision {
  api: string;
  key?: string | null;
  decision_path: string;
  latency_ms?: number | null;
  ts: string;
  detail?: Record<string, any>;
}

export interface Zone {
  id: string;
  name: string;
  kind: "no_parking" | "restricted" | string;
  polygon: [number, number][]; // [lon, lat] ring
  escalation: { warn_min: number; notice_min: number; challan_min: number };
  enabled: boolean;
  updated_at?: string;
}

export interface PoliceIncident extends Alert {
  rc?: Record<string, any>;
  challan?: Record<string, any>;
  evidence_url?: string | null;
}

export interface Scenario {
  id: string;
  name: string;
  started_at?: string | null;
  ended_at?: string | null;
  params?: Record<string, any>;
}

export interface ScenarioStep {
  handle_id: string;
  scenario: string; // tfc1 | tfc2 | tfc3
  step_no: number;
  title: string;
  status: "ok" | "degraded" | "failed" | "info" | string;
  trigger?: string | null;
  ts: string;
  detail?: Record<string, any>;
  trace_id?: string | null;
}

// --- KPI engine contract (mirrors shared/jnpa_shared/kpi.py KpiResult) ---
export interface KpiResult {
  key: string;
  label: string;
  unit: string;
  value: number;
  target: number;
  baseline: number;
  deltaPct: number;
  direction: "lower_is_better" | "higher_is_better";
  onTarget: boolean;
  trend: number[];
}

// --- Appendix-C capability wire types (gateway routers) ---

// Empty-container (/api/empty)
export interface EmptyAllocation {
  demand_id: string;
  supply_depot: string;
  container_type: string;
  cargo_type: string;
  distance_km: number;
  est_trt_min: number;
  confidence?: number;
}

// Carbon (/api/carbon)
export interface CarbonRollup {
  total_kg: number;
  vehicle_count: number;
  by_class: Record<string, number>;
  by_source: { moving: number; idle: number };
}

// Gate-data / Auto-LEO (/api/gate-data)
export interface AutoLeoResult {
  container_no: string;
  vehicle_plate?: string | null;
  leo_ready: boolean;
  checks: Record<string, any>;
  customs_flags: string[];
  // Optional map anchor (mock fills these; live may omit) so a clicked queue
  // row can pan/zoom the map to the container's gate location.
  gate_id?: string | null;
  lat?: number;
  lon?: number;
}

// Identity / face-recognition (/api/identity)
export type IdentitySimMode = "genuine" | "impostor" | "unknown";

/** Verify input: a captured frame (base64/data-URL) and/or the legacy simulate. */
export interface IdentityVerifyArg {
  simulate?: IdentitySimMode;
  image?: string;
}

export interface IdentityVerifyResult {
  driver_id: string;
  matched: boolean;
  score: number;
  decision: "VERIFIED" | "PROVISIONAL" | "REJECTED" | string;
  provisional_until?: string;
  cure_window_h?: number;
  reason?: string;
  /** Which embedding provider produced the capture vector ("synthetic" | "onnx"). */
  provider?: string;
}

export interface IdentityEnrolResult {
  enrolled: boolean;
  driver_id: string;
  provider?: string;
}

// --- Vehicle Violation Detection (/api/violations) ---
// Orchestration-only enforcement console on the Reports page: ANPR + vehicle/
// driver lookup -> operator-confirmed violations -> jnpa.alerts incidents.

/** One selectable violation kind + its e-Challan fine (reports._CHALLAN). */
export interface ViolationCatalogItem {
  kind: string;
  label: string;
  section?: string | null;
  fine_inr?: number | null;
}

/** Mapped driver for a detected plate (jnpa.drivers / driver_enrollments). */
export interface ViolationDriver {
  driver_id: string;
  name?: string | null;
  status?: string | null;
  vehicle_no?: string | null;
}

/** Result of POST /api/violations/detect — no incident persisted yet. */
export interface ViolationDetectResult {
  case_id: string;
  plate?: string | null;
  confidence?: number | null;
  anpr_decision_path: string; // LIVE | SYNTHETIC
  /** True only when the real ANPR service produced the read (LIVE). */
  anpr_real?: boolean;
  /** [x1,y1,x2,y2] plate box in the uploaded image's pixels; null if synthetic. */
  bbox?: number[] | null;
  degraded: boolean;
  vehicle?: Record<string, any> | null;
  vehicle_class?: string | null;
  driver?: ViolationDriver | null;
  evidence_url?: string | null;
  evidence_sha256?: string | null;
  gate_id?: string | null;
  available_violations: ViolationCatalogItem[];
}

/** Body for POST /api/violations/commit. */
export interface ViolationCommitInput {
  case_id?: string;
  plate?: string | null;
  gate_id?: string | null;
  evidence_url?: string | null;
  evidence_sha256?: string | null;
  confidence?: number | null;
  driver_id?: string | null;
  vehicle_class?: string | null;
  zone_id?: string | null;
  /** false = stop at CONFIRMED (Save Case); true (default) = issue challan. */
  issue_challan?: boolean;
  violations: string[];
}

/** Case lifecycle states (mirrors the gateway state machine). */
export type CaseStatus =
  | "DETECTED"
  | "REVIEWED"
  | "CONFIRMED"
  | "CHALLAN_ISSUED"
  | "PAID"
  | "CLOSED";

/** Committed incident — case + (optional) immutable challan. */
export interface ViolationIncident {
  case_id: string;
  challan_id?: string | null;
  challan_no?: string | null;
  status?: CaseStatus | string;
  vehicle_number?: string | null;
  driver_id?: string | null;
  violations: ViolationCatalogItem[];
  confidence?: number | null;
  fine_total: number;
  total_fine?: number;
  evidence_url?: string | null;
  evidence_sha256?: string | null;
  timestamp: string;
  gate_id?: string | null;
  alert_ids: string[];
  skipped?: string[];
}

/** Result of the fully-automatic POST /api/violations/enforce pipeline. */
export interface ViolationEnforceResult {
  case_id: string;
  plate?: string | null;
  confidence?: number | null;
  anpr_decision_path?: string;
  anpr_real?: boolean;
  bbox?: number[] | null;
  degraded?: boolean;
  vehicle?: Record<string, any> | null;
  vehicle_class?: string | null;
  driver?: ViolationDriver | null;
  violations: ViolationCatalogItem[];
  total_fine: number;
  fine_total?: number;
  challan_id?: string | null;
  challan_no?: string | null;
  status?: CaseStatus | string;
  evidence_url?: string | null;
  evidence_sha256?: string | null;
  alert_ids: string[];
  skipped?: string[];
  notification_sent: boolean;
}

/** Payload of the `violation_enforced` WS frame (real-time enforcement event). */
export interface ViolationEnforcedEvent {
  type: "VIOLATION_ENFORCED";
  case_id: string;
  plate?: string | null;
  vehicle?: Record<string, any> | null;
  driver?: ViolationDriver | null;
  violations: ViolationCatalogItem[];
  fine: number;
  challan_no?: string | null;
  status?: string;
  evidence_url?: string | null;
  alert_ids: string[];
  ts: string;
}

// Driver enrolment request lifecycle (Driver PWA submit -> admin approve).
export type EnrollmentStatus = "PENDING" | "ACTIVE" | "REJECTED" | "REENROLL" | string;

export interface DriverEnrollment {
  driver_id: string;
  name: string;
  license_no?: string;
  mobile?: string;
  vehicle_no?: string;
  aadhaar_masked?: string;
  emergency_contact?: string;
  status: EnrollmentStatus;
  consent?: boolean;
  consent_at?: string | null;
  /** List thumbnail: MinIO photo URL, else the first captured frame (data-URL). */
  photo?: string | null;
  photo_url?: string | null;
  /** Captured reference frames — only present on the detail fetch. */
  face_images?: string[];
  documents?: { kind: string; image: string }[];
  template_dim?: number | null;
  provider?: string | null;
  submitted_at?: string;
  reviewed_at?: string | null;
  reviewed_by?: string | null;
  rejection_reason?: string | null;
}

// Parking (/api/parking)
export interface ParkingFacility {
  facility_id: string;
  name: string;
  gate_id?: string | null;
  lat: number;
  lon: number;
  capacity: number;
  occupied: number;
  available: number;
  utilisation_pct: number;
  status: "AVAILABLE" | "FILLING" | "FULL" | string;
}

export interface ParkingSummary {
  total_capacity: number;
  total_occupied: number;
  total_available: number;
  facilities: number;
  full_count: number;
}

// --- Terminal Appointment System (gateway /api/tas/slots) ---
export interface TasSlot {
  slot_id: string;
  gate_id: string;
  start: string; // ISO timestamp
  status: "BOOKED" | "RESCHEDULED" | "CANCELLED" | string;
  rescheduled_to?: string | null;
}

// --- Fault-injection / control surface (gateway /api/control/fault) ---
// Mirrors the gateway responses 1:1 so the Demo Console behaves identically in
// mock and live mode. `forced_rung === null` means the chain is on its natural
// LIVE/PRIMARY rung; severity is null until a rung is forced.
export type FaultSeverity = "GREEN" | "AMBER" | "RED";

export interface FaultDomainState {
  forced_rung: string | null;
  severity: FaultSeverity | null;
}

export interface FaultState {
  domains: {
    camera: FaultDomainState;
    vahan: FaultDomainState;
    trucks: FaultDomainState;
  };
  rungs: {
    camera: string[];
    vahan: string[];
    trucks: string[];
  };
}

// The operator banner is echoed by force/clear responses AND pushed live over
// the WS as an `operator_banner` frame (see SocketContext).
export interface OperatorBanner {
  active: boolean;
  domains: string[];
  severity: FaultSeverity | null;
}

// POST /api/control/fault/{domain} and DELETE responses share this shape.
export interface FaultControlResult {
  forced?: Record<string, string>;
  cleared?: string;
  banner: OperatorBanner;
}

// WebSocket frame shapes (gateway/routers/ws.py + scenario_ext.py).
export type WsFrame =
  | { type: "hello"; payload: { service: string; channels: string[] } }
  | { type: "alert"; payload: Alert }
  | { type: "traffic"; payload: TrafficSnapshot }
  | {
      type: "truck_position";
      payload: { device_id: string; plate?: string; lat: number; lon: number; speed_kmh?: number };
    }
  | { type: "decision"; payload: Decision }
  | { type: "scenario_step"; payload: ScenarioStep }
  | { type: "operator_banner"; payload: OperatorBanner }
  | { type: "violation_enforced"; payload: ViolationEnforcedEvent };

// --- FASTag (ULIP) — mirrors gateway/routers/fastag.py response models ---
export interface FastagBalance {
  rc_number?: string | null;
  tag_id?: string | null;
  available_balance?: string | null;
  tag_status?: string | null;
  updated?: boolean;
  correlation_id: string;
  provider_name?: string | null;
  provider_code?: string | null;
  customer_name?: string | null;
  available_recharge_limit?: string | null;
  vehicle_class?: string | null;
  vehicle_class_desc?: string | null;
  model_name?: string | null;
}

export interface TollPlaza {
  name?: string | null;
  cost?: string | null;
  lat?: number | null;
  lng?: number | null;
}

export interface TollEnroute {
  id: string;
  source?: string | null;
  destination?: string | null;
  distance?: string | null;
  duration?: string | null;
  plaza_count: number;
  toll_plaza_details: TollPlaza[];
  correlation_id: string;
}

export interface TollEnrouteInput {
  source_state: string;
  source_name: string;
  destination_state: string;
  destination_name: string;
  vehicle_type: string;
}

export interface FastagTransactionRow {
  seq_no?: string | null;
  transaction_date_time?: string | null;
  toll_plaza_name?: string | null;
  toll_plaza_geocode?: string | null;
  vehicle_type?: string | null;
  lane_direction?: string | null;
  bank_name?: string | null;
  status?: string | null;
}

export interface FastagTransactions {
  inserted_count: number;
  skipped_count: number;
  failed_count: number;
  total: number;
  correlation_id: string;
  transactions: FastagTransactionRow[];
}

export interface FastagHealth {
  module: string;
  status: string;
  ulip_configured: boolean;
  db: string;
  tables: Record<string, boolean>;
}
