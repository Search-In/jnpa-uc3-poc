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
  EmptyAllocation,
  FaultControlResult,
  FaultState,
  Gate,
  IdentityVerifyResult,
  KpiResult,
  ParkingFacility,
  ParkingSummary,
  PoliceIncident,
  Scenario,
  ScenarioStep,
  SourceHealth,
  TrafficSnapshot,
  TruckDevice,
  Zone,
} from "@/lib/types";

// Realism probes for the Demo Console status panel. Both endpoints are optional
// on the gateway, so LiveAdapter degrades to `null` rather than throwing (the
// screen then shows a static "target/advisory" note). Mock returns plausible
// deterministic values.
export interface OcrEval {
  /** OCR accuracy in the CLEAR condition, 0..1 (e.g. 0.97). */
  clear_accuracy: number;
}

export interface CongestionMetrics {
  /** Forecaster F1 score, 0..1 (e.g. 0.86). */
  f1: number;
}

export type DataMode = "mock" | "live";

export interface DataAdapter {
  readonly mode: DataMode;

  // geometry
  gates(): Promise<Gate[]>;
  corridor(): Promise<CorridorGeometry>;

  // live state
  trafficSnapshots(): Promise<TrafficSnapshot[]>;
  trafficPredict(horizon?: number): Promise<{ decision_path: string; predictions: Record<string, number> }>;
  trucks(state?: string, limit?: number): Promise<TruckDevice[]>;
  reroute(deviceId: string, body: { gate_id?: string; lat?: number; lon?: number; force_state?: string }): Promise<{ rerouted: boolean }>;

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
  policePdfUrl(params?: Record<string, string | undefined>): string;

  // scenarios
  scenarios(): Promise<Scenario[]>;
  runScenario(name: string, params: Record<string, any>): Promise<{ handle_id: string; name: string; status: string; trace_id?: string }>;
  resetScenario(name: string, handleId?: string): Promise<{ ok: boolean }>;
  scenarioTimeline(handleId: string): Promise<{ handle_id: string; steps: ScenarioStep[] }>;

  // --- Appendix-C capabilities ---
  emptyAllocations(): Promise<EmptyAllocation[]>;
  emptyTrtKpi(): Promise<KpiResult>;
  carbonRollup(): Promise<CarbonRollup>;
  leoQueue(): Promise<AutoLeoResult[]>;
  customsFlags(): Promise<Alert[]>;
  identityGallery(): Promise<{ driver_id: string; name: string; license_no: string }[]>;
  identityVerify(driverId: string, simulate: "genuine" | "impostor" | "unknown"): Promise<IdentityVerifyResult>;
  parkingAvailability(minuteOfDay?: number): Promise<ParkingFacility[]>;
  parkingSummary(minuteOfDay?: number): Promise<ParkingSummary>;

  // --- Fault-injection control surface (Demo Console) ---
  getFaults(): Promise<FaultState>;
  forceFault(domain: string, rung: string): Promise<FaultControlResult>;
  clearFault(domain?: string): Promise<FaultControlResult>; // no domain => clear all

  // --- Realism probes (graceful: null when the gateway lacks the endpoint) ---
  ocrEval(): Promise<OcrEval | null>;
  congestionMetrics(): Promise<CongestionMetrics | null>;
}
