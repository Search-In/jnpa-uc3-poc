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
  // Provenance: "live" = aggregated from real event data; "baseline" = no data
  // yet, showing the configured placeholder. Optional so mock fixtures (which
  // are demonstrative by construction) default to "live" in the demo build.
  source?: "live" | "baseline";
  // Sample count behind a live value (trips/vehicles aggregated).
  n?: number;
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

export interface IdentityEnrollResult {
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

// Driver enrollment request lifecycle (Driver PWA submit -> admin approve).
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
  /** Provenance: "PWA" (driver self-service) or "ADMIN" (Control-Room created). */
  source?: string | null;
  /** Admin actor who created the profile (ADMIN source only). */
  created_by?: string | null;
  submitted_at?: string;
  reviewed_at?: string | null;
  reviewed_by?: string | null;
  rejection_reason?: string | null;
}

// A fleet vehicle available for admin assignment (Control-Room dropdown).
export interface AvailableVehicle {
  vehicle_id: string;
  plate?: string | null;
  vehicle_type?: string | null;
  state?: string | null;
}

// Vehicle Master lifecycle status.
export type VehicleStatus = "ACTIVE" | "INACTIVE" | "MAINTENANCE" | string;

// A registered vehicle in the Vehicle Master (jnpa.fleet_vehicles).
export interface FleetVehicle {
  vehicle_id: string;
  vehicle_number?: string | null;
  vehicle_type?: string | null;
  chassis_number?: string | null;
  rfid_fastag_id?: string | null;
  status: VehicleStatus;
  created_by?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  /** Active driver currently assigned this vehicle (joined server-side). */
  assigned_driver?: { driver_id: string; name?: string | null } | null;
}

// Vehicle Master dashboard counters.
export interface VehicleStats {
  total: number;
  active: number;
  assigned: number;
  available: number;
}

// Payload to register a vehicle in the master. The Vehicle ID is generated by the
// backend (TRK sequence) and is never sent by the client; vehicle_number (plate)
// is the required, dedup'd identifier.
export interface CreateVehicleInput {
  vehicle_number: string;
  vehicle_type?: string;
  chassis_number?: string;
  rfid_fastag_id?: string;
  status?: VehicleStatus;
}

// Patch a vehicle's editable fields / status.
export interface UpdateVehicleInput {
  vehicle_number?: string;
  vehicle_type?: string;
  chassis_number?: string;
  rfid_fastag_id?: string;
  status?: VehicleStatus;
}

// Payload for admin-originated driver-profile creation.
export interface CreateDriverInput {
  name: string;
  vehicle_no: string;
  license_no?: string;
  mobile?: string;
  emergency_contact?: string;
}

// Vehicle Intelligence — Identity face-match result (POST /api/vehicle/{n}/identity).
export interface VehicleIdentityResult {
  driver_name: string | null;
  driver_id?: string | null;
  vehicle_number?: string | null;
  vehicle_id?: string | null;
  confidence: number;
  status: "MATCHED" | "NOT_MATCHED" | string;
  matched: boolean;
  decision?: string;
  reason?: string | null;
  message?: string;
}

// Vehicle Intelligence — ANPR detection result (POST /api/vehicle/detection).
export interface VehicleDetectionResult {
  detected_vehicle: string | null;
  confidence: number;
  /** null when no expected plate was supplied (client compares instead). */
  match: boolean | null;
  expected?: string | null;
  decision_path?: string;
  message?: string;
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
  /** Store the returned rows came from — "RDS" (persisted history) normally. */
  source?: string;
  /** Where the underlying refresh came from — "LIVE" (real ULIP) or "SIM". */
  fetch_source?: string;
  rc_number?: string | null;
  stored_count?: number;
}

export interface FastagHealth {
  module: string;
  status: string;
  ulip_configured: boolean;
  db: string;
  tables: Record<string, boolean>;
}

// --- Customs & Gate systems (RDS-backed: jnpa.gate_captures / leo_reconciliation / alerts) ---
export interface GateCapture {
  id: number;
  capture_type: "ESEAL" | "FORM13" | "WEIGHBRIDGE" | "ICEGATE";
  container_no: string | null;
  vehicle_plate: string | null;
  gate_id: string | null;
  source_mode: string;
  status: string | null;
  captured_at: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface LeoReconciliation {
  id: number;
  container_no: string | null;
  vehicle_plate: string | null;
  leo_ready: boolean;
  customs_flags: string[];
  checks: Record<string, unknown>;
  source_mode: string;
  reconciled_at: string;
}

export interface CustomsAlert {
  id: string;
  ts: string;
  kind: string;
  severity: string;
  plate: string | null;
  payload: Record<string, unknown>;
  ack: boolean;
}

// --- Parking Management (RDS-backed) ---
export interface ParkingFacilityRow {
  facility_id: string;
  name: string | null;
  lat: number | null;
  lon: number | null;
  gate_id: string | null;
  capacity: number;
  occupied: number;
  available: number;
  free_pct: number | null;
  status: string;
}
export interface ParkingMgmtSummary {
  source?: string;
  capacity: number;
  occupied: number;
  available: number;
  facilities: number;
  full: number;
}
export interface ParkingAllocation {
  allocated: boolean;
  facility_id?: string;
  slot_number?: string;
  slot_id?: number;
  transaction_id?: number;
  entry_time?: string;
  reason?: string;
}
export interface ParkingTransaction {
  id: number;
  vehicle_id: string | null;
  driver_id: string | null;
  facility_id: string | null;
  slot_id: number | null;
  entry_time: string | null;
  exit_time: string | null;
  duration_s: number | null;
  status: string;
}
export interface ParkingViolation {
  id: number;
  event_type: string;
  vehicle_id: string | null;
  facility_id: string | null;
  detail: Record<string, unknown>;
  created_at: string;
}

// --- Empty Container Allocation (RDS-backed) ---
export interface ContainerInventory {
  container_id: string;
  container_type: string | null;
  location: string | null;
  owner: string | null;
  availability_status: string;
  updated_at: string;
}
export interface ContainerAllocateInput {
  container_type: string;
  truck_id?: string;
  trailer_id?: string;
  driver_id?: string;
  shipping_line?: string;
  cargo_type?: string;
  allocation_reason?: string;
}
export interface ContainerAllocation {
  allocated?: boolean;
  id?: number;
  allocation_id?: number;
  container_id: string | null;
  truck_id: string | null;
  trailer_id: string | null;
  driver_id: string | null;
  shipping_line: string | null;
  cfs: string | null;
  ecd: string | null;
  allocation_reason: string | null;
  allocated_at: string | null;
  status?: string;
}

// --- Geo-fence enforcement (RDS-backed) ---
export interface GeoVehicleInZone {
  vehicle_id: string;
  zone_id: string;
  entry_time: string;
  dwell_s: number;
  violated: boolean;
}
export interface GeofenceEvent {
  id: number;
  vehicle_id: string | null;
  driver_id: string | null;
  zone_id: string | null;
  event_type: string | null;
  entry_time: string | null;
  exit_time: string | null;
  dwell_seconds: number | null;
  violation_type: string | null;
  action_taken: string | null;
  created_at: string;
}
export interface AiEvent {
  id: number;
  event_type: string;
  vehicle_id: string | null;
  driver_id: string | null;
  location: Record<string, unknown>;
  payload: Record<string, unknown>;
  created_at: string;
}

// --- Vehicle & Driver Intelligence (RDS-backed aggregates) ---
export interface VehicleIntel {
  vehicle_number: string;
  rc: Record<string, unknown> | null;
  tracking: { ts: string; lat: number; lon: number; speed_kmh: number }[];
  violations: Record<string, unknown>[];
  challans: Record<string, unknown>[];
  alerts: Record<string, unknown>[];
  verification_history: Record<string, unknown>[];
}
export interface DriverIntel {
  driver_key: string;
  driver: Record<string, unknown> | null;
  dl_history: Record<string, unknown>[];
  activity: Record<string, unknown>[];
  vehicle_no: string | null;
  violations: Record<string, unknown>[];
}
