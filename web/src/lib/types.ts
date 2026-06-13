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

// WebSocket frame shapes (gateway/routers/ws.py + scenario_ext.py).
export type WsFrame =
  | { type: "hello"; payload: { service: string; channels: string[] } }
  | { type: "alert"; payload: Alert }
  | { type: "traffic"; payload: TrafficSnapshot }
  | { type: "truck_position"; payload: { device_id: string; plate?: string; lat: number; lon: number; speed_kmh?: number } }
  | { type: "decision"; payload: Decision }
  | { type: "scenario_step"; payload: ScenarioStep };
