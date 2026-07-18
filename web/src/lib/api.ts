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

// Multipart POST (file upload). Unlike http<>, we must NOT set content-type so
// the browser adds the multipart boundary; the bearer token is still attached.
async function postForm<T>(path: string, form: FormData): Promise<T> {
  const token = getToken();
  const authHeader: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
  const res = await fetch(path, { method: "POST", headers: { ...authHeader }, body: form });
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
  return (await res.json()) as T;
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
    // Name the file by what it contains: a single incident when an id is given,
    // otherwise the filtered batch. Keeps "this report" vs "all reports" distinct.
    const filename = params?.id ? `police-report-${params.id}.pdf` : "police-report.pdf";
    return downloadFile(`/api/reports/police?${q.toString()}`, filename);
  },

  // --- vehicle violation detection (Reports page enforcement console) ---
  violationCatalog: () =>
    http<{ violations: import("./types").ViolationCatalogItem[] }>("/api/violations/catalog"),
  violationDetect: (image: Blob, gateId?: string) => {
    const fd = new FormData();
    fd.append("image", image, "frame.jpg");
    if (gateId) fd.append("gate_id", gateId);
    return postForm<import("./types").ViolationDetectResult>("/api/violations/detect", fd);
  },
  violationCommit: (input: import("./types").ViolationCommitInput) =>
    http<import("./types").ViolationIncident>("/api/violations/commit", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  // Fully-automatic pipeline: one upload → ANPR → case → challan → notification.
  violationEnforce: (
    image: Blob,
    opts?: { gateId?: string; zoneId?: string; violations?: string },
  ) => {
    const fd = new FormData();
    fd.append("image", image, "frame.jpg");
    if (opts?.gateId) fd.append("gate_id", opts.gateId);
    if (opts?.zoneId) fd.append("zone_id", opts.zoneId);
    if (opts?.violations) fd.append("violations", opts.violations);
    return postForm<import("./types").ViolationEnforceResult>("/api/violations/enforce", fd);
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
  // Recent scenario run handles (What-If demo timeline picker) — RDS-backed.
  scenarioHandles: (limit = 50) =>
    http<{
      count: number;
      handles: {
        handle_id: string;
        name: string;
        status: string;
        trace_id?: string | null;
        started_at?: string | null;
        ended_at?: string | null;
        step_count: number;
        is_demo: boolean;
      }[];
    }>(`/api/scenarios/handles?limit=${limit}`),
  scenarioTimeline: (handleId: string) =>
    http<{
      handle_id: string;
      name?: string;
      status?: string;
      trace_id?: string;
      steps: import("./types").ScenarioStep[];
    }>(`/api/scenarios/handle/${handleId}/timeline`),

  // --- FASTag (ULIP) — /api/fastag/* ---
  fastagBalance: (rcNumber: string) =>
    http<import("./types").FastagBalance>("/api/fastag/balance", {
      method: "POST",
      body: JSON.stringify({ rc_number: rcNumber }),
    }),
  fastagTransactions: (rcNumber: string) =>
    http<import("./types").FastagTransactions>("/api/fastag/transactions", {
      method: "POST",
      body: JSON.stringify({ rc_number: rcNumber }),
    }),
  // Stored transactions for an RC straight from jnpa.fastag_transactions (no
  // vendor call). Used as the display source and as a fallback when the live
  // ULIP fetch is unavailable, so the tab always shows persisted RDS history.
  fastagTransactionsHistory: (rcNumber: string, limit = 100) =>
    http<{
      source: string;
      rc_number: string;
      count: number;
      transactions: import("./types").FastagTransactionRow[];
    }>(`/api/fastag/transactions/history?rc_number=${encodeURIComponent(rcNumber)}&limit=${limit}`),
  tollEnroute: (body: import("./types").TollEnrouteInput) =>
    http<import("./types").TollEnroute>("/api/fastag/toll-enroute", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  fastagHealth: () => http<import("./types").FastagHealth>("/api/fastag/health"),

  // --- Terminal Appointment System (TFC-1) ---
  tasSlots: (gateId?: string) =>
    http<{ slots: import("./types").TasSlot[] }>(
      `/api/tas/slots${gateId ? `?gate_id=${encodeURIComponent(gateId)}` : ""}`,
    ),

  health: () => http<{ status: string; ws_clients: number }>("/healthz"),

  // --- Customs & Gate systems (e-Seal / Form-13 / Weighbridge / ICEGATE) ---
  // All RDS-backed (jnpa.gate_captures / leo_reconciliation / alerts).
  gateProviders: () =>
    http<{ sources: Record<string, { mode: string; requested: string; url_configured: boolean }> }>(
      "/api/gate-data/providers",
    ),
  gateCaptures: (type?: string, containerNo?: string, limit = 100) => {
    const q = new URLSearchParams();
    if (type) q.set("type", type);
    if (containerNo) q.set("container_no", containerNo);
    q.set("limit", String(limit));
    return http<{ count: number; captures: import("./types").GateCapture[] }>(
      `/api/gate-data/captures?${q.toString()}`,
    );
  },
  gateReconciliations: (ready?: boolean, limit = 100) => {
    const q = new URLSearchParams();
    if (ready !== undefined) q.set("ready", String(ready));
    q.set("limit", String(limit));
    return http<{ count: number; reconciliations: import("./types").LeoReconciliation[] }>(
      `/api/gate-data/reconciliations?${q.toString()}`,
    );
  },
  customsHistory: (limit = 200) =>
    http<{ count: number; alerts: import("./types").CustomsAlert[] }>(
      `/api/gate-data/customs/history?limit=${limit}`,
    ),

  // --- Parking Management (RDS-backed: parking_facilities/slots/transactions/events) ---
  parkingAvailability: () =>
    http<{ source: string; facilities: import("./types").ParkingFacilityRow[] }>(
      "/api/parking/availability",
    ),
  parkingSummary: () => http<import("./types").ParkingMgmtSummary>("/api/parking/summary"),
  parkingAllocate: (facilityId: string, vehicleId: string, driverId?: string) =>
    http<import("./types").ParkingAllocation>("/api/parking/allocate", {
      method: "POST",
      body: JSON.stringify({ facility_id: facilityId, vehicle_id: vehicleId, driver_id: driverId }),
    }),
  parkingRelease: (vehicleId: string) =>
    http<{ released: boolean; facility_id?: string; duration_s?: number }>("/api/parking/release", {
      method: "POST",
      body: JSON.stringify({ vehicle_id: vehicleId }),
    }),
  parkingHistory: (limit = 100) =>
    http<{ count: number; transactions: import("./types").ParkingTransaction[] }>(
      `/api/parking/history?limit=${limit}`,
    ),
  parkingViolations: (limit = 100) =>
    http<{ count: number; violations: import("./types").ParkingViolation[] }>(
      `/api/parking/violations?limit=${limit}`,
    ),

  // --- Empty Container Allocation (RDS-backed) ---
  containersAvailable: (containerType?: string, limit = 200) => {
    const q = new URLSearchParams();
    if (containerType) q.set("container_type", containerType);
    q.set("limit", String(limit));
    return http<{
      count: number;
      containers: import("./types").ContainerInventory[];
      by_type?: any[];
    }>(`/api/empty/containers/available?${q.toString()}`);
  },
  containersAllocate: (body: import("./types").ContainerAllocateInput) =>
    http<import("./types").ContainerAllocation>("/api/empty/containers/allocate", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  containersAllocationHistory: (limit = 100) =>
    http<{ count: number; allocations: import("./types").ContainerAllocation[] }>(
      `/api/empty/containers/allocation/history?limit=${limit}`,
    ),

  // --- Geo-fence enforcement (RDS-backed: geofence_events, DB-driven engine) ---
  geoZonesActive: () =>
    http<{
      count: number;
      source: string;
      zones: { id: string; name: string; kind: string; points: number }[];
    }>("/api/geo/zones-active"),
  geoVehiclesInZones: () =>
    http<{ count: number; vehicles: import("./types").GeoVehicleInZone[] }>(
      "/api/geo/vehicles-in-zones",
    ),
  geoEvents: (eventType?: string, limit = 200) => {
    const q = new URLSearchParams();
    if (eventType) q.set("event_type", eventType);
    q.set("limit", String(limit));
    return http<{ count: number; events: import("./types").GeofenceEvent[] }>(
      `/api/geo/events?${q.toString()}`,
    );
  },
  geoViolations: (limit = 200) =>
    http<{ count: number; violations: import("./types").GeofenceEvent[] }>(
      `/api/geo/violations?limit=${limit}`,
    ),
  aiEvents: (eventType?: string, limit = 200) => {
    const q = new URLSearchParams();
    if (eventType) q.set("event_type", eventType);
    q.set("limit", String(limit));
    return http<{ count: number; events: import("./types").AiEvent[] }>(
      `/api/ai/events?${q.toString()}`,
    );
  },

  // --- Vehicle & Driver Intelligence (Vahan/Sarathi, RDS-backed) ---
  vehicleIntel: (plate: string) =>
    http<import("./types").VehicleIntel>(`/api/vahan/vehicle-intel/${encodeURIComponent(plate)}`),
  driverIntel: (key: string) =>
    http<import("./types").DriverIntel>(`/api/vahan/driver-intel/${encodeURIComponent(key)}`),
  dlLookup: (dl: string) =>
    http<{ dl: string; decision_path?: string; status?: string; record?: Record<string, unknown> }>(
      `/api/vahan/dl/${encodeURIComponent(dl)}`,
    ),
  verificationHistory: (limit = 100) =>
    http<{ count: number; history: Record<string, unknown>[] }>(
      `/api/vahan/verification-history?limit=${limit}`,
    ),
  dlHistory: (limit = 100) =>
    http<{ count: number; history: Record<string, unknown>[] }>(
      `/api/vahan/dl-history?limit=${limit}`,
    ),

  // --- Workflow Composer (automation rule authoring + execution audit) ---
  wfCatalog: () =>
    http<{ fields: WfField[]; operators: string[]; actions: WfAction[] }>("/api/workflows/catalog"),
  wfRules: () => http<{ rules: WfRule[]; count: number }>("/api/workflows/rules"),
  wfCreateRule: (body: WfRuleInput) =>
    http<{ rule: WfRule }>("/api/workflows/rules", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  wfUpdateRule: (id: string, body: Partial<WfRuleInput>) =>
    http<{ rule: WfRule }>(`/api/workflows/rules/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  wfDeleteRule: (id: string) =>
    http<{ deleted: string }>(`/api/workflows/rules/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  wfEvaluate: (event: Record<string, unknown>) =>
    http<WfExecution>("/api/workflows/evaluate", {
      method: "POST",
      body: JSON.stringify({ event }),
    }),
  wfExecutions: (limit = 50) =>
    http<{ executions: WfExecution[]; count: number }>(`/api/workflows/executions?limit=${limit}`),

  // ===================================================================
  // UC-III Final-Completion feature APIs (additive; gateway routers 0024)
  // ===================================================================
  // --- Accidents (Feature 1) ---
  accidents: (params?: { status?: string; accident_type?: string; vehicle_id?: string; limit?: number }) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    return http<{ count: number; accidents: any[] }>(`/api/accidents${q.toString() ? `?${q}` : ""}`);
  },
  accidentDashboard: () => http<any>("/api/accidents/dashboard"),
  accident: (id: number) => http<{ accident: any; timeline: any[] }>(`/api/accidents/${id}`),
  accidentReport: (body: Record<string, any>) =>
    http<{ created: boolean; accident: any }>("/api/accidents", { method: "POST", body: JSON.stringify(body) }),
  accidentStatus: (id: number, body: Record<string, any>) =>
    http<any>(`/api/accidents/${id}/status`, { method: "POST", body: JSON.stringify(body) }),
  accidentInvestigation: (id: number, body: Record<string, any>) =>
    http<any>(`/api/accidents/${id}/investigation`, { method: "POST", body: JSON.stringify(body) }),
  accidentResolve: (id: number, body: Record<string, any>) =>
    http<any>(`/api/accidents/${id}/resolve`, { method: "POST", body: JSON.stringify(body) }),

  // --- Transporter blacklist (Feature 2) ---
  transporters: (params?: { q?: string; status?: string; limit?: number }) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    return http<{ count: number; transporters: any[] }>(`/api/transporters${q.toString() ? `?${q}` : ""}`);
  },
  transporterBlacklist: () => http<{ count: number; blacklist: any[] }>("/api/transporters/blacklist"),
  transporter: (id: number) =>
    http<{ transporter: any; vehicles: any[]; blacklist_history: any[] }>(`/api/transporters/${id}`),
  transporterCreate: (body: Record<string, any>) =>
    http<any>("/api/transporters", { method: "POST", body: JSON.stringify(body) }),
  transporterAddVehicle: (id: number, body: Record<string, any>) =>
    http<any>(`/api/transporters/${id}/vehicles`, { method: "POST", body: JSON.stringify(body) }),
  transporterBlacklistAdd: (id: number, body: Record<string, any>) =>
    http<any>(`/api/transporters/${id}/blacklist`, { method: "POST", body: JSON.stringify(body) }),
  transporterLift: (id: number, body?: Record<string, any>) =>
    http<any>(`/api/transporters/${id}/lift`, { method: "POST", body: JSON.stringify(body || {}) }),
  validateVehicle: (plate: string) =>
    http<any>(`/api/transporters/validate/vehicle/${encodeURIComponent(plate)}`),
  validateDriver: (driverId: string) =>
    http<any>(`/api/transporters/validate/driver/${encodeURIComponent(driverId)}`),

  // --- Camera AI (Features 3/4/5) ---
  cameraCounts: (params?: { camera_id?: string; gate_id?: string; limit?: number }) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    return http<{ count: number; counts: any[] }>(`/api/camera-ai/counts${q.toString() ? `?${q}` : ""}`);
  },
  cameraSummary: () => http<any>("/api/camera-ai/summary"),
  cameraDashboard: () => http<any>("/api/camera-ai/dashboard"),
  cameraTrailers: (limit = 100) => http<{ count: number; trailers: any[] }>(`/api/camera-ai/trailer?limit=${limit}`),
  cameraContainers: (limit = 100) => http<{ count: number; containers: any[] }>(`/api/camera-ai/container?limit=${limit}`),
  cameraCountIngest: (body: Record<string, any>) =>
    http<any>("/api/camera-ai/counts", { method: "POST", body: JSON.stringify(body) }),
  cameraTrailerIngest: (body: Record<string, any>) =>
    http<any>("/api/camera-ai/trailer", { method: "POST", body: JSON.stringify(body) }),
  cameraContainerIngest: (body: Record<string, any>) =>
    http<any>("/api/camera-ai/container", { method: "POST", body: JSON.stringify(body) }),

  // --- Document OCR (Feature 6) ---
  ocrDocuments: (params?: { doc_type?: string; limit?: number }) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    return http<{ count: number; documents: any[] }>(`/api/ocr/documents${q.toString() ? `?${q}` : ""}`);
  },
  ocrDocument: (id: number) => http<any>(`/api/ocr/documents/${id}`),
  ocrHealth: () => http<any>("/api/ocr/health"),
  ocrUpload: (file: File, docType: string, sourceRef?: string) => {
    const fd = new FormData();
    fd.append("file", file, file.name);
    fd.append("doc_type", docType);
    if (sourceRef) fd.append("source_ref", sourceRef);
    return postForm<any>("/api/ocr/document", fd);
  },

  // --- NVR (Feature 7) ---
  nvrDevices: () => http<{ count: number; devices: any[] }>("/api/nvr/devices"),
  nvrDevice: (id: string) => http<any>(`/api/nvr/devices/${encodeURIComponent(id)}`),
  nvrStreams: () => http<{ count: number; streams: any[] }>("/api/nvr/streams"),
  nvrHealth: () => http<any>("/api/nvr/health"),
  nvrRegister: (body: Record<string, any>) =>
    http<any>("/api/nvr/devices", { method: "POST", body: JSON.stringify(body) }),
  nvrMapChannel: (id: string, body: Record<string, any>) =>
    http<any>(`/api/nvr/devices/${encodeURIComponent(id)}/channels`, { method: "POST", body: JSON.stringify(body) }),

  // --- ECY TRT (Feature 8) ---
  trtSummary: () => http<any>("/api/trt/summary"),
  trtRecords: (params?: { status?: string; vehicle_id?: string; limit?: number }) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    return http<{ count: number; records: any[] }>(`/api/trt/records${q.toString() ? `?${q}` : ""}`);
  },
  trtPhase: (body: Record<string, any>) =>
    http<any>("/api/trt/phase", { method: "POST", body: JSON.stringify(body) }),

  // --- Bottlenecks (Feature 9) ---
  bottlenecks: (top = 3) => http<any>(`/api/bottlenecks?top=${top}`),
  bottleneckSnapshot: () => http<any>("/api/bottlenecks/snapshot", { method: "POST" }),
  bottleneckHistory: (limit = 100) => http<{ count: number; snapshots: any[] }>(`/api/bottlenecks/history?limit=${limit}`),

  // --- Reefer (Feature 11) ---
  reeferSlots: (params?: { facility_id?: string; status?: string }) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    return http<{ count: number; slots: any[] }>(`/api/reefer/slots${q.toString() ? `?${q}` : ""}`);
  },
  reeferAvailability: () => http<any>("/api/reefer/availability"),
  reeferSeed: (count = 24) =>
    http<any>("/api/reefer/seed", { method: "POST", body: JSON.stringify({ count }) }),
  reeferAllocate: (body: Record<string, any>) =>
    http<any>("/api/reefer/allocate", { method: "POST", body: JSON.stringify(body) }),
  reeferRelease: (body: Record<string, any>) =>
    http<any>("/api/reefer/release", { method: "POST", body: JSON.stringify(body) }),

  // --- Integrations: PDP / LDB / RMS-TAS (Features 12/13/14) ---
  pdpVehicle: (plate: string) => http<any>(`/api/pdp/vehicle/${encodeURIComponent(plate)}`),
  pdpTraffic: () => http<any>("/api/pdp/traffic"),
  pdpHealth: () => http<any>("/api/pdp/health"),
  ldbContainer: (no: string) => http<any>(`/api/ldb/container/${encodeURIComponent(no)}`),
  ldbMovements: (no: string) => http<any>(`/api/ldb/container/${encodeURIComponent(no)}/movements`),
  ldbHealth: () => http<any>("/api/ldb/health"),
  rmsSlots: (params?: { gate_id?: string; date?: string }) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    return http<{ count: number; slots: any[] }>(`/api/rms-tas/slots${q.toString() ? `?${q}` : ""}`);
  },
  rmsHealth: () => http<any>("/api/rms-tas/health"),
  rmsSeed: (body: Record<string, any>) =>
    http<any>("/api/rms-tas/seed", { method: "POST", body: JSON.stringify(body) }),
  rmsBook: (body: Record<string, any>) =>
    http<any>("/api/rms-tas/book", { method: "POST", body: JSON.stringify(body) }),

  // --- TT Double Trip (Feature 15) ---
  doubleTripStatistics: () => http<any>("/api/double-trip/statistics"),
  doubleTripCycles: (params?: { vehicle_id?: string; limit?: number }) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    return http<{ count: number; cycles: any[] }>(`/api/double-trip/cycles${q.toString() ? `?${q}` : ""}`);
  },
  doubleTripStart: (body: Record<string, any>) =>
    http<any>("/api/double-trip/start", { method: "POST", body: JSON.stringify(body) }),
  doubleTripComplete: (tripId: number) =>
    http<any>(`/api/double-trip/${tripId}/complete`, { method: "POST" }),
};

export interface WfField {
  key: string;
  label: string;
  unit: string;
  type: "number" | "string";
}
export interface WfAction {
  key: string;
  label: string;
}
export interface WfRuleInput {
  name: string;
  field: string;
  op: string;
  value: string | number;
  actions: string[];
  enabled?: boolean;
}
export interface WfRule extends WfRuleInput {
  id: string;
  value: string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}
export interface WfExecutionResult {
  rule_id: string;
  name: string;
  condition: string;
  field_present: boolean;
  matched: boolean;
  actions_fired: string[];
}
export interface WfExecution {
  ts: string;
  event: Record<string, unknown>;
  results: WfExecutionResult[];
  matched_count: number;
}
