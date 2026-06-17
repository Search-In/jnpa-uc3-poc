// LiveAdapter — talks to the gateway /api surface. The UI never imports this
// directly; it receives a DataAdapter from the selector in ./index.ts.

import { api } from "@/lib/api";
import type {
  Alert,
  AutoLeoResult,
  CarbonRollup,
  EmptyAllocation,
  IdentityVerifyResult,
  KpiResult,
  ParkingFacility,
  ParkingSummary,
} from "@/lib/types";
import type { DataAdapter, DataMode } from "./types";

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { "content-type": "application/json" } });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} (${path})`);
  return (await res.json()) as T;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} (${path})`);
  return (await res.json()) as T;
}

export class LiveAdapter implements DataAdapter {
  readonly mode: DataMode = "live";

  gates = async () => (await api.gates()).gates;
  corridor = () => api.corridor();
  trafficSnapshots = async () => (await api.trafficSnapshots()).snapshots;
  trafficPredict = (horizon = 15) => api.trafficPredict(horizon);
  trucks = async (state?: string, limit = 300) => (await api.trucks(state, limit)).devices;
  reroute = async (deviceId: string, body: any) => {
    const r = await api.reroute(deviceId, body);
    return { rerouted: r.rerouted };
  };
  alerts = async (params?: any) => (await api.alerts(params)).alerts;

  // The KPI strip comes from the gateway /api/kpi/strip materialiser if present,
  // else we surface whatever the views give. The gateway returns the engine
  // shape (KpiResult[]); if the route is older, return [].
  kpiStrip = async (): Promise<KpiResult[]> => {
    try {
      const data = await getJson<{ strip?: KpiResult[] }>("/api/kpi/strip");
      return data.strip ?? [];
    } catch {
      return [];
    }
  };
  sources = async () => (await api.sources()).sources;
  cameras = async () => (await api.cameras()).cameras;
  decisions = (apiName?: string, limit = 200) => api.decisions(apiName, limit);

  zones = async () => (await api.zones()).zones;
  putZones = (zones: any) => api.putZones(zones);

  policeReport = async (params?: any) => (await api.policeReport(params)).incidents;
  policePdfUrl = (params?: any) => api.policePdfUrl(params);

  scenarios = async () => (await api.scenarios()).scenarios;
  runScenario = (name: string, params: any) => api.runScenario(name, params);
  resetScenario = async (name: string, handleId?: string) => {
    const r = await api.resetScenario(name, handleId);
    return { ok: r.ok };
  };
  scenarioTimeline = (handleId: string) => api.scenarioTimeline(handleId);

  // --- Appendix-C capabilities ---
  emptyAllocations = async (): Promise<EmptyAllocation[]> =>
    (await getJson<{ allocations: EmptyAllocation[] }>("/api/empty/allocations")).allocations;
  emptyTrtKpi = async (): Promise<KpiResult> =>
    (await getJson<{ kpi: KpiResult }>("/api/empty/kpi")).kpi;
  carbonRollup = (): Promise<CarbonRollup> => getJson<CarbonRollup>("/api/carbon/rollup");
  leoQueue = async (): Promise<AutoLeoResult[]> =>
    (await getJson<{ results: AutoLeoResult[] }>("/api/gate-data/leo/queue")).results;
  customsFlags = async (): Promise<Alert[]> =>
    (await getJson<{ alerts: Alert[] }>("/api/gate-data/customs/flags")).alerts;
  identityGallery = async () =>
    (await getJson<{ drivers: any[] }>("/api/identity/gallery")).drivers;
  identityVerify = (driverId: string, simulate: any): Promise<IdentityVerifyResult> =>
    postJson<IdentityVerifyResult>("/api/identity/verify", { driver_id: driverId, simulate });
  parkingAvailability = async (minuteOfDay?: number): Promise<ParkingFacility[]> =>
    (await getJson<{ facilities: ParkingFacility[] }>(
      `/api/parking/availability${minuteOfDay != null ? `?minute_of_day=${minuteOfDay}` : ""}`
    )).facilities;
  parkingSummary = (minuteOfDay?: number): Promise<ParkingSummary> =>
    getJson<ParkingSummary>(`/api/parking/summary${minuteOfDay != null ? `?minute_of_day=${minuteOfDay}` : ""}`);
}
