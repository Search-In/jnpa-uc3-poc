// Thin fetch wrapper around the gateway's /api surface. The app always calls
// relative paths; the Vite dev proxy (dev) or nginx (prod) forwards to the
// gateway. Every helper returns parsed JSON and throws on non-2xx so TanStack
// Query surfaces the error state.

import { getToken } from "./auth";

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  // Attach the bearer token when a session exists (auth-enabled builds). When
  // auth is disabled there is no token and the header is simply omitted.
  const token = getToken();
  const authHeader: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
  const res = await fetch(path, {
    headers: { "content-type": "application/json", ...authHeader, ...(init?.headers || {}) },
    ...init,
  });
  if (!res.ok) {
    let detail: any = undefined;
    try {
      detail = await res.json();
    } catch {
      /* non-json error body */
    }
    throw new Error(
      `${res.status} ${res.statusText}${detail ? ` — ${JSON.stringify(detail)}` : ""}`,
    );
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// Authenticated file download. A plain <a href>/new-tab navigation can NOT carry
// the bearer token, so it 401s ("missing bearer token") on auth-enabled builds.
// Fetch the file with the token attached, then save the response blob via a
// temporary object URL.
async function downloadFile(path: string, filename: string): Promise<void> {
  const token = getToken();
  const authHeader: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
  const res = await fetch(path, { headers: { ...authHeader } });
  if (!res.ok) {
    let detail: any = undefined;
    try {
      detail = await res.json();
    } catch {
      /* non-json error body */
    }
    throw new Error(
      `${res.status} ${res.statusText}${detail ? ` — ${JSON.stringify(detail)}` : ""}`,
    );
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export const api = {
  // --- geometry ---
  gates: () => http<{ gates: import("./types").Gate[] }>("/api/gates"),
  corridor: () => http<import("./types").CorridorGeometry>("/api/corridor"),

  // --- live state ---
  trafficSnapshots: () =>
    http<{ snapshots: import("./types").TrafficSnapshot[] }>("/api/traffic/snapshots"),
  trafficPredict: (horizon = 15) =>
    http<{ decision_path: string; predictions: Record<string, number> }>(
      `/api/traffic/predict?horizon_min=${horizon}`,
    ),
  trucks: (state?: string, limit = 300) =>
    http<{ devices: import("./types").TruckDevice[]; count: number }>(
      `/api/trucks?limit=${limit}${state ? `&state=${state}` : ""}`,
    ),
  reroute: (
    deviceId: string,
    body: { gate_id?: string; lat?: number; lon?: number; force_state?: string },
  ) =>
    http<{ rerouted: boolean; dest: { lat: number; lon: number }; route_km: number }>(
      `/api/trucks/${encodeURIComponent(deviceId)}/route`,
      { method: "POST", body: JSON.stringify(body) },
    ),

  // --- alerts ---
  alerts: (params?: { since?: string; kind?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.since) q.set("since", params.since);
    if (params?.kind) q.set("kind", params.kind);
    if (params?.limit) q.set("limit", String(params.limit));
    return http<{ source: string; alerts: import("./types").Alert[] }>(
      `/api/alerts${q.toString() ? `?${q}` : ""}`,
    );
  },

  // --- kpi / health ---
  kpi: () => http<{ views: Record<string, any[]> }>("/api/kpi"),
  sources: () => http<{ sources: import("./types").SourceHealth[] }>("/api/kpi/sources"),
  cameras: () => http<{ cameras: import("./types").CameraHealth[] }>("/api/kpi/cameras"),
  decisions: (apiName?: string, limit = 200) =>
    http<import("./types").Decision[]>(
      `/api/debug/decisions?limit=${limit}${apiName ? `&api=${apiName}` : ""}`,
    ),

  // --- zones (geo-fencing manager) ---
  zones: () => http<{ source: string; zones: import("./types").Zone[] }>("/api/zones"),
  putZones: (zones: import("./types").Zone[]) =>
    http<{ saved: boolean; count: number }>("/api/zones", {
      method: "PUT",
      body: JSON.stringify({ zones }),
    }),

  // --- police reports ---
  policeReport: (params?: Record<string, string | undefined>) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v && q.set(k, v));
    return http<{ incidents: import("./types").PoliceIncident[]; count: number }>(
      `/api/reports/police?format=json${q.toString() ? `&${q}` : ""}`,
    );
  },
  policePdfUrl: (params?: Record<string, string | undefined>) => {
    const q = new URLSearchParams({ format: "pdf" });
    Object.entries(params || {}).forEach(([k, v]) => v && q.set(k, v));
    return `/api/reports/police?${q.toString()}`;
  },
  // Download the report PDF with auth attached (the bare URL above can't be used
  // for a browser navigation under auth-enabled builds — it 401s).
  downloadPolicePdf: (params?: Record<string, string | undefined>) => {
    const q = new URLSearchParams({ format: "pdf" });
    Object.entries(params || {}).forEach(([k, v]) => v && q.set(k, v));
    return downloadFile(`/api/reports/police?${q.toString()}`, "police-report.pdf");
  },

  // --- scenarios (What-If Console) ---
  scenarios: () =>
    http<{ source: string; scenarios: import("./types").Scenario[] }>("/api/scenarios"),
  runScenario: (name: string, params: Record<string, any>) =>
    http<{ handle_id: string; name: string; status: string; trace_id?: string }>(
      `/api/scenarios/${name}/run`,
      { method: "POST", body: JSON.stringify(params) },
    ),
  resetScenario: (name: string, handleId?: string) =>
    http<{ ok: boolean; handle_id?: string }>(`/api/scenarios/${name}/reset`, {
      method: "POST",
      body: JSON.stringify(handleId ? { handle_id: handleId } : {}),
    }),
  scenarioTimeline: (handleId: string) =>
    http<{
      handle_id: string;
      name?: string;
      status?: string;
      trace_id?: string;
      steps: import("./types").ScenarioStep[];
    }>(`/api/scenarios/handle/${handleId}/timeline`),

  // --- Terminal Appointment System (TFC-1) ---
  tasSlots: (gateId?: string) =>
    http<{ slots: import("./types").TasSlot[] }>(
      `/api/tas/slots${gateId ? `?gate_id=${encodeURIComponent(gateId)}` : ""}`,
    ),

  health: () => http<{ status: string; ws_clients: number }>("/healthz"),
};
