// The single typed data-access contract for the dashboard (UC1 parity).
//
// Every screen talks to a `DataAdapter`, never to `fetch`/the gateway directly.
// Two implementations sit behind this interface — `MockAdapter` (deterministic
// fixtures, zero credentials, instant demo) and `LiveAdapter` (calls the gateway
// /api surface) — selected at startup by `VITE_DATA_MODE=mock|live`. This keeps
// camera/Vahan/ULIP/AI APIs out of the UI entirely and lets `npm run dev` run
// the full dashboard with no backend.

import type {
  Alert,
  AutoLeoResult,
  AvailableVehicle,
  CameraHealth,
  CarbonRollup,
  CorridorGeometry,
  CreateDriverInput,
  CreateVehicleInput,
  FleetVehicle,
  UpdateVehicleInput,
  VehicleDetectionResult,
  VehicleIdentityResult,
  VehicleStats,
  Decision,
  DriverEnrollment,
  EmptyAllocation,
  FastagBalance,
  FastagHealth,
  FastagTransactions,
  FaultControlResult,
  FaultState,
  Gate,
  IdentityVerifyArg,
  IdentityVerifyResult,
  IdentityEnrollResult,
  KpiResult,
  ParkingFacility,
  ParkingSummary,
  PoliceIncident,
  Scenario,
  ScenarioStep,
  SourceHealth,
  TasSlot,
  TollEnroute,
  TollEnrouteInput,
  TrafficSnapshot,
  TruckDevice,
  ViolationCatalogItem,
  ViolationCommitInput,
  ViolationDetectResult,
  ViolationEnforceResult,
  ViolationIncident,
  Zone,
} from "@/lib/types";

// Realism probes for the Demo Console status panel. Both endpoints are optional
// on the gateway, so LiveAdapter degrades to `null` rather than throwing (the
// screen then shows a static "target/advisory" note). Mock returns plausible
// deterministic values.
export interface AnprConditionSlice {
  condition?: string;
  n?: number;
  exact_match?: number;
  char_accuracy?: number;
  detection_recall?: number;
}

export interface OcrEval {
  /** OCR accuracy in the CLEAR condition, 0..1 (e.g. 0.97). */
  clear_accuracy: number;
  /** Committed target (0..1), e.g. 0.95. */
  target?: number;
  /** True only when the real CRNN weights are loaded and the ≥95% gate passes. */
  target_met?: boolean;
  /** True when the deterministic fallback OCR is active (no CRNN weights). */
  degraded?: boolean;
  // --- evaluator-facing normalized fields (from GET /api/anpr/eval) ---
  model_name?: string;
  accuracy?: number;
  precision?: number;
  recall?: number;
  ocr_confidence?: number;
  dataset_breakdown?: AnprConditionSlice[];
  /** Whole-twin data posture: "mock" | "live". */
  data_mode?: DataMode;
  /** True when metrics come from the degraded fallback OCR, not the real model. */
  metrics_synthetic?: boolean;
}

export interface CongestionMetrics {
  /** Forecaster F1 score, 0..1 (e.g. 0.86). */
  f1: number;
  /** Committed target F1 (0..1), e.g. 0.85. */
  target?: number;
  /** True only when f1 >= target. */
  target_met?: boolean;
  // --- evaluator-facing normalized fields (from GET /api/traffic/metrics) ---
  model_name?: string;
  precision?: number;
  recall?: number;
  evaluation_dataset?: string;
  data_mode?: DataMode;
  metrics_synthetic?: boolean;
}

export type DataMode = "mock" | "live";

// Follow-the-Box cross-twin container journey (UC-II → UC-III).
export interface JourneyStage {
  twin: "UC-II" | "UC-III";
  stage: string;
  source: string; // "gate-data" | "live" | "derived"
  source_system?: string;
  event_id?: string;
  correlation_id?: string;
  container_no?: string;
  ts: string;
  data_mode?: DataMode;
  title: string;
  detail: string;
  facts?: Record<string, unknown>;
}

export interface CrossTwinEvent {
  topic: string;
  publishing_twin: string;
  receiving_twin: string;
  correlation_id: string;
  case_id?: string;
  event_id: string;
  event_time: string;
  container_no: string;
  status: string; // "Delivered"
  data_mode?: DataMode;
  simulated?: boolean;
}

export interface JourneyStatusStep {
  key: string;
  label: string;
  done: boolean;
}

export interface ContainerJourney {
  container_no: string;
  iso6346_valid: boolean;
  owner_code?: string | null;
  found: boolean;
  correlation_id?: string;
  case_id?: string;
  vehicle_no?: string;
  gate?: string;
  eta_min?: number;
  gate_record_source?: string;
  data_mode?: DataMode;
  cross_twin?: CrossTwinEvent;
  journey_status?: JourneyStatusStep[];
  stages: JourneyStage[];
  note?: string;
}

// Per-vehicle carbon-emission ledger row (jnpa.carbon_emission via
// GET /api/carbon/history). Persisted output of POST /api/carbon/calculate.
export interface CarbonEmissionRecord {
  id?: number;
  vehicle_id: string;
  vehicle_type?: string;
  distance_km?: number;
  fuel_consumed_litre?: number;
  idle_time_minutes?: number;
  co2_kg?: number;
  source?: string;
  calculation_method?: string;
  created_at?: string;
}

export interface DataAdapter {
  readonly mode: DataMode;

  // geometry
  gates(): Promise<Gate[]>;
  corridor(): Promise<CorridorGeometry>;

  // live state
  trafficSnapshots(): Promise<TrafficSnapshot[]>;
  trafficPredict(
    horizon?: number,
  ): Promise<{ decision_path: string; predictions: Record<string, number> }>;
  trucks(state?: string, limit?: number): Promise<TruckDevice[]>;
  reroute(
    deviceId: string,
    body: { gate_id?: string; lat?: number; lon?: number; force_state?: string },
  ): Promise<{ rerouted: boolean }>;

  // alerts
  alerts(params?: { since?: string; kind?: string; limit?: number }): Promise<Alert[]>;

  // kpi / health
  kpiStrip(): Promise<KpiResult[]>;
  sources(): Promise<SourceHealth[]>;
  cameras(): Promise<CameraHealth[]>;
  decisions(apiName?: string, limit?: number): Promise<Decision[]>;

  // zones
  zones(): Promise<Zone[]>;
  putZones(zones: Zone[]): Promise<{ saved: boolean; count: number }>;

  // police reports
  policeReport(params?: Record<string, string | undefined>): Promise<PoliceIncident[]>;
  // vehicle violation detection (Reports-page enforcement console)
  violationCatalog(): Promise<ViolationCatalogItem[]>;
  violationDetect(image: Blob, gateId?: string): Promise<ViolationDetectResult>;
  violationCommit(input: ViolationCommitInput): Promise<ViolationIncident>;
  // Fully-automatic pipeline (upload → ANPR → case → challan → notification).
  violationEnforce(
    image: Blob,
    opts?: { gateId?: string; zoneId?: string; violations?: string },
  ): Promise<ViolationEnforceResult>;
  policePdfUrl(params?: Record<string, string | undefined>): string;
  // Download the report PDF (auth-aware — see LiveAdapter). Async because it
  // streams the file with the bearer token rather than navigating to a URL.
  downloadPolicePdf(params?: Record<string, string | undefined>): Promise<void>;

  // scenarios
  scenarios(): Promise<Scenario[]>;
  runScenario(
    name: string,
    params: Record<string, any>,
  ): Promise<{ handle_id: string; name: string; status: string; trace_id?: string }>;
  resetScenario(name: string, handleId?: string): Promise<{ ok: boolean }>;
  scenarioTimeline(handleId: string): Promise<{ handle_id: string; steps: ScenarioStep[] }>;

  // --- Appendix-C capabilities ---
  emptyAllocations(): Promise<EmptyAllocation[]>;
  emptyTrtKpi(): Promise<KpiResult>;
  carbonRollup(): Promise<CarbonRollup>;
  // Persisted per-vehicle emission ledger (all recent, or one vehicle's history).
  carbonHistory(vehicleId?: string, limit?: number): Promise<CarbonEmissionRecord[]>;
  leoQueue(): Promise<AutoLeoResult[]>;
  customsFlags(): Promise<Alert[]>;
  identityGallery(): Promise<
    { driver_id: string; name: string; license_no: string; photo_url?: string | null }[]
  >;
  // `arg` accepts the legacy simulate string OR a { simulate?, image? } payload
  // (image = captured frame as base64/data-URL) so the camera flow and the old
  // tests share one method.
  identityVerify(
    driverId: string,
    arg?: "genuine" | "impostor" | "unknown" | IdentityVerifyArg,
  ): Promise<IdentityVerifyResult>;
  identityEnroll(driverId: string, image: string): Promise<IdentityEnrollResult>;

  // --- Driver enrollment approval workflow (admin portal) ---
  enrollments(status?: string): Promise<DriverEnrollment[]>;
  enrollmentDetail(driverId: string): Promise<DriverEnrollment>;
  approveEnrollment(driverId: string): Promise<{ approved: boolean }>;
  rejectEnrollment(driverId: string, reason: string): Promise<{ rejected: boolean }>;
  reenrollEnrollment(driverId: string, reason?: string): Promise<{ reenroll: boolean }>;
  // Admin creates a driver profile + vehicle assignment (source=ADMIN, PENDING).
  createDriverProfile(
    input: CreateDriverInput,
  ): Promise<{ created: boolean; driver_id: string; status: string }>;
  // Fleet vehicles not yet assigned to an active driver (assign-vehicle dropdown).
  availableVehicles(q?: string, limit?: number): Promise<AvailableVehicle[]>;

  // --- Vehicle Master (fleet registry, admin portal) ---
  vehicles(q?: string, status?: string): Promise<FleetVehicle[]>;
  vehicleStats(): Promise<VehicleStats>;
  createVehicle(input: CreateVehicleInput): Promise<{ created: boolean; vehicle: FleetVehicle }>;
  updateVehicle(
    vehicleId: string,
    input: UpdateVehicleInput,
  ): Promise<{ updated: boolean; vehicle: FleetVehicle }>;

  // --- Vehicle Intelligence Identity & Detection (camera workflows) ---
  vehicleIdentity(vehicleNumber: string, image: string): Promise<VehicleIdentityResult>;
  vehicleDetection(image: string, expected?: string): Promise<VehicleDetectionResult>;

  parkingAvailability(minuteOfDay?: number): Promise<ParkingFacility[]>;
  parkingSummary(minuteOfDay?: number): Promise<ParkingSummary>;

  // --- Terminal Appointment System (TAS) ---
  tasSlots(gateId?: string): Promise<TasSlot[]>;

  // --- FASTag (ULIP) — /api/fastag/* ---
  fastagBalance(rcNumber: string): Promise<FastagBalance>;
  fastagTransactions(rcNumber: string): Promise<FastagTransactions>;
  tollEnroute(body: TollEnrouteInput): Promise<TollEnroute>;
  fastagHealth(): Promise<FastagHealth>;

  // --- Fault-injection control surface (Demo Console) ---
  getFaults(): Promise<FaultState>;
  forceFault(domain: string, rung: string): Promise<FaultControlResult>;
  clearFault(domain?: string): Promise<FaultControlResult>; // no domain => clear all

  // --- Realism probes (graceful: null when the gateway lacks the endpoint) ---
  ocrEval(): Promise<OcrEval | null>;
  congestionMetrics(): Promise<CongestionMetrics | null>;

  // --- Follow-the-Box cross-twin container journey ---
  containerJourney(containerNo: string): Promise<ContainerJourney | null>;
}
