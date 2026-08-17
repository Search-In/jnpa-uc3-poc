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
  /**
   * Movement state, or null when the gateway has not classified the device yet.
   *
   * This was typed `string`, which is why the compiler never flagged the
   * `.toLowerCase()` that blanked /live: the type asserted a guarantee the API
   * does not make. Every consumer must handle null.
   */
  state: string | null;
  /** Null for a device that has never reported a position (see `source`). */
  position: { lat: number; lon: number } | null;
  speed_kmh: number | null;
  heading: number | null;
  /**
   * Distance still to run to the gate, or null when it was never measured.
   *
   * Nullable because a `pwa-registered` device is a registration, not a
   * position fix: the gateway sends null rather than 0, and the UI must render
   * "—". Typing this `number` is what would let a `.toFixed()` print "0.0 km"
   * for a distance nobody measured.
   */
  remaining_km: number | null;
  eta_s: number | null;
  segment_id?: string | null;
  /**
   * Provenance. `truck-sim` = a synthetic simulator truck; `pwa-registered` = a
   * device a real driver is signed in on (core.push_subscription). The console
   * must never present one as the other.
   */
  source?: string | null;
  /** Present on `pwa-registered` devices with an ACTIVE assigned driver. */
  driver_id?: string | null;
  driver_name?: string | null;
  /** ISO timestamp of the last real telemetry fix, when there is one. */
  last_seen?: string | null;
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

// --- UC3-003: empty-container TRT (KPI 3) wire types -----------------------
// Mirrors services/cfs_ecy/trt_service.py::EmptyTrtService.kpi(). Every number
// is derived from the imported CFS/ECY CODECO gate log — none is configured in
// the UI except the target/baseline, which come from jnpa_shared/kpi.py.

export interface EmptyTrtChain {
  container_no: string;
  ecy_out_ts: string | null;
  ecy_in_ts: string | null;
  cfs_in_ts: string | null;
  cfs_out_ts: string | null;
  chain_status: "COMPLETE" | "PARTIAL" | "ORPHAN";
  legs_present: number;
  event_count: number;
  ecy_out_events: number;
  ecy_in_events: number;
  cfs_in_events: number;
  cfs_out_events: number;
  trt_min: number | null;
  dwell_min: number | null;
  cycle_min: number | null;
  anomaly_codes: string[];
  anomaly_labels?: string[];
}

export interface DqIssue {
  issue_id: number;
  file_id: number | null;
  source_path?: string | null;
  source_table: string | null;
  record_ref: string | null;
  issue_type: string;
  severity: "info" | "warn" | "error";
  description: string;
  detected_at: string;
}

export interface EmptyTrtResponse {
  kpi: KpiResult;
  definition: {
    key: string;
    label: string;
    measure: string;
    unit: string;
    target: number;
    baseline: number;
    direction: "lower_is_better" | "higher_is_better";
    eligible: string;
  };
  distribution: {
    valid_containers: number;
    avg_trt_min: number | null;
    median_trt_min: number | null;
    min_trt_min: number | null;
    max_trt_min: number | null;
    avg_dwell_min: number | null;
    avg_cycle_min: number | null;
    window_from: string | null;
    window_to: string | null;
    vs_target_min: number | null;
    vs_baseline_min: number | null;
  };
  chains: { complete: number; partial: number; orphan: number; total: number };
  source: {
    ecy_out_events: number;
    ecy_in_events: number;
    cfs_in_events: number;
    cfs_out_events: number;
    total_events: number;
    ecy_pairing_gap: number;
    cfs_paired: boolean;
    files: {
      file_id: number;
      path: string;
      source_system: string;
      file_format: string;
      row_count: number | null;
      loaded_at: string;
      imported_events: number;
    }[];
  };
  anomalies: { code: string; containers: number; label: string }[];
  data_quality: DqIssue[];
  daily: { day: string; containers: number; avg_trt_min: number | null }[];
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
  // Vehicles counted per class. Optional because an older carbon service (or a
  // cached upstream response) predates it — the tile then shows the fleet total
  // and says the category breakdown is unavailable rather than inventing one.
  vehicles_by_class?: Record<string, number>;
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

// Customs document view for one container (GET /api/customs/containers/{cn}).
// Aggregates the existing customs_* tables (module 5) — surfaced in the ICEGATE
// details drawer on the Customs & Gate page. All fields are read-only.
export interface CustomsContainerStatus {
  container_no: string;
  igm_no: string | null;
  declared_igm: boolean | null;
  rms_selected: boolean | null;
  ooc_cleared: boolean | null;
  smtp_bonded: boolean | null;
}

export interface CustomsContainerVessel {
  igm_no: string | null;
  igm_date: string | null;
  vessel_code: string | null;
  voyage_no: string | null;
  shipping_line_code: string | null;
  port_of_arrival: string | null;
  expected_arrival: string | null;
  entry_inward: string | null;
  message_id: number | null;
}

export interface CustomsEvent {
  id: number;
  event: string;
  module: string;
  reference: string | null;
  container_no: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface CustomsContainerView {
  container_no: string;
  status: CustomsContainerStatus | null;
  vessel: CustomsContainerVessel | null;
  message_id: number | null;
  igm: Array<{
    igm_no: string;
    line_no: string | number | null;
    container_no: string;
    seal_no: string | null;
    container_status: string | null;
    iso_size_type: string | null;
  }>;
  ooc: Array<{
    bill_of_entry_no: string | null;
    out_of_charge_no: string | null;
    out_of_charge_date: string | null;
    importer_name: string | null;
  }>;
  smtp: Array<{
    smtp_no: string | null;
    bond_no: string | null;
    destination_code: string | null;
    consignee_name: string | null;
  }>;
  rms: Array<{
    igm_no: string | null;
    scan_machine: string | null;
    scan_location: string | null;
    cfs_name: string | null;
  }>;
  workflow: {
    import_stage: "MANIFESTED" | "SCAN_SELECTED" | "OUT_OF_CHARGE" | null;
    transhipment: "BONDED" | null;
    cleared_for_release: boolean;
  };
  last_event: CustomsEvent | null;
  import_export: "IMPORT" | "TRANSHIPMENT" | null;
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
  // UC3-030: the gateway attaches the issuance disclosure to every
  // challan-bearing response, so a client cannot render a challan number without
  // the SIMULATED badge that qualifies it (assumption A5).
  issuance_mode?: "SIMULATED";
  badge?: "SIMULATED";
  is_legal_instrument?: false;
  authority_note?: string;
  assumption_ref?: string;
  disclosure?: string;
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
  // UC3-030: the gateway attaches the issuance disclosure to every
  // challan-bearing response, so a client cannot render a challan number without
  // the SIMULATED badge that qualifies it (assumption A5).
  issuance_mode?: "SIMULATED";
  badge?: "SIMULATED";
  is_legal_instrument?: false;
  authority_note?: string;
  assumption_ref?: string;
  disclosure?: string;
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
  /** Explicit alias for `plate`; both are sent so older callers keep working. */
  vehicle_number?: string | null;
  vehicle_type?: string | null;
  state?: string | null;
  /** Driver bound to this truck in core.driver_identity, null when unassigned. */
  driver_id?: string | null;
  driver_name?: string | null;
  /** That driver's licence — the PERSON behind the record. The available-driver
   *  list carries one record per licence, so the binding is matched to it by
   *  licence; a Driver ID alone cannot tell that the same person is listed under
   *  another record. */
  driver_licence?: string | null;
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

/** Wire shape of the focus frame — mirrors gateway/routers/focus.py PortFocus. */
export interface PortFocusFrame {
  vcn?: string | null;
  viaNo?: string | null;
  imoNo?: string | null;
  vesselName?: string | null;
  containerNo?: string | null;
  vehicleNo?: string | null;
  igmNo?: string | null;
  fromDate?: string | null;
  toDate?: string | null;
  asOf?: string | null;
  origin: "UC-1" | "UC-2" | "UC-3" | "SUITE";
  nonce: number;
}

// WebSocket frame shapes (gateway/routers/ws.py + scenario_ext.py + focus.py).
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
  | { type: "violation_enforced"; payload: ViolationEnforcedEvent }
  // The port-wide entity focus, relayed by POST /api/focus/broadcast. This is
  // how a vessel selected in UC-1 or UC-2 reaches this app: the three
  // dashboards are on different origins, so no browser-local channel can do it.
  | { type: "focus"; payload: PortFocusFrame }
  // --- frames the gateway emits that this app does not act on (GAP-WS-02) ---
  //
  // These were emitted and silently dropped: a `default:` branch discarded them
  // and the union did not admit they existed, so nothing distinguished "we chose
  // not to handle this" from "we forgot". Each screen below owns its own polled
  // read of the same data, so no behaviour depends on the frame — but the frame
  // IS sent, and a future handler should start from a type that already matches
  // what the server puts on the wire rather than from a guess.
  //
  // Payload shapes are taken from the emit sites, not invented; every field is
  // optional because each of these frames carries several distinct `type`
  // discriminators inside one channel (e.g. "tas_booking" vs
  // "deferred_arrival_applied") and modelling them as required would be a lie
  // about which arrive together.
  | { type: "accident"; payload: AccidentFrame }        // routers/accidents.py
  | { type: "bottleneck"; payload: BottleneckFrame }    // routers/bottlenecks.py
  | { type: "camera_ai"; payload: CameraAiFrame }       // routers/camera_ai.py
  | { type: "double_trip"; payload: DoubleTripFrame }   // routers/double_trip.py
  | { type: "reroute_ack"; payload: RerouteAckFrame }   // routers/trucks.py
  | { type: "tas"; payload: TasFrame }                  // crosstwin.py, rms_tas.py
  | { type: "trt"; payload: TrtFrame };                 // routers/trt.py
//
// NOTE: there is deliberately no `anpr` member. `anpr_pump` in gateway/main.py
// is constructed with `broadcast=False` — ANPR reads are persisted to
// core.anpr_read and never reach a socket. Adding it would model a frame that
// cannot arrive.

/** Accident raised or updated — routers/accidents.py:133,275. */
export interface AccidentFrame {
  type?: "accident_update" | string;
  accident_id?: string | number;
  plate?: string | null;
  severity?: string | null;
  location?: string | null;
  status?: string | null;
  [k: string]: unknown;
}

/** Ranked corridor bottleneck board — routers/bottlenecks.py:263. */
export interface BottleneckFrame {
  segment_id?: string;
  name?: string | null;
  rank?: number;
  jam_factor?: number;
  segments?: Array<Record<string, unknown>>;
  [k: string]: unknown;
}

/** One camera-AI count row — routers/camera_ai.py:167. */
export interface CameraAiFrame {
  camera_id?: string;
  gate_id?: string | null;
  ts?: string;
  [k: string]: unknown;
}

/** Double-trip cycle progress — routers/double_trip.py:179. */
export interface DoubleTripFrame {
  trip_id?: string;
  cycle_id?: string;
  completed_count?: number;
  trip_count?: number;
  is_double_trip?: boolean;
  [k: string]: unknown;
}

/** Driver acknowledged a reroute — routers/trucks.py:595. Addressed to one
 *  device_id, so a shared listener sees other drivers' acks too. */
export interface RerouteAckFrame {
  device_id?: string;
  state?: string;
  [k: string]: unknown;
}

/** TAS slot book changed — either a booking (rms_tas.py:252) or a UC-II
 *  deferred-arrival window applied across the twins (crosstwin.py:109). */
export interface TasFrame {
  type?: "tas_booking" | "deferred_arrival_applied" | string;
  booking_id?: string | number;
  slot_code?: string;
  vehicle_id?: string;
  correlation_id?: string;
  gate_id?: string;
  window_start?: string;
  window_end?: string;
  slot_cap?: number;
  applied_slots?: unknown[];
  transport?: string;
  [k: string]: unknown;
}

/** Terminal round-trip time completed — routers/trt.py:170. */
export interface TrtFrame {
  type?: "trt_completed" | string;
  record_id?: string | number;
  vehicle_id?: string;
  plate?: string | null;
  trip_id?: string | null;
  trt_min?: number | null;
  gate_to_park_min?: number | null;
  park_to_load_min?: number | null;
  load_to_out_min?: number | null;
  [k: string]: unknown;
}

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
  // ULIP grants no wallet-balance API, so this surface can only replay a
  // stored snapshot. `data_available: false` with
  // `source: "NOT_PROVIDED_BY_ULIP"` is the honest answer for an RC we hold
  // nothing for — render that state, never a zero balance.
  data_available?: boolean;
  source?: string | null;
}

export interface FastagTagRow {
  tag_id?: string | null;
  rc_number?: string | null;
  tid?: string | null;
  vehicle_class?: string | null;
  tag_status?: string | null;
  issue_date?: string | null;
  exc_code?: string | null;
  bank_id?: string | null;
  commercial_vehicle?: string | null;
}

export interface FastagTagStatus {
  rc_number?: string | null;
  tag_id?: string | null;
  count: number;
  tags: FastagTagRow[];
  correlation_id: string;
}

export interface GatiShaktiRow {
  state_id?: string | null;
  nh_no?: string | null;
  name?: string | null;
  latitude?: number | null;
  longitude?: number | null;
  /** Which of GATISHAKTI/01..04 produced the row. GATISHAKTI/01 (highway
   *  attributes) and /02 (food-storage depots) share one table and share no
   *  fields, so this is what tells them apart. */
  source_api?: string | null;
  /** The upstream item, kept whole — the four APIs publish different field
   *  names, so the columns above hold only what all of them have in common
   *  and everything else (lane count, storage capacity, district…) is here. */
  detail?: Record<string, unknown> | null;
  fetched_at?: string | null;
}

/** One entry of /api/gatishakti/nh-numbers — a highway /01 is seeded for. */
export interface GatiShaktiNhNumber {
  nh_no?: string | null;
  segments?: number | null;
  fetched_at?: string | null;
}

export interface GatiShaktiRows {
  rows: GatiShaktiRow[];
  count: number;
  data_available: boolean;
  path: string;
  source: string;
  status: string;
  as_of: string;
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
// GET /api/parking/summary — the gateway maps the RDS rollup through
// `_summary_contract`, which emits the shared ParkingBoard header contract
// (total_* / full_count), NOT capacity/occupied/full. Typed accurately here so a
// future consumer reads the real field names. (The Parking Management KPI cards
// no longer depend on this — they roll up the per-facility availability data.)
export interface ParkingMgmtSummary {
  source?: string;
  decision_path?: string;
  total_capacity: number;
  total_occupied: number;
  total_available: number;
  full_count: number;
  utilization_pct?: number;
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
/** /api/vahan/vehicle-intel/{plate}. Every field is optional on purpose: the
 *  gateway answers 200 with `{}` when it has no DSN, and each of the six lookups
 *  degrades independently, so a field can be absent or null on a successful
 *  response. Callers must normalise before dereferencing. */
export interface VehicleIntel {
  vehicle_number?: string;
  rc?: Record<string, unknown> | null;
  tracking?: { ts: string; lat: number; lon: number; speed_kmh: number }[] | null;
  violations?: Record<string, unknown>[] | null;
  challans?: Record<string, unknown>[] | null;
  alerts?: Record<string, unknown>[] | null;
  verification_history?: Record<string, unknown>[] | null;
}
/** Document expiry verdict from /api/vahan/vehicle-360 (VALID | EXPIRING |
 *  EXPIRED | NOT_AVAILABLE). */
export interface DocumentValidity {
  status?: string | null;
  valid_to?: string | null;
  days_left?: number | null;
}

/** /api/vahan/vehicle-360/{plate} — the operator's single-screen vehicle view.
 *  Aggregates the vehicle master, its assigned driver, that driver's licence/PDP,
 *  the transport company, RC compliance, alerts and the lifecycle timeline.
 *  Nullable throughout: a card whose source row is missing comes back as null
 *  rather than as fabricated values. */
export interface Vehicle360 {
  plate?: string;
  found?: boolean;
  vehicle?: {
    number?: string | null;
    id?: string | null;
    status?: string | null;
    class?: string | null;
    fuel?: string | null;
    type?: string | null;
    chassis_number?: string | null;
    rfid_fastag_id?: string | null;
    registered_at?: string | null;
    assignment_status?: string | null;
    in_master?: boolean;
  } | null;
  driver?: {
    id?: string | null;
    name?: string | null;
    photo?: string | null;
    mobile?: string | null;
    dob?: string | null;
    status?: string | null;
    enrollment_status?: string | null;
    enrolled_at?: string | null;
    license?: {
      number?: string | null;
      type?: string | null;
      valid_until?: string | null;
      validity?: DocumentValidity | null;
      pdp_number?: string | null;
      pdp_status?: string | null;
      pdp_valid_until?: string | null;
      verification_status?: string | null;
      verified_at?: string | null;
      verification_score?: number | null;
      in_master?: boolean;
    } | null;
  } | null;
  transporter?: {
    id?: number | string | null;
    name?: string | null;
    code?: string | null;
    status?: string | null;
    gstin?: string | null;
    contact?: string | null;
    blacklisted?: boolean;
    blacklist_reason?: string | null;
    mapped_at?: string | null;
    source?: string | null;
  } | null;
  compliance?: {
    rc?: Record<string, unknown> | null;
    insurance?: DocumentValidity | null;
    puc?: DocumentValidity | null;
    fitness?: DocumentValidity | null;
    blacklist?: { status?: string | null; source?: string | null; reason?: string | null } | null;
    fastag?: { status?: string | null } | null;
  } | null;
  alerts?: Record<string, unknown>[] | null;
  timeline?:
    | { stage?: string; label?: string; ts?: string | null; detail?: string | null }[]
    | null;
  intel?: {
    rc?: Record<string, unknown> | null;
    tracking?: { ts: string; lat: number; lon: number; speed_kmh: number }[] | null;
    violations?: Record<string, unknown>[] | null;
    challans?: Record<string, unknown>[] | null;
    verification_history?: Record<string, unknown>[] | null;
  } | null;
  jobs?: Record<string, unknown>[] | null;
  gate_events?: Record<string, unknown>[] | null;
}

export interface DriverIntel {
  driver_key: string;
  driver: Record<string, unknown> | null;
  dl_history: Record<string, unknown>[];
  activity: Record<string, unknown>[];
  vehicle_no: string | null;
  violations: Record<string, unknown>[];
}

/** NLDS/LDB truck Port Events payload used by Vehicle Management → Track. */
export interface LdbTruckEvent {
  eventName?: string;
  locName?: string;
  containerNumber?: string;
  eventTime?: string | number;
  eventTimeLabel?: string;
  dateMarker?: string;
  transportMode?: string;
  locLat?: string;
  locLong?: string;
}

export interface LdbTruckTracking {
  truckNumber: string;
  truckType?: string;
  alert?: string | null;
  latest?: LdbTruckEvent | null;
  events: LdbTruckEvent[];
  terminals: Array<{ locName: string; events: LdbTruckEvent[] }>;
  /** LIVE LDB Vahan compliance (GET /vahan/get/vahanDetails/{plate}). Never mocked. */
  compliance?: {
    status: "COMPLIANT" | "NON_COMPLIANT" | "UNKNOWN";
    vehicleClass?: string | null;
    fuelType?: string | null;
    fitnessValidUpto?: string | null;
    taxValidUpto?: string | null;
    insuranceValidUpto?: string | null;
    pucValidUpto?: string | null;
    permitValidUpto?: string | null;
    vehicleCategory?: string | null;
    source?: string;
  } | null;
}

export interface LdbTruckTrackingResponse {
  source: string;
  tracking: LdbTruckTracking;
}
// --- Weather (Open-Meteo weather + marine, /api/weather) ---------------------
// Mirrors services/weather/service.py's response contract. `status` / `source` /
// `decision_path` carry the LIVE → CACHED → SYNTHETIC fallback provenance; the
// endpoint never 5xxes for an upstream outage, it degrades and says so here.
export interface WeatherBlock {
  temperature: number | null; // °C
  wind_speed: number | null; // km/h
  wind_direction: number | null; // degrees
  wind_gusts: number | null; // km/h
  visibility: number | null; // metres
  precipitation: number | null; // mm
  weather_code: number | null; // WMO code
  condition: string | null;
  observed_at: string | null;
  synthetic?: boolean;
}
export interface MarineBlock {
  wave_height: number | null; // metres
  wave_period: number | null; // seconds
  swell_wave_height: number | null; // metres
  sea_level_height: number | null; // metres
  observed_at: string | null;
  synthetic?: boolean;
}
// OpenWeatherMap enrichment (integrations/openweather). `null` on the parent
// response when the provider is disabled (no OPENWEATHER_API_KEY configured) —
// the surface then behaves exactly as the Open-Meteo-only build.
export interface OpenWeatherBlock {
  temperature: number | null; // °C
  feels_like: number | null; // °C
  humidity: number | null; // %
  pressure: number | null; // hPa
  rain: number | null; // mm over the last hour (0 = not raining)
  clouds: number | null; // % cloud cover
  condition: string | null; // e.g. "Cloudy"
  condition_id: number | null; // OpenWeatherMap condition code
  description: string | null; // e.g. "scattered clouds"
  label: string | null; // operational label: CLEAR/CLOUDY/RAIN/STORM/…
  wind_speed: number | null; // km/h (converted from m/s)
  wind_direction: number | null; // degrees
  visibility: number | null; // metres
  station: string | null;
  observed_at: string | null;
  // Cross-provider temperature validation vs the Open-Meteo block.
  temperature_delta?: number | null; // °C (openweather − open-meteo)
  temperature_consistent?: boolean | null; // |delta| within tolerance
  synthetic?: boolean;
}
export interface WeatherForecastHour {
  time: string;
  temperature: number | null;
  wind_speed: number | null;
  wind_direction: number | null;
  wind_gusts: number | null;
  visibility: number | null;
  precipitation: number | null;
  weather_code: number | null;
  condition: string | null;
}
export interface WeatherCurrent {
  status: "LIVE" | "DEGRADED" | "OFFLINE";
  source: "OPEN_METEO+OPENWEATHER" | "OPEN_METEO" | "OPEN_METEO_CACHE" | "SYNTHETIC";
  decision_path: "LIVE" | "CACHED" | "SYNTHETIC";
  location: { latitude: number; longitude: number };
  weather: WeatherBlock;
  marine: MarineBlock;
  // null when OPENWEATHER_API_KEY is not configured (sources.openweather = "DISABLED").
  openweather: OpenWeatherBlock | null;
  sources: { weather: string; marine: string; openweather: string };
  cache_age_s: number | null;
  units: Record<string, string>;
  timestamp: string;
  forecast?: WeatherForecastHour[];
}
export interface WeatherHealth {
  system: string;
  provider: string; // "OPEN_METEO" | "OPEN_METEO + OPENWEATHER"
  providers: string[];
  configured: boolean;
  api_key_required: boolean;
  weather_url: string;
  marine_url: string;
  timeout_s: number;
  retries: number;
  openweather: {
    configured: boolean;
    api_key_required: boolean;
    url: string;
    timeout_s: number;
    retries: number;
  };
  cache_ttl_s: number;
  default_location: { latitude: number; longitude: number };
}

// --- Traffic (TomTom flow + incidents, /api/traffic/current) -----------------
// Mirrors services/traffic/service.py's response contract. `status` / `source` /
// `decision_path` carry the LIVE → CACHED → DATABASE → SYNTHETIC fallback
// provenance; the endpoint never 5xxes for a TomTom outage, it degrades and
// says so here. Distinct from TrafficSnapshot (per-segment sim map overlay).
export type CongestionLevel = "LOW" | "MEDIUM" | "HIGH" | "SEVERE" | "UNKNOWN";
export interface TrafficBlock {
  current_speed: number | null; // km/h
  free_flow_speed: number | null; // km/h
  current_travel_time: number | null; // seconds
  free_flow_travel_time: number | null; // seconds
  congestion_level: CongestionLevel;
  delay_seconds: number | null; // seconds vs free flow
  road_closure: boolean;
  confidence: number | null; // 0..1 (TomTom flow confidence)
  road_class: string | null; // functional road class, e.g. "FRC0"
  synthetic?: boolean;
}
export interface TrafficIncident {
  type: string; // ACCIDENT / JAM / ROAD_WORKS / ROAD_CLOSED / …
  description: string | null;
  severity: string; // MINOR / MODERATE / MAJOR / CLOSURE / UNKNOWN
  road: string | null;
  delay: number | null; // seconds
}
export interface TrafficCurrent {
  status: "LIVE" | "DEGRADED" | "OFFLINE";
  source: "TOMTOM" | "TOMTOM_CACHE" | "TOMTOM_DB" | "SYNTHETIC";
  decision_path: "LIVE" | "CACHED" | "DATABASE" | "SYNTHETIC";
  location: { latitude: number; longitude: number };
  traffic: TrafficBlock;
  incidents: TrafficIncident[];
  incident_count: number;
  sources: { traffic: string; incidents: string };
  cache_age_s: number | null;
  units: Record<string, string>;
  timestamp: string;
}
export interface TrafficHealth {
  system: string; // "TRAFFIC"
  provider: string; // "TOMTOM"
  configured: boolean;
  api_key_required: boolean;
  flow_url: string;
  incidents_url: string;
  timeout_s: number;
  retries: number;
  cache_ttl_s: number;
  default_location: { latitude: number; longitude: number };
}

// --- Air quality (OpenAQ, /api/air-quality/current) ---------------------------
// Mirrors services/air_quality/service.py's response contract. `status` /
// `source` / `decision_path` carry the LIVE → CACHED → DATABASE → SYNTHETIC
// fallback provenance; the endpoint never 5xxes for an OpenAQ outage, it
// degrades and says so here. All concentrations are µg/m³.
export type AqStatus = "GOOD" | "MODERATE" | "UNHEALTHY" | "VERY_UNHEALTHY" | "UNKNOWN";
export interface AirQualityBlock {
  pm25: number | null;
  pm10: number | null;
  no2: number | null;
  so2: number | null;
  co: number | null;
  o3: number | null;
  air_quality_status: AqStatus;
  source: string; // "OPENAQ" | "SYNTHETIC"
  observed_at: string | null; // newest station timestamp (UTC ISO)
  stations?: string[]; // contributing OpenAQ station names
  synthetic?: boolean;
}
export interface AirQualityCurrent {
  status: "LIVE" | "DEGRADED" | "OFFLINE";
  source: "OPENAQ" | "OPENAQ_CACHE" | "OPENAQ_DB" | "SYNTHETIC";
  decision_path: "LIVE" | "CACHED" | "DATABASE" | "SYNTHETIC";
  location: { latitude: number; longitude: number };
  air_quality: AirQualityBlock;
  cache_age_s: number | null;
  units: Record<string, string>;
  timestamp: string;
}
export interface AirQualityHealth {
  system: string; // "AIR_QUALITY"
  provider: string; // "OPENAQ"
  configured: boolean;
  api_key_required: boolean; // always false — OpenAQ needs no key
  api_key_present: boolean;
  base_url: string;
  timeout_s: number;
  retries: number;
  radius_m: number;
  cache_ttl_s: number;
  default_location: { latitude: number; longitude: number };
}

// --- Logistics (ULIP, /api/logistics/*) ---------------------------------------
// Mirrors services/logistics/service.py's response contract. `status` /
// `source` / `decision_path` carry the LIVE → CACHED → DATABASE → FALLBACK
// provenance; the endpoints never 5xx for a ULIP outage — they degrade and say
// so here. The FALLBACK rung is explicitly EMPTY (data_available: false):
// the logistics surface never fabricates shipment data.
export type LogisticsTrackingStatus = "IN_TRANSIT" | "IDLE" | "UNKNOWN";
export interface LogisticsEvent {
  ref_type: "VEHICLE" | "CONTAINER";
  ref_id: string;
  event_type: string; // "TOLL_CROSSING" | "CONTAINER_MOVEMENT"
  event_ts: string | null;
  location: string | null;
  latitude: number | null;
  longitude: number | null;
  source: string; // "ULIP"
  source_api: string | null; // "FASTAG" | "LDB"
}
export interface LogisticsTrackedRef {
  ref_type: "VEHICLE" | "CONTAINER";
  ref_id: string;
  status: LogisticsTrackingStatus;
  last_event: string | null;
  last_location: string | null;
  last_event_ts: string | null;
  event_count: number;
  updated_at: string | null;
}
export interface LogisticsSummaryBlock {
  window_h: number;
  event_count: number;
  vehicle_count: number;
  container_count: number;
  events_by_type: Record<string, number>;
  last_event_ts: string | null;
  latest_events: LogisticsEvent[];
  tracked: LogisticsTrackedRef[];
  data_available: boolean;
}
export interface LogisticsCurrent {
  status: "LIVE" | "DEGRADED" | "OFFLINE";
  source: "ULIP" | "ULIP_CACHE" | "ULIP_DB" | "NONE";
  decision_path: "LIVE" | "CACHED" | "DATABASE" | "FALLBACK";
  logistics: LogisticsSummaryBlock;
  ulip: {
    configured: boolean;
    last_call_at: string | null;
    last_call_ok: boolean | null;
    fresh: boolean;
  };
  cache_age_s: number | null;
  timestamp: string;
}
export interface LogisticsTrackingBlock {
  ref_id: string;
  ref_type: "VEHICLE" | "CONTAINER";
  tracking_status: LogisticsTrackingStatus;
  last_event: string | null;
  last_location: string | null;
  last_event_ts: string | null;
  event_count: number;
  events: LogisticsEvent[];
  data_available: boolean;
}
export interface LogisticsTracking {
  status: "LIVE" | "DEGRADED" | "OFFLINE";
  source: "ULIP" | "ULIP_CACHE" | "ULIP_DB" | "NONE";
  decision_path: "LIVE" | "CACHED" | "DATABASE" | "FALLBACK";
  tracking: LogisticsTrackingBlock;
  cache_age_s: number | null;
  timestamp: string;
}
export interface LogisticsEventsPage {
  events: LogisticsEvent[];
  count: number;
  total: number;
  limit: number;
  offset: number;
}
export interface LogisticsHealth {
  system: string; // "LOGISTICS"
  provider: string; // "ULIP"
  configured: boolean;
  auth_mode: "static" | "login" | "none";
  api_url: string;
  apis: { vehicle: string; container: string };
  timeout_s: number;
  retries: number;
  cache_ttl_s: number;
  last_call_at: string | null;
  last_call_ok: boolean | null;
  fresh: boolean;
}

// --- UC3-021 Gate & Lane Board ---------------------------------------------
/**
 * One gate card. `queue_vehicles` is COUNTED by video analytics and is null when
 * no camera observation exists — it is never derived from throughput, so a
 * stopped gate shows a rising queue beside zero throughput (UI-068).
 */
export interface GateCard {
  gate_id: string;
  name: string | null;
  lat: number | null;
  lon: number | null;
  closed_at: string | null;
  in_count: number;
  out_count: number;
  throughput_60min: number;
  avg_txn_minutes: number | null;
  txn_samples: number;
  queue_vehicles: number | null;
  queue_status: "COUNTED" | "NO_OBSERVATION";
  queue_count_method: string | null;
  queue_camera_id: string | null;
  queue_observed_at: string | null;
  queue_confidence: number | null;
  congestion_level: "LOW" | "MEDIUM" | "HIGH" | null;
}

export interface GateBoardResponse {
  gates: GateCard[];
  count: number;
  window_minutes: number;
  thresholds: { medium: number; high: number };
  kpi: { queue_length_target: number; queue_length_baseline: number };
  queue_provenance: {
    source_table: string;
    accepted_methods: string[];
    derived_from_throughput: boolean;
    note: string;
  };
}

export interface GateLane {
  lane_id: string;
  gate_id: string;
  lane_no: number;
  lane_type: "IN" | "OUT" | "REVERSIBLE";
  lane_state: "OPEN" | "CLOSED" | "MAINTENANCE";
  boom_barrier: "UP" | "DOWN" | "UNKNOWN";
  updated_at: string;
}

export interface GateConfirmation {
  id: number;
  ts: string;
  gate_id: string;
  plate: string | null;
  device_id: string | null;
  trip_id: string | null;
  event_type: string;
  container_number: string | null;
  bat_lane: string | null;
  source: string | null;
}

export interface LaneReassignPreview {
  lane_id: string;
  gate_id: string;
  from_lane_type: string;
  to_lane_type: string;
  queue_now: number | null;
  queue_projected: number | null;
  queue_delta: number | null;
  congestion_now: string | null;
  congestion_projected: string | null;
  open_lanes_at_gate: number;
  added_capacity_vph: number;
  window_minutes: number;
  throughput_60min: number;
  simulated: boolean;
  method: { added_capacity_vph: number; basis: string; formula: string };
  /** Always "HUMAN_TASK" — applying never commands gate equipment (UI-103). */
  applies_as: string;
  sends_equipment_command: false;
}

export interface LaneReassignTask {
  task_id: string;
  gate_id: string;
  lane_id: string;
  from_lane_type: string;
  to_lane_type: string;
  reason: string | null;
  impact_preview: Record<string, unknown>;
  status: "PENDING" | "ACKNOWLEDGED" | "DONE" | "CANCELLED";
  assigned_to: string;
  created_by: string | null;
  created_at: string;
  acknowledged_by: string | null;
  acknowledged_at: string | null;
  dispatched_to_equipment: false;
}

// --- UC3-027 CPP metered release -------------------------------------------
export interface CppReleasePlan {
  terminal_code: string;
  gate_id: string | null;
  gate_queue_vehicles: number;
  clearing_rate_vph: number;
  release_rate_vph: number;
  hold_minutes: number;
  congestion_level: "LOW" | "MEDIUM" | "HIGH";
  advice_text: string;
  mode: "METERED" | "UNIFORM";
  simulated: boolean;
  /** Present on freshly computed plans and re-attached to persisted ones; still
   *  optional so a plan from an older row can never crash the board. */
  method?: Record<string, string | number>;
}

export interface CppZone {
  facility_id: string;
  zone: string | null;
  location: Record<string, unknown> | string | null;
  capacity: number;
  occupied: number;
  available: number;
  utilisation: number | null;
  status: string | null;
}

export interface CppBoardResponse {
  zones: CppZone[];
  zone_count: number;
  totals: { capacity: number; occupied: number; available: number };
  dwell_histogram: Array<{
    bucket: number;
    trucks: number;
    min_minutes: number | null;
    max_minutes: number | null;
  }>;
  dwell_status: "OK" | "NO_DATA";
  release_plans: CppReleasePlan[];
  amenities: { status: string; note: string };
  occupancy_source: string;
}

// --- UC3-040 Auto-LEO four-way join ----------------------------------------
export type LeoSourceState = "MATCH" | "MISMATCH" | "MISSING";

export interface AutoLeoRow {
  container_no: string;
  vehicle_plate: string | null;
  leo_ready: boolean;
  customs_flags: string[];
  sources: Record<"eseal" | "form13" | "weighbridge" | "icegate", LeoSourceState>;
  checks: Record<string, unknown>;
  evidence: Record<
    string,
    {
      capture_id: number;
      captured_at: string | null;
      status: string | null;
      source_mode: string | null;
      provenance: "REAL" | "SIMULATED";
      evidence_uri: string | null;
      payload: Record<string, unknown>;
    }
  >;
  form13_document: {
    doc_variant: string;
    doc_ref: string | null;
    vehicle_no: string | null;
    custom_seal_no: string | null;
    line_seal_no: string | null;
    declared_wt_kg: number | null;
    data_origin: string | null;
  } | null;
  anchored_to_real_document: boolean;
  weighbridge_reroute: {
    failed_wb_id: string;
    alternate_wb_id: string | null;
    customs_notified: boolean;
    notified_at: string | null;
  } | null;
}

export interface AutoLeoBoardResponse {
  rows: AutoLeoRow[];
  count: number;
  summary: {
    total: number;
    leo_ready: number;
    blocked: number;
    anchored_to_real_document: number;
    by_flag: Record<string, number>;
  };
  flags: Array<{ flag: string; meaning: string }>;
  weight_tolerance_pct: number;
  assumption: { ref: string; text: string };
}

// --- UC3-024 trip resolver / UC3-025 visit timeline -------------------------
export type EvidenceLabel = "VERIFIED" | "KEY_ONLY" | "NOT_IN_CORPUS";

export interface TripMatch {
  trip_id: string;
  doc_id: number;
  doc_category: string;
  doc_variant: string;
  document_no: string | null;
  pin_no: string | null;
  container_no: string | null;
  vehicle_no: string | null;
  line_seal_no: string | null;
  custom_seal_no: string | null;
  bat_no: string | null;
  terminal_code: string | null;
  terminal_name: string | null;
  terminal_operator: string | null;
  transporter_name: string | null;
  driver_name: string | null;
  driver_licence: string | null;
  vessel_name: string | null;
  voyage: string | null;
  pol: string | null;
  pod: string | null;
  booking_no: string | null;
  cfs: string | null;
  iso_code: string | null;
  gross_weight_kg: number | null;
  yard_position: string | null;
  gate_no: string | null;
  doc_ts: string | null;
  truck_in_ts: string | null;
  truck_out_ts: string | null;
  image_file: string | null;
  source_file: string | null;
  data_origin: string | null;
  attrs: Record<string, string>;
  matched_by: Array<{ column: string; kind: string; value: string }>;
  match_confidence: number;
}

export interface TripSearchResponse {
  query: string;
  status: "RESOLVED" | "AMBIGUOUS" | "NO_MATCH" | "INVALID_INPUT";
  ambiguous?: boolean;
  trips: TripMatch[];
  count: number;
  detected_kind: string;
  resolved_trip_id?: string | null;
  reason?: string | null;
  suggestions?: Array<{
    trip_id: string;
    document_no: string | null;
    container_no: string | null;
    vehicle_no: string | null;
    terminal_code: string | null;
  }>;
  searchable_keys: Array<{ kind: string; label: string; example: string }>;
}

export interface TimelineStep {
  key: string;
  label: string;
  ts: string | null;
  evidence: EvidenceLabel;
  source: string | null;
  detail: string | null;
  dwell_minutes: number | null;
}

export interface TripDetail extends TripMatch {
  timeline: TimelineStep[];
  timeline_summary: {
    total_steps: number;
    verified: number;
    key_only: number;
    not_in_corpus: number;
    in_gate_minutes: number | null;
    note: string;
  };
  evidence_labels: Record<EvidenceLabel, string>;
  documents: Array<{
    trip_id: string;
    doc_id: number;
    doc_category: string;
    doc_variant: string;
    document_no: string | null;
    pin_no: string | null;
    container_no: string | null;
    vehicle_no: string | null;
    terminal_code: string | null;
    doc_ts: string | null;
    image_file: string | null;
    data_origin: string | null;
  }>;
  share_path: string;
}

// --- UC3-036 carbon method + idle delta ------------------------------------
export interface CarbonFactor {
  vehicle_class: string;
  value: number;
  unit: string;
  source: string;
  derivation: string;
}

export interface CarbonMethodBlock {
  metric: string;
  unit: string;
  formula: string;
  assumption_ref: string;
  assumption_text: string;
  activity_data: { input: string; provenance: string; note: string };
  constants?: Array<{
    key: string;
    value: number;
    unit: string;
    source: string;
    basis: string;
  }>;
  factors: CarbonFactor[];
  sources: Record<string, string>;
}

export interface CarbonMethodResponse {
  assumption_ref: string;
  assumption_text: string;
  idle: CarbonMethodBlock;
  moving: CarbonMethodBlock;
  sources: Record<string, string>;
  factors_are_published: boolean;
  activity_data_is_simulated: boolean;
}

export interface CarbonIdleDelta {
  scenario: string | null;
  vehicle_class: string;
  unit: string;
  baseline: { idle_minutes: number; idle_co2e_kg: number; label: string };
  scenario_run: { idle_minutes: number; idle_co2e_kg: number };
  delta_kg: number;
  delta_pct: number | null;
  improvement: boolean;
  idle_factor_gco2e_per_min: number;
  simulated: boolean;
  method: CarbonMethodBlock;
}

// --- UC3-030 e-Challan disclosure ------------------------------------------
/** Attached by the gateway to every challan-bearing payload (assumption A5). */
export interface ChallanDisclosure {
  issuance_mode: "SIMULATED";
  badge: "SIMULATED";
  is_legal_instrument: false;
  authority_note: string;
  assumption_ref: string;
  disclosure: string;
}

// --- UC3-041 OCR engine health ---------------------------------------------
export interface OcrHealth {
  engine: string;
  configured: boolean;
  upstream: {
    url: string | null;
    reachable: boolean;
    status?: string;
    engine_ready?: boolean;
    tesseract_version?: string;
    error?: string;
  };
  active_rung: "OCR_SERVICE" | "OCR" | "MOCK";
  eir_doc_types: string[];
  will_produce: { source: string; real_read: boolean; expected_confidence: number };
  rungs: Array<{
    rung: number;
    source: string;
    engine: string;
    real_read: boolean;
    nominal_confidence: number;
    available: boolean;
    value_prefix?: string;
    note?: string;
  }>;
  failed_extraction_source: string;
}

// --- UC3-028 violation queue / UC3-029 hash-chained audit -------------------
export interface ViolationCaseRow {
  case_id: string;
  vehicle_number: string | null;
  driver_id: string | null;
  status: string;
  total_fine: number;
  first_detected_at: string;
  last_updated_at: string;
  gate_id: string | null;
  confidence: number | null;
  evidence_url: string | null;
  /** Evidence is written once and referenced by this SHA-256 (UI-113). */
  evidence_sha256: string | null;
  challan_no: string | null;
  challan_status: string | null;
  kinds: string[];
  severity: string;
}

export interface ViolationQueueResponse {
  cases: ViolationCaseRow[];
  count: number;
  by_status: Record<string, number>;
  lifecycle: string[];
  violation_types: string[];
  evidence_policy: { hash: string; note: string };
}

/** One append-only, hash-chained audit entry (hash = sha256(prev_hash + body)). */
export interface CaseAuditEntry {
  event: string;
  from_status: string | null;
  to_status: string | null;
  actor: string | null;
  ts: string;
  hash: string;
}

export interface ViolationCaseBundle {
  case: ViolationCaseRow;
  violations: Array<{
    id: string;
    kind: string;
    severity: string;
    ts: string;
    fine_inr: number;
    section: string | null;
  }>;
  challan: (Record<string, unknown> & ChallanDisclosure) | null;
  audit: CaseAuditEntry[];
}

export interface ChainVerification {
  case_id: string;
  valid: boolean;
  length: number;
  broken_at?: unknown;
}

// --- UC3-035 dual turnaround definitions -----------------------------------
/** One turnaround definition. Never rendered without its sibling (UI-122). */
export interface DualTatArm {
  key: string;
  label: string;
  unit: string;
  definition: string;
  method: string;
  target: number | null;
  baseline: number | null;
  baseline_source: string;
}

export interface DualTatResponse {
  /** Both arms in one payload — there is no way to request a single definition. */
  pair: { terminal: DualTatArm; driver: DualTatArm };
  render_rule: { ref: string; must_render_together: boolean; note: string };
  ground_truth_markers: Array<{
    source_document: string;
    vehicle_no: string | null;
    container_no: string | null;
    terminal_code: string | null;
    tat_minutes: number;
    definition: string;
    provenance: string;
    note: string;
  }>;
  ground_truth_note: string;
}

// --- UC3-035 KPI distribution (daily average, P90, peak-hour ratio) --------
export interface KpiDistributionEntry {
  key: string;
  label: string;
  unit: string;
  target: number | null;
  baseline: number | null;
  window_hours: number;
  daily_average: number | null;
  median: number | null;
  p90: number | null;
  peak_hour_ratio: number | null;
  peak_hour_utc: string | null;
  peak_hour_mean: number | null;
  samples: number;
  source: "live" | "baseline";
  /** Set when the mean exceeds P90 — outliers dominate; median is representative. */
  skew_warning: string | null;
  method: Record<string, string>;
}

export interface KpiDistributionResponse {
  distribution: Record<string, KpiDistributionEntry>;
  window_hours: number;
  note: string;
}

// --- UC3-028 escalation ladder + per-channel delivery ----------------------
export interface EscalationRung {
  escalation_id: number;
  rung: number;
  rung_label: string;
  n_minutes: number;
  due_after_min: number;
  zone_id: string | null;
  fired_at: string;
}

/** One channel to one recipient. UNAVAILABLE is a real outcome, not an error. */
export interface NotificationDelivery {
  delivery_id: number;
  escalation_id: number | null;
  rung: number | null;
  channel: "SMS" | "EMAIL" | "WHATSAPP";
  recipient_role: "OWNER" | "TRANSPORTER" | "TRAFFIC_POLICE" | "DRIVER";
  recipient: string | null;
  recipient_name: string | null;
  recipient_source: string | null;
  status: "QUEUED" | "SENT" | "DELIVERED" | "FAILED" | "UNAVAILABLE";
  provider: string | null;
  detail: string | null;
  created_at: string;
}

export interface FieldVerificationTask {
  task_id: number;
  case_id: string;
  reason: string;
  assigned_to: string;
  evidence_url: string | null;
  evidence_sha256: string | null;
  zone_id: string | null;
  status: string;
  resolved_plate: string | null;
  created_at: string;
  resolved_at: string | null;
}

export interface CaseNotifications {
  case_id: string;
  escalations: EscalationRung[];
  deliveries: NotificationDelivery[];
  by_status: Record<string, number>;
  channels: string[];
  ladder: Array<{ rung: number; label: string; multiplier: string }>;
  field_verification_task: FieldVerificationTask | null;
}

export interface EscalateResult {
  case_id: string;
  n_minutes: number;
  dwell_minutes: number;
  schedule: Array<{ rung: number; label: string; due_after_min: number }>;
  rungs_due: number[];
  rungs_fired: number[];
  rungs_already_fired: number[];
  deliveries: NotificationDelivery[];
  /** Measured server-side against the F-08 10-second budget. */
  elapsed_ms: number;
  latency_budget_ms: number;
  within_budget: boolean;
}

// --- UC3-023 camera degraded mode (EC-6) -----------------------------------
export interface CameraDegradedMode {
  camera_id: string;
  decision_path: string;
  frame_age_s: number | null;
  fault_injected: boolean;
  rung: "LIVE" | "DEGRADED" | "NO_FEED";
  no_feed: boolean;
  confirmation_mode: "ANPR_RFID_JOIN" | "RFID_ONLY";
  confidence: number;
  confidence_basis: string;
  manual_verify_lane: boolean;
  service_rate_factor: number;
  source_card: "LIVE" | "DEGRADED" | "DOWN";
  /** Replayed frames are labelled REPLAY — never LIVE. */
  feed_label: "LIVE" | "REPLAY" | "NO FEED";
}

export interface DegradedModeResponse {
  cameras: CameraDegradedMode[];
  count: number;
  degraded_count: number;
  no_feed_count: number;
  overall_rung: "LIVE" | "DEGRADED" | "NO_FEED";
  service_rate_factor: number;
  nominal_service_vph: number;
  effective_service_vph: number;
  timing_contract: {
    no_feed_detect_seconds: number;
    card_down_visible_seconds: number;
    note: string;
  };
  fault_injection: { endpoint: string; note: string };
  reconciliation: { note: string };
}

// --- UC3-020 corridor congestion heatmap -----------------------------------
export interface HeatmapSegment {
  segment_code: string;
  lat: number;
  lon: number;
  start: [number, number];
  end: [number, number];
  length_km: number;
  flow_vph: number | null;
  speed_kph: number | null;
  congestion_index: number | null;
  band: "FREE" | "BUSY" | "HEAVY" | "SEVERE" | null;
  jam_probability: number | null;
  reroute_recommended: boolean;
  reroute_reason?: string | null;
  observation: "COUNTED" | "EXTRAPOLATED" | "NO_DATA";
  data_mode: "OBSERVED" | "DERIVED";
}

export interface CorridorHeatmapResponse {
  at: string;
  now: string;
  offset_minutes: number;
  offset_requested: number;
  clamped: boolean;
  /** OBSERVED at or before now; DERIVED strictly after — the flip is exact. */
  data_mode: "OBSERVED" | "DERIVED";
  confidence: number;
  segments: HeatmapSegment[];
  segment_count: number;
  measured_count: number;
  window: {
    past_hours: number;
    forecast_hours: number;
    bucket_minutes: number;
    min_offset_minutes: number;
    max_offset_minutes: number;
  };
  bands: { free: number; busy: number; heavy: number };
  reroute: {
    threshold: number;
    triggered: boolean;
    segments: string[];
    action: string | null;
  };
  method: Record<string, string | number>;
  provenance: { mode: string; note: string; resolution_disclaimer: string };
}


// --- S-06 Evidence & Audit Explorer (GET /api/thread/*) ---
export interface ThreadHop {
  hop: string;
  label: string;
  stage: string;
  verdict: string;
  source_table: string;
  source_files: string;
  row_count: number;
  provenance: string[];
  synthetic: boolean;
  rows: Array<Record<string, unknown>>;
  vehicles: string[];
  note: string | null;
}

/** A truck reached from a container, with WHY we believe the attribution.
 *
 *  `provenance` qualifies the transporter/driver bridge, NOT the plate: the plate
 *  itself comes from a gate document or a CODECO message and is always real. So
 *  `SYNTHETIC` here means "this plate resolves to that transporter only by the
 *  stated assumption" — `11-Transport Data` carries no vehicle-registration
 *  column, so no plate can be resolved from JNPA's own masters (defect B1). */
export interface ThreadVehicle {
  plate: string;
  provenance: string | null;
  assumption_ref: string | null;
  source_ref: string | null;
  transporter: string | null;
  driver_name: string | null;
  driver_licence: string | null;
}

export interface ThreadResponse {
  subject: { type?: string; container_no?: string };
  summary: {
    hops_total: number;
    hops_found: number;
    hops_not_in_corpus: number;
    hops_errored: number;
    reaches_a_vehicle: boolean;
    vehicle_count: number;
    stages_found: string[];
    has_synthetic_hops: boolean;
    synthetic_hops: string[];
  };
  hops: ThreadHop[];
  vehicles: ThreadVehicle[];
  queries: Array<{ hop: string; sql: string; params: Record<string, unknown>; row_count: number; error?: string | null }>;
}


// --- S-08 Ad-hoc Query (GET /api/query/*) ---
/** One queryable view of the canonical model, as the server declares it. */
export interface QueryDataset {
  key: string;
  label: string;
  table: string;
  columns: string[];
  /** Only these columns may be filtered on — the API rejects anything else. */
  filters: string[];
  date_column: string | null;
  note: string;
}

export interface QueryResult {
  dataset: string;
  label?: string;
  table: string;
  rows: Array<Record<string, unknown>>;
  count: number;
  truncated?: boolean;
  window_applied?: boolean;
  note?: string;
  error?: string;
  /** Composed by the SERVER, returned so the working stays traceable. */
  sql: string;
  params: Record<string, unknown>;
}


// --- T-09 Facilities & Utilities Directory (GET /api/facilities) ---
/** One place the corpus names, with the table and file family that name it.
 *
 *  Composed at read time from five sources — there is no facilities master
 *  table — so `source_table` is not decoration: a CFS named in a monthly dwell
 *  report and a terminal from the reference model are evidence of different
 *  strength, and the reader must be able to tell them apart. */
export interface FacilityRow {
  facility_id: string;
  type: string;
  name: string;
  operator: string | null;
  site_code: string | null;
  lat: number | null;
  lon: number | null;
  capacity: number | null;
  berth_count: number | null;
  dwell_hours: string | number | null;
  source_table: string;
  source_files: string;
}

/** A facility class the corpus does not name at all. Carried in the response so
 *  a driver-side locator can say "not supplied" instead of drawing an empty map
 *  that reads as a failed load. */
export interface AbsentFacility {
  type: string;
  why: string;
  would_need: string;
}

export interface FacilitiesResponse {
  facilities: FacilityRow[];
  count: number;
  by_type: Record<string, number>;
  absent: AbsentFacility[];
  sources_unavailable: string[];
  note: string;
}


// --- D-13 Fleet View (GET /api/fleet) ---
/** One vehicle linked to a transporter.
 *
 *  `provenance` describes the LINK, not the truck: DOCUMENT_EVIDENCED means a
 *  gate document names both the plate and the company; anything else means the
 *  link was generated under `assumption_ref`, because `11-Transport Data`
 *  carries no vehicle-registration column to resolve one (defect B1). */
export interface TransporterFleetVehicle {
  vehicle_no: string;
  vehicle_no_norm: string;
  provenance: string | null;
  assumption_ref: string | null;
  source_ref: string | null;
  created_at: string | null;
  company_name: string | null;
  company_blacklist_entries: number;
  jobs: number;
  last_gate_document_ts: string | null;
}

export interface FleetResponse {
  transporter_id: number | null;
  company: string | null;
  vehicles: TransporterFleetVehicle[];
  count: number;
  by_provenance: Record<string, number>;
  /** Set when the caller may not see a fleet. A refusal is an answer — the UI
   *  renders it rather than showing an empty table. */
  reason?: string | null;
  note?: string;
  error?: string;
}

// --- UC-3 peak yard utilisation + truck arrival management ------------------
// Mirrors gateway/routers/yard.py + services/yard_capacity. `capacity_declared`
// is deliberately part of the contract: the console must be able to mark a
// declared denominator as declared rather than presenting it as measured.
export interface YardCapacity {
  yard_id: string;
  terminal_code: string;
  name: string;
  capacity_slots: number;
  occupied_slots: number;
  available_slots: number;
  /** Slots the yard plans up to (capacity x critical threshold). */
  operating_ceiling_slots: number;
  /** Bookable slots below that ceiling — what admission decisions use. */
  headroom_slots: number;
  utilization_pct: number;
  capacity_status: "NORMAL" | "ELEVATED" | "HIGH" | "CRITICAL";
  constrained: boolean;
  admissible_trucks: number;
  capacity_source: string;
  capacity_declared: boolean;
  occupancy_source: string | null;
  source_note: string | null;
  thresholds: {
    high_utilization_pct: number;
    critical_utilization_pct: number;
    slots_per_truck: number;
    release_rate_slots_per_hour: number | null;
    preferred_parking_facility_id: string | null;
  };
  updated_at: string | null;
}

export interface YardCapacityEvent {
  id?: number;
  yard_id?: string;
  event_type: "SEED" | "INCREASE" | "RELEASE" | "SET" | "SYNC";
  delta_slots: number;
  occupied_before: number;
  occupied_after: number;
  capacity_slots?: number;
  utilization_pct: number;
  status: string;
  reason: string | null;
  actor: string | null;
  created_at?: string | null;
}

export interface YardCapacityBoard {
  yard: YardCapacity | null;
  yards: YardCapacity[];
  recent_events: YardCapacityEvent[];
  active_holds: number;
  degraded?: boolean;
  detail?: string;
  ts?: string;
}

export interface TruckArrivalHold {
  id: number;
  device_id: string;
  plate: string | null;
  driver_id: string | null;
  driver_name: string | null;
  /** "truck-sim" | "pwa-registered" — the same provenance the fleet list stamps. */
  source: string;
  gate_id: string | null;
  eta_s: number | null;
  yard_id: string;
  yard_utilization_pct: number | null;
  status: "HOLD_AT_PARKING" | "RELEASED" | "CANCELLED";
  reason: string;
  recommended_facility_id: string | null;
  recommended_facility_name: string | null;
  facility_available: number | null;
  facility_lat: number | null;
  facility_lon: number | null;
  estimated_wait_min: number | null;
  alert_id: string | null;
  notified: boolean;
  release_notified: boolean;
  held_at: string | null;
  released_at: string | null;
}

export interface YardParkingRecommendation {
  recommended: boolean;
  facility_id: string | null;
  name: string | null;
  lat?: number | null;
  lon?: number | null;
  capacity?: number | null;
  available?: number | null;
  status?: string | null;
  is_preferred?: boolean;
  reason: string;
  estimated_wait_min: number | null;
  facilities_considered: number;
  preferred_facility_id: string | null;
  source?: string;
}

export interface YardEvaluation {
  yard: YardCapacity;
  arrivals: {
    total: number;
    simulator: number;
    enrolled_pwa: number;
    already_held: number;
    queue_source?: string | null;
    queue_degraded?: boolean;
  };
  congestion_pressure: number;
  constrained: boolean;
  reason: string | null;
  alerts: Array<Record<string, unknown>>;
  held: TruckArrivalHold[];
  proceeding: string[];
  parking: YardParkingRecommendation | null;
  dry_run: boolean;
  would_hold?: string[];
  detail?: string;
  ts?: string;
}

export interface YardRelease {
  yard: YardCapacity;
  released: TruckArrivalHold[];
  released_count: number;
  still_held: number;
  reason: string;
  ts?: string;
}

export interface YardArrivalBoard {
  yard: YardCapacity | null;
  holds: TruckArrivalHold[];
  active_count: number;
  released_recent: TruckArrivalHold[];
  by_source?: Record<string, number>;
  degraded?: boolean;
  ts?: string;
}
