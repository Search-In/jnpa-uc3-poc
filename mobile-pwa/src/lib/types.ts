// Wire types mirroring the gateway contracts the PWA consumes (gateway/routers/
// trucks.py, vahan.py, alerts.py, scenario_ext.py). Kept loose where the backend
// payloads are open-ended so the UI never fights the schema during a live demo.

export interface DevicePosition {
  lat: number;
  lon: number;
}

// GET /api/trucks/{id} envelope (PRIMARY/SECONDARY/TERTIARY chain).
export interface TruckEnvelope {
  device_id: string;
  decision_path: string; // PRIMARY | SECONDARY | TERTIARY
  gate_boom_delay_s: number;
  elevated_scrutiny: boolean;
  record: TruckRecord;
}

// GET /devices/{id} snapshot (the `record` field; truck-sim shape).
export interface TruckRecord {
  device_id: string;
  plate?: string | null;
  gate_id?: string | null;
  state: string;
  position?: DevicePosition;
  speed_kmh?: number;
  heading?: number;
  battery?: number;
  accuracy_m?: number;
  remaining_km?: number;
  eta_s?: number | null;
  segment_id?: string | null;
}

export interface Gate {
  id: string;
  name: string;
  lat: number;
  lon: number;
}

export interface CorridorGeometry {
  name: string;
  polyline: [number, number][]; // [lon, lat]
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

export interface TasSlot {
  slot_id: string;
  gate_id: string;
  start: string;
  status: "BOOKED" | "RESCHEDULED" | "CANCELLED" | string;
  rescheduled_to?: string | null;
}

export interface Advisory {
  id: string; // synthesised client-side (ts+device) for inbox keying
  type: "reroute" | "alert" | "challan" | string;
  device_id?: string;
  ts: string;
  title?: string;
  body?: string;
  reason?: string;
  gate_id?: string | null;
  dest?: DevicePosition | null;
  route_km?: number | null;
  severity?: string;
  requires_ack?: boolean;
  acked?: boolean;
  // raw alert/challan payload passthrough
  payload?: Record<string, any>;
  kind?: string;
  plate?: string | null;
}

// VahanRecord via gateway /api/vahan/rc/{plate}.
export interface VahanEnvelope {
  plate: string;
  decision_path: string; // LIVE_PRIMARY | LIVE_FALLBACK | CACHED | PROVISIONAL
  record: Record<string, any>;
  provisional?: boolean;
}

// /api/ws frame envelope.
export type WsFrame =
  | { type: "hello"; payload: { service: string; channels: string[] } }
  | { type: "reroute"; payload: RerouteAdvisory }
  | { type: "reroute_ack"; payload: { device_id: string; state: string; ts: string } }
  | { type: "alert"; payload: AlertFrame }
  | {
      type: "truck_position";
      payload: { device_id: string; lat: number; lon: number; speed_kmh?: number };
    }
  | { type: "traffic"; payload: TrafficSnapshot }
  | { type: string; payload: any };

export interface RerouteAdvisory {
  type: "reroute";
  device_id: string;
  ts: string;
  gate_id?: string | null;
  dest?: DevicePosition | null;
  route_km?: number | null;
  reason?: string;
  title?: string;
  body?: string;
  requires_ack?: boolean;
}

export interface AlertFrame {
  id?: string;
  ts?: string;
  kind?: string;
  severity?: string;
  gate_id?: string | null;
  plate?: string | null;
  payload?: Record<string, any>;
}
