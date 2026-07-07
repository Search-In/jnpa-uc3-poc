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
  CameraHealth,
  CarbonRollup,
  CorridorGeometry,
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
  IdentityEnrolResult,
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
export interface OcrEval {
  /** OCR accuracy in the CLEAR condition, 0..1 (e.g. 0.97). */
  clear_accuracy: number;
  /** Committed target (0..1), e.g. 0.95. */
  target?: number;
  /** True only when the real CRNN weights are loaded and the ≥95% gate passes. */
  target_met?: boolean;
  /** True when the deterministic fallback OCR is active (no CRNN weights). */
  degraded?: boolean;
}

export interface CongestionMetrics {
  /** Forecaster F1 score, 0..1 (e.g. 0.86). */
  f1: number;
  /** Committed target F1 (0..1), e.g. 0.85. */
  target?: number;
  /** True only when f1 >= target. */
  target_met?: boolean;
}

export type DataMode = "mock" | "live";

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
  identityEnrol(driverId: string, image: string): Promise<IdentityEnrolResult>;

  // --- Driver enrolment approval workflow (admin portal) ---
  enrollments(status?: string): Promise<DriverEnrollment[]>;
  enrollmentDetail(driverId: string): Promise<DriverEnrollment>;
  approveEnrollment(driverId: string): Promise<{ approved: boolean }>;
  rejectEnrollment(driverId: string, reason: string): Promise<{ rejected: boolean }>;
  reenrollEnrollment(driverId: string, reason?: string): Promise<{ reenroll: boolean }>;

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
}
