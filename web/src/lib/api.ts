// Thin fetch wrapper around the gateway's /api surface. The app always calls
// relative paths; the Vite dev proxy (dev) or nginx (prod) forwards to the
// gateway. Every helper returns parsed JSON and throws on non-2xx so TanStack
// Query surfaces the error state.

import { authEnabled, clearSession, getToken } from "./auth";
import { getDataSourceMode } from "./dataSourceMode";
import type { AvailableVehicle, DqIssue, EmptyTrtResponse } from "./types";

// Request budget. Without one, `fetch` waits indefinitely: the 2026-08-04 audit
// measured /api/kpi at 81s against RDS and the panel simply hung — no spinner
// resolution, no error state, nothing for the operator to act on. A bounded wait
// that surfaces a clear failure is always better than an unbounded one.
export const DEFAULT_TIMEOUT_MS = 15_000;

// Uploads/downloads move megabytes over venue wifi; they get a longer budget.
export const UPLOAD_TIMEOUT_MS = 120_000;

// ULIP LDB/01 aggregates a container's whole trail across terminals, rail and
// road and measures 10-20s on production — routinely longer than the 15s
// default, so container tracking failed in the browser while the gateway was
// answering correctly. Matches the gateway's own ULIP_LDB_TIMEOUT_S budget.
export const LDB_TIMEOUT_MS = 35_000;

// Marker used on the thrown Error so apiError() can classify a timeout without
// depending on the browser's DOMException wording (which differs across engines).
const TIMEOUT_MARKER = "ETIMEDOUT";

/** AbortSignal that fires after `ms`, combined with any caller-supplied signal. */
function timeoutSignal(ms: number, existing?: AbortSignal | null): AbortSignal {
  // AbortSignal.any is not in every target browser yet; fall back to the plain
  // timeout signal when the caller passed none (the overwhelming majority).
  const timeout = AbortSignal.timeout(ms);
  if (!existing) return timeout;
  const anyOf = (AbortSignal as { any?: (s: AbortSignal[]) => AbortSignal }).any;
  return anyOf ? anyOf([existing, timeout]) : timeout;
}

/** True when `err` is an abort raised by our own timeout (not a user cancel). */
function isTimeout(err: unknown): boolean {
  return err instanceof DOMException && err.name === "TimeoutError";
}

async function http<T>(
  path: string,
  init?: RequestInit,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  // Attach the bearer token when a session exists (auth-enabled builds). When
  // auth is disabled there is no token and the header is simply omitted.
  const token = getToken();
  const authHeader: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
  // Data-source provenance filter (LIVE = JNPA-API rows, DEMO = pre-loaded).
  const dataModeHeader: Record<string, string> = { "x-data-mode": getDataSourceMode() };
  let res: Response;
  try {
    res = await fetch(path, {
      // `init` is spread FIRST (method, body, …). It must not come after
      // `headers`: when a caller passes its own `headers` object that key
      // overwrites the merged one below wholesale, dropping Authorization and
      // x-data-mode — which surfaced as a 401 "missing bearer token" on the one
      // call that sets its own headers (geoNotifyZone). Caller headers still win
      // per-key, because init.headers is spread last INSIDE the merged object.
      ...init,
      headers: {
        "content-type": "application/json",
        ...authHeader,
        ...dataModeHeader,
        ...(init?.headers || {}),
      },
      signal: timeoutSignal(timeoutMs, init?.signal),
    });
  } catch (err) {
    if (isTimeout(err)) {
      // Shaped like the HTTP errors below so apiError() parses it uniformly and
      // panels can render one consistent failure state.
      throw new Error(
        `408 Request Timeout — ${JSON.stringify({
          detail: {
            error: TIMEOUT_MARKER,
            detail: `The server did not respond within ${Math.round(timeoutMs / 1000)}s.`,
            path,
          },
        })}`,
      );
    }
    throw err;
  }
  if (!res.ok) {
    if (res.status === 401) onUnauthorized();
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

/** Drop an expired/revoked session and return to the login gate.
 *
 *  A JWT lasts at most 8 h and cannot be revoked server-side, so the console has
 *  to notice a dead session itself. Without this a 401 surfaced as a generic
 *  panel error and the operator was stuck on a dashboard where nothing loaded
 *  and nothing offered a way to sign in again. Guarded on a token actually being
 *  present so an anonymous 401 can never loop the page. */
function onUnauthorized(): void {
  if (!authEnabled() || !getToken()) return;
  clearSession();
  try {
    window.location.assign("/");
  } catch {
    /* navigation is unimplemented in jsdom (unit tests) */
  }
}

// The gateway reports refusals as `{detail: {error, detail, ...extra}}` with a
// machine-readable `error` code (e.g. "pdp_expired", "no_gate_document"), but
// http() flattens that into an Error message so TanStack Query can surface it.
// This reads the structure back out so a caller can render the precise reason
// instead of a raw "400 Bad Request — {...}" string.
export interface ApiErrorInfo {
  status: number | null;
  code: string | null;
  detail: string;
  extra: Record<string, unknown>;
  /** True when the request exceeded its client-side budget (see http()). */
  timedOut: boolean;
}

/** Convenience for panels: "did this fail because the server was too slow?" */
export function isTimeoutError(err: unknown): boolean {
  return apiError(err).code === TIMEOUT_MARKER;
}

export function apiError(err: unknown): ApiErrorInfo {
  const message = err instanceof Error ? err.message : String(err ?? "");
  const status = Number.parseInt(message, 10);
  const sep = message.indexOf(" — ");
  const out: ApiErrorInfo = {
    status: Number.isNaN(status) ? null : status,
    code: null,
    detail: message,
    extra: {},
    timedOut: false,
  };
  if (sep === -1) return out;
  try {
    const body = JSON.parse(message.slice(sep + 3)) as { detail?: unknown };
    const d = body?.detail;
    if (typeof d === "string") return { ...out, detail: d };
    if (d && typeof d === "object") {
      const { error, detail, ...extra } = d as Record<string, unknown>;
      const code = typeof error === "string" ? error : null;
      return {
        ...out,
        code,
        detail: typeof detail === "string" ? detail : out.detail,
        extra,
        timedOut: code === TIMEOUT_MARKER,
      };
    }
  } catch {
    /* non-JSON error body — keep the raw message */
  }
  return out;
}

// Authenticated file download. A plain <a href>/new-tab navigation can NOT carry
// the bearer token, so it 401s ("missing bearer token") on auth-enabled builds.
// Fetch the file with the token attached, then save the response blob via a
// temporary object URL.
async function downloadFile(path: string, filename: string): Promise<void> {
  const token = getToken();
  const authHeader: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
  const res = await fetch(path, {
    // Data-source provenance filter (LIVE = JNPA-API rows, DEMO = pre-loaded).
    headers: { ...authHeader, "x-data-mode": getDataSourceMode() },
    signal: timeoutSignal(UPLOAD_TIMEOUT_MS),
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
  const res = await fetch(path, {
    method: "POST",
    // Data-source provenance filter (LIVE = JNPA-API rows, DEMO = pre-loaded).
    headers: { ...authHeader, "x-data-mode": getDataSourceMode() },
    body: form,
    signal: timeoutSignal(UPLOAD_TIMEOUT_MS),
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
  // NETC tag registry (ULIP FASTAG/02). Supply EXACTLY ONE of rcNumber /
  // tagId — the upstream rejects both together (respCode 239). A vehicle can
  // hold several tags (re-issues), so `tags` is a list.
  fastagTagStatus: (body: { rc_number?: string; tag_id?: string }) =>
    http<import("./types").FastagTagStatus>("/api/fastag/tag-status", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // --- GatiShakti reference data — /api/gatishakti/* ---
  // Backend-only reference master data (NHAI toll plazas, road network).
  // Served from core.gs_*; `refresh` is what re-pulls it from ULIP.
  gatishaktiHealth: () => http<any>("/api/gatishakti/health"),
  gatishaktiTollPlazas: (stateId = "27", limit = 500) =>
    http<import("./types").GatiShaktiRows>(
      `/api/gatishakti/toll-plazas?state_id=${encodeURIComponent(stateId)}&limit=${limit}`,
    ),
  gatishaktiRoads: (params?: { state_id?: string; nh_no?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.state_id) q.set("state_id", params.state_id);
    if (params?.nh_no) q.set("nh_no", params.nh_no);
    if (params?.limit) q.set("limit", String(params.limit));
    return http<import("./types").GatiShaktiRows>(
      `/api/gatishakti/roads${q.toString() ? `?${q}` : ""}`,
    );
  },
  gatishaktiRoadPoints: (stateId = "27", limit = 1000) =>
    http<import("./types").GatiShaktiRows>(
      `/api/gatishakti/road-points?state_id=${encodeURIComponent(stateId)}&limit=${limit}`,
    ),

  // --- Terminal Appointment System (TFC-1) ---
  tasSlots: (gateId?: string) =>
    http<{ slots: import("./types").TasSlot[] }>(
      `/api/tas/slots${gateId ? `?gate_id=${encodeURIComponent(gateId)}` : ""}`,
    ),
  // Cross-twin XT-2: DeferredArrivalWindow events consumed from UC-II via
  // jnpa.crosstwin.deferred-arrival and applied to the TAS slot book.
  tasDeferredWindows: () => http<{ windows: any[] }>(`/api/tas/deferred-windows`),

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
  // Full customs document view of one container (module 5, /api/customs). Reused
  // by the ICEGATE details drawer; RBAC matches /api/gate-data (CONTROL_ROOM|CUSTOMS).
  customsContainer: (containerNo: string) =>
    http<import("./types").CustomsContainerView>(
      `/api/customs/containers/${encodeURIComponent(containerNo)}`,
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
  // Operator-triggered zone notification. entry_time pins the request to ONE
  // occupancy, so a stale row can never notify against a later re-entry. 409
  // when the vehicle has left (vehicle_not_in_zone) or re-entered since
  // (occupancy_changed); `created: false` means it was already triggered.
  geoNotifyZone: (vehicleId: string, zoneId: string, entryTime: string) =>
    http<{
      alert_id: string;
      created: boolean;
      vehicle_id: string;
      zone_id: string;
      entry_time: string;
      email: { attempted: boolean; delivered: boolean };
      // Per-transport outcome of the push to the vehicle's own driver.
      // device_resolved:false = the vehicle has no paired PWA device, so only
      // the control room was notified.
      driver: { device_resolved: boolean; webpush: boolean; fcm: boolean };
    }>("/api/geo/zones/notify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        vehicle_id: vehicleId,
        zone_id: zoneId,
        entry_time: entryTime,
      }),
    }),
  // ---- UC3 Email Processing (/api/email) ---------------------------------
  // The mailbox password is server-side only: no endpoint below returns it and
  // emailHealth() answers with a masked address plus a connected flag.
  emailHealth: () => http<EmailHealth>("/api/email/health"),
  emailSync: () =>
    http<{ scanned: number; stored: number; subject_prefix: string }>("/api/email/sync", {
      method: "POST",
    }),
  emailMessages: (status?: string, limit = 50, offset = 0) => {
    const q = new URLSearchParams();
    if (status) q.set("status", status);
    q.set("limit", String(limit));
    q.set("offset", String(offset));
    return http<{ items: EmailMessage[]; total: number }>(`/api/email/messages?${q.toString()}`);
  },
  emailMessage: (id: number) => http<EmailMessageDetail>(`/api/email/messages/${id}`),
  // Dry run: classifies and validates, writes nothing. Shows the operator which
  // master table the data would land in BEFORE anything is imported.
  emailPreview: (id: number) =>
    http<EmailProcessResult>(`/api/email/messages/${id}/preview`, { method: "POST" }),
  emailImport: (id: number, override = false) =>
    http<EmailProcessResult>(
      `/api/email/messages/${id}/import${override ? "?override=true" : ""}`,
      { method: "POST" },
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
  // Vehicle 360: one call for the whole operator view (master + driver + licence
  // + transporter + compliance + timeline). Wraps vehicle-intel server-side, so
  // the profile screen needs a single round-trip instead of a lookup chain.
  vehicle360: (plate: string) =>
    http<import("./types").Vehicle360>(`/api/vahan/vehicle-360/${encodeURIComponent(plate)}`),
  driverIntel: (key: string) =>
    http<import("./types").DriverIntel>(`/api/vahan/driver-intel/${encodeURIComponent(key)}`),
  // Registration lookup down the vehicle ladder: ULIP VAHAN/04 -> VAHAN/01 ->
  // vahan-sim -> CACHED -> PROVISIONAL. This is the LIVE registry read.
  // ``vehicleIntel`` above is a different thing — an RDS aggregate of what we
  // have already stored about a plate — and answers ``rc: null`` for a vehicle
  // the port has never seen, so it must not be used to look a vehicle up.
  vahanRc: (plate: string) =>
    http<{ plate: string; decision_path: string; record: Record<string, unknown> }>(
      `/api/vahan/rc/${encodeURIComponent(plate)}`,
    ),
  // Alternate-key RC lookups (ULIP VAHAN/02 and /03). ULIP-only — the
  // simulator is keyed by plate — so these 503 when ULIP_LIVE_ENABLED is off
  // and 404 on a miss rather than falling back to a different vehicle.
  vahanByChassis: (chassisNumber: string) =>
    http<{ chassis: string; decision_path: string; record: Record<string, unknown> }>(
      `/api/vahan/chassis/${encodeURIComponent(chassisNumber)}`,
    ),
  vahanByEngine: (engineNumber: string) =>
    http<{ engine: string; decision_path: string; record: Record<string, unknown> }>(
      `/api/vahan/engine/${encodeURIComponent(engineNumber)}`,
    ),
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
  accidents: (params?: {
    status?: string;
    accident_type?: string;
    vehicle_id?: string;
    limit?: number;
  }) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    return http<{ count: number; accidents: any[] }>(
      `/api/accidents${q.toString() ? `?${q}` : ""}`,
    );
  },
  accidentDashboard: () => http<any>("/api/accidents/dashboard"),
  accident: (id: number | string) =>
    http<{ accident: any; timeline: any[] }>(`/api/accidents/${id}`),
  accidentReport: (body: Record<string, any>) =>
    http<{ created: boolean; accident: any }>("/api/accidents", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  accidentStatus: (id: number | string, body: Record<string, any>) =>
    http<any>(`/api/accidents/${id}/status`, { method: "POST", body: JSON.stringify(body) }),
  accidentInvestigation: (id: number | string, body: Record<string, any>) =>
    http<any>(`/api/accidents/${id}/investigation`, { method: "POST", body: JSON.stringify(body) }),
  accidentResolve: (id: number | string, body: Record<string, any>) =>
    http<any>(`/api/accidents/${id}/resolve`, { method: "POST", body: JSON.stringify(body) }),

  // --- Transporter blacklist (Feature 2) ---
  transporters: (params?: { q?: string; status?: string; limit?: number; offset?: number }) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    // `total` is the registry-wide COUNT(*); `count` is only this page's length.
    return http<{
      items: any[];
      transporters: any[];
      total: number;
      limit: number;
      offset: number;
      count: number;
    }>(`/api/transporters${q.toString() ? `?${q}` : ""}`);
  },
  transporterBlacklist: (params?: { limit?: number; offset?: number }) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    return http<{
      items: any[];
      blacklist: any[];
      total: number;
      limit: number;
      offset: number;
      count: number;
    }>(`/api/transporters/blacklist${q.toString() ? `?${q}` : ""}`);
  },
  transporterStats: () =>
    http<{ total: number; active: number; blacklisted: number; vehicles_assigned: number }>(
      "/api/transporters/stats",
    ),
  transporter: (id: number) =>
    http<{ transporter: any; vehicles: any[]; blacklist_history: any[] }>(
      `/api/transporters/${id}`,
    ),
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

  // --- Driver Master & Intelligence (read-only registry) ---
  driversMaster: (params?: {
    q?: string;
    company?: string;
    status?: string;
    enrolled?: boolean;
    verification?: string;
    transporter_id?: number;
    sort?: string;
    direction?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/drivers/master${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  driverMasterStats: () =>
    http<{
      total_drivers: number;
      active_pdp: number;
      expiring_soon: number;
      expired_pdp: number;
      companies: number;
      enrolled: number;
      pending_enrollment: number;
      not_enrolled: number;
    }>("/api/drivers/master/stats"),
  driverMaster: (licence: string) =>
    http<any>(`/api/drivers/master/${encodeURIComponent(licence)}`),
  driverMasterPdpHistory: (licence: string, params?: { limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && qs.set(k, String(v)));
    return http<{
      licence: string;
      appl_number: string | null;
      items: any[];
      total: number;
      limit: number;
      offset: number;
      count: number;
    }>(
      `/api/drivers/master/${encodeURIComponent(licence)}/pdp-history${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  driverMasterValidate: (licence: string) =>
    http<any>(`/api/drivers/master/validate/${encodeURIComponent(licence)}`),

  // --- CFS-ECY CODECO gate movements (module 13, read-only) ---
  cfsEcyMovements: (params?: {
    facility?: string;
    mode?: string;
    container?: string;
    from?: string;
    to?: string;
    sort?: string;
    direction?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/cfs-ecy/movements${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  cfsEcyStats: (params?: { facility?: string; from?: string; to?: string }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{
      total_in: number;
      total_out: number;
      total_events: number;
      container_count: number;
      active_containers: number;
      iso_invalid: number;
      average_dwell_hours: number | null;
      median_dwell_hours: number | null;
      dwell_count: number;
      daily_throughput: { day: string; in_count: number; out_count: number }[];
    }>(`/api/cfs-ecy/stats${qs.toString() ? `?${qs}` : ""}`);
  },
  cfsEcyDwell: (params?: { from?: string; to?: string; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; total: number; summary: any; note: string }>(
      `/api/cfs-ecy/dwell${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  cfsEcyContainer: (containerNumber: string) =>
    http<any>(`/api/cfs-ecy/containers/${encodeURIComponent(containerNumber)}`),

  // --- CFS-ECY Data Upload (module 13 sub-module) — mirrors the shipping-lines helpers ---
  cfsEcyDownloadTemplate: (facility: string) =>
    downloadFile(`/api/cfs-ecy/templates/${facility}`, `cfs_ecy_${facility}_template.csv`),
  cfsEcyUploadValidate: (facility: string, file: File) => {
    const f = new FormData();
    f.append("file", file);
    f.append("facility", facility);
    return postForm<any>("/api/cfs-ecy/validate", f);
  },
  cfsEcyUpload: (facility: string, file: File) => {
    const f = new FormData();
    f.append("file", file);
    f.append("facility", facility);
    return postForm<any>("/api/cfs-ecy/upload", f);
  },
  cfsEcyUploads: (params?: { facility?: string; status?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && qs.set(k, String(v)));
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/cfs-ecy/uploads${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  cfsEcyUploadDetail: (fileId: number) => http<any>(`/api/cfs-ecy/uploads/${fileId}`),

  // --- UC3-003: empty-container gate events + the TRT KPI (real CODECO corpus) ---
  // These read core.container_event (the imported ECY/CFS CODECO gate log), not
  // core.cfs_ecy_movement, so the KPI is computed from the corpus feed alone.
  cfsEcyEvents: (params?: {
    container?: string;
    location_type?: string;
    event_type?: string;
    direction?: string;
    from?: string;
    to?: string;
    sort?: string;
    order?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/cfs-ecy/events${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  /** KPI 3 "TRT for empty containers from ECD" plus the evidence behind it. */
  emptyTrt: () => http<EmptyTrtResponse>("/api/cfs-ecy/empty-trt"),
  emptyTrtChains: (params?: {
    container?: string;
    chain_status?: string;
    anomaly_code?: string;
    anomaly_only?: boolean;
    sort?: string;
    order?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/cfs-ecy/empty-trt/chains${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  emptyTrtAnomaly: (code: string, params?: { limit?: number; offset?: number }) => {
    // Numeric-only params, so no empty-string guard (and TS rejects one).
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && qs.set(k, String(v)));
    return http<{ code: string; label: string; items: any[]; total: number }>(
      `/api/cfs-ecy/empty-trt/anomalies/${encodeURIComponent(code)}${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  emptyTrtContainer: (containerNo: string) =>
    http<any>(`/api/cfs-ecy/empty-trt/containers/${encodeURIComponent(containerNo)}`),

  // --- Data Quality ledger (core.dq_issue) ---
  dqIssues: (params?: {
    source_table?: string;
    issue_type?: string;
    severity?: string;
    file_id?: number;
    q?: string;
    sort?: string;
    order?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: DqIssue[]; total: number; limit: number; offset: number; count: number }>(
      `/api/dq/issues${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  dqSummary: (params?: { source_table?: string; severity?: string }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{
      total: number;
      errors: number;
      warnings: number;
      info: number;
      by_source_table: any[];
      by_issue_type: any[];
    }>(`/api/dq/summary${qs.toString() ? `?${qs}` : ""}`);
  },

  // --- ECY→CFS repositioning chains (F-Y1 lifecycle, migration 0114) ---
  ecyCfsChains: (params?: {
    container?: string;
    chain_status?: string;
    anomaly_only?: boolean;
    anomaly_code?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{
      items: EcyCfsChain[];
      total: number;
      limit: number;
      offset: number;
      count: number;
    }>(`/api/cfs-ecy/chains${qs.toString() ? `?${qs}` : ""}`);
  },
  ecyCfsChain: (containerNumber: string) =>
    http<EcyCfsChain>(`/api/cfs-ecy/chains/${encodeURIComponent(containerNumber)}`),
  ecyCfsChainStats: () => http<EcyCfsChainStats>("/api/cfs-ecy/chains/stats"),
  ecyCfsChainRebuild: () =>
    http<{ chains: number; complete: number; anomalies: number; ms: number }>(
      "/api/cfs-ecy/chains/rebuild",
      { method: "POST" },
    ),

  // --- UC-III gate documents (EIR / PIN ticket / Form-13) ---
  gateDocSummary: () => http<GateDocSummary>("/api/gate-docs/summary"),
  gateDocList: (
    docType: "eir" | "pin" | "form13",
    params?: {
      container?: string;
      truck?: string;
      vehicle?: string;
      pin?: string;
      visit_id?: string;
      terminal?: string;
      limit?: number;
      offset?: number;
    },
  ) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/gate-docs/${docType}${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  // Form-13 provenance: in LIVE data mode the timeline pins source=live so
  // simulator-generated Form 13s never mix into the ingested document trail
  // (the server default is "all"). Explicit `source` overrides the pin.
  gateDocsForContainer: (containerNo: string, source?: "live" | "sim" | "all") => {
    const pin = source ?? (getDataSourceMode() === "LIVE" ? "live" : undefined);
    return http<GateDocBundle>(
      `/api/gate-docs/container/${encodeURIComponent(containerNo)}${pin ? `?source=${pin}` : ""}`,
    );
  },
  gateDocsForTruck: (truckNo: string, source?: "live" | "sim" | "all") => {
    const pin = source ?? (getDataSourceMode() === "LIVE" ? "live" : undefined);
    return http<GateDocBundle>(
      `/api/gate-docs/truck/${encodeURIComponent(truckNo)}${pin ? `?source=${pin}` : ""}`,
    );
  },
  /** REAL parsed gate documents (core.gate_document) — the T-04 truck-visit
   *  source. Distinct from gateDocsForTruck, which reads the upload tables. */
  // --- UC3-004 vehicle -> transporter registry ---
  vehicleMappings: (params: {
    provenance?: VehicleProvenance;
    q?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)));
    return http<VehicleMappingPage>(
      `/api/vehicle-registry/mappings${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  vehicleMapping: (plate: string) =>
    http<VehicleMapping>(`/api/vehicle-registry/vehicle/${encodeURIComponent(plate)}`),
  vehicleRegistrySummary: () => http<VehicleRegistrySummary>("/api/vehicle-registry/summary"),

  // --- UC3-005 corridor simulation ---
  corridorSimSummary: () => http<CorridorSimSummary>("/api/corridor-sim/summary"),
  corridorSimTrucks: (params: {
    segment?: string;
    direction?: "IN" | "OUT";
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)));
    return http<CorridorSimTruckPage>(`/api/corridor-sim/trucks${qs.toString() ? `?${qs}` : ""}`);
  },

  gateSourceDocs: (params: {
    vehicle?: string;
    container?: string;
    driver_licence?: string;
    category?: string;
    terminal?: string;
    from_date?: string;
    to_date?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)));
    return http<GateSourceDocPage>(`/api/gate-docs/documents${qs.toString() ? `?${qs}` : ""}`);
  },
  gateDocTat: (terminal?: string) =>
    http<GateDocTat>(
      `/api/gate-docs/tat${terminal ? `?terminal=${encodeURIComponent(terminal)}` : ""}`,
    ),
  gateDocDownloadTemplate: (docType: string) =>
    downloadFile(
      `/api/gate-docs/templates/${docType}`,
      `gate_doc_${docType.toLowerCase()}_template.csv`,
    ),
  gateDocUploadValidate: (docType: string, file: File) => {
    const f = new FormData();
    f.append("file", file);
    f.append("doc_type", docType);
    return postForm<any>("/api/gate-docs/validate", f);
  },
  gateDocUpload: (docType: string, file: File) => {
    const f = new FormData();
    f.append("file", file);
    f.append("doc_type", docType);
    return postForm<any>("/api/gate-docs/upload", f);
  },
  gateDocUploads: (params?: { doc_type?: string; status?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && qs.set(k, String(v)));
    return http<{ items: any[]; total: number; count: number }>(
      `/api/gate-docs/uploads${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  gateDocUploadDetail: (fileId: number) => http<any>(`/api/gate-docs/uploads/${fileId}`),

  // --- UC-III job spine: assignment + gate / yard / scan events ---
  jobs: (params?: {
    container?: string;
    vehicle_id?: string;
    driver_id?: string;
    status?: string;
    open_only?: boolean;
    /** Prefix the page with the UC-II -> UC-III handover queue (released, no truck yet). */
    include_pending?: boolean;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{
      items: JobListItem[];
      total: number;
      limit: number;
      offset: number;
      count: number;
    }>(`/api/jobs${qs.toString() ? `?${qs}` : ""}`);
  },
  job: (jobId: number) => http<ContainerJob & { events: JobEvent[] }>(`/api/jobs/${jobId}`),
  // The handover queue on its own: containers UC-II released that still need a truck.
  pendingHandover: (params?: { container?: string; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{
      items: PendingHandoverEntry[];
      total: number;
      limit: number;
      offset: number;
      count: number;
    }>(`/api/jobs/pending-handover${qs.toString() ? `?${qs}` : ""}`);
  },
  jobValidate: (body: JobAssignInput) =>
    http<{ ok: boolean; checks: JobCheck[]; vehicle: any; permit: any }>("/api/jobs/validate", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  jobAssign: (body: JobAssignInput) =>
    http<{ job: ContainerJob; checks: JobCheck[] }>("/api/jobs", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  jobAccept: (jobId: number) =>
    http<{ job: ContainerJob }>(`/api/jobs/${jobId}/accept`, { method: "POST" }),
  jobComplete: (jobId: number, notes?: string) =>
    http<{ job: ContainerJob }>(`/api/jobs/${jobId}/complete`, {
      method: "POST",
      body: JSON.stringify({ notes }),
    }),
  jobCancel: (jobId: number, reason: string) =>
    http<{ job: ContainerJob }>(`/api/jobs/${jobId}/cancel`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
  jobForContainer: (containerNo: string) =>
    http<ContainerJob & { events: JobEvent[] }>(
      `/api/cargo-jobs/container/${encodeURIComponent(containerNo)}`,
    ),

  // --- assignment dropdown sources ------------------------------------------
  // The two masters a job is raised against. Both resolve to the SAME tables the
  // assignment validator checks (core.vehicle / core.driver_identity), so a
  // selected option's id is always a valid vehicle_id / driver_id.
  // NOTE: these two live under a stricter RBAC policy than /api/jobs itself
  // (CUSTOMS + DTCCC_ADMIN only) — callers must handle 403 (see JobAssignPanel).
  availableVehicles: (q?: string, limit = 50) => {
    const qs = new URLSearchParams({ limit: String(limit) });
    if (q) qs.set("q", q);
    // `count` is this page's length (capped by `limit`); `available_total` is how
    // many vehicles the DATABASE says are assignable right now — that is the
    // number the "Vehicle (N available)" label must show.
    return http<{ vehicles: AvailableVehicle[]; count: number; available_total: number }>(
      `/api/vehicles/available?${qs.toString()}`,
    );
  },
  /** The full enrolled roster (occupied or not) — verification gallery, not Assign Job. */
  activeDrivers: () => http<{ drivers: ActiveDriver[]; count: number }>("/api/identity/drivers"),

  /**
   * Drivers who can take a NEW job: ACTIVE and holding no open container job.
   * The exclusion is a SQL NOT EXISTS on core.container_job_assignment, so an
   * occupied driver is absent from the page AND unfindable by `q` — the client
   * never receives them to filter. Counterpart of `availableVehicles`.
   */
  availableDrivers: (q?: string, limit = 50) => {
    const qs = new URLSearchParams({ limit: String(limit) });
    if (q) qs.set("q", q);
    return http<{ drivers: ActiveDriver[]; count: number; available_total: number }>(
      `/api/identity/drivers/available?${qs.toString()}`,
    );
  },

  gateEventCreate: (body: {
    event_type: string;
    plate: string;
    gate_id?: string;
    job_id?: number;
    container_number?: string;
    bat_lane?: string;
    document_type?: string;
    document_reference?: string;
  }) =>
    http<{ gate_event: any; job: ContainerJob | null }>("/api/gate/events", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  gateEvents: (params?: {
    plate?: string;
    container?: string;
    job_id?: number;
    limit?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; count: number }>(
      `/api/gate/events${qs.toString() ? `?${qs}` : ""}`,
    );
  },

  yardMovementCreate: (body: {
    movement_type: string;
    job_id?: number;
    container_number?: string;
    yard_location?: string;
    from_location?: string;
    terminal?: string;
  }) =>
    http<{ movement: any; job: ContainerJob | null }>("/api/yard/movements", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  yardMovements: (params?: { container?: string; job_id?: number; limit?: number }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; count: number }>(
      `/api/yard/movements${qs.toString() ? `?${qs}` : ""}`,
    );
  },

  scanMachines: () => http<{ items: ScannerMachine[]; count: number }>("/api/scan/machines"),
  scanStatus: (containerNo: string) =>
    http<ScanStatus>(`/api/scan/status/${encodeURIComponent(containerNo)}`),
  scanRecord: (body: {
    container_number: string;
    result: string;
    machine_code?: string;
    job_id?: number;
    remarks?: string;
  }) => http<{ scan: any }>("/api/scan/events", { method: "POST", body: JSON.stringify(body) }),
  scanEvents: (params?: { container?: string; result?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; count: number }>(
      `/api/scan/events${qs.toString() ? `?${qs}` : ""}`,
    );
  },

  // --- Berthing Reports (module 7) — vessel calls + lifecycle + Data Upload ---
  berthingReports: (params?: {
    terminal?: string;
    status?: string;
    vessel?: string;
    voyage?: string;
    berthed_only?: boolean;
    from?: string;
    to?: string;
    sort?: string;
    direction?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/berthing${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  berthingStats: (params?: { terminal?: string; from?: string; to?: string }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{
      total: number;
      expected: number;
      arrived: number;
      berthed: number;
      completed: number;
      departed: number;
      terminals: number;
      avg_berth_hours: number | null;
      by_terminal: { terminal: string; count: number; berthed: number }[];
    }>(`/api/berthing/stats${qs.toString() ? `?${qs}` : ""}`);
  },
  berthingReport: (id: number) => http<any>(`/api/berthing/${id}`),
  berthingTimeline: (id: number) => http<any>(`/api/berthing/${id}/timeline`),

  // Data Upload (module 7 sub-module) — mirrors the cfs-ecy helpers.
  berthingDownloadTemplate: (terminal: string) =>
    downloadFile(`/api/berthing/templates/${terminal || "ALL"}`, `berthing_template.csv`),
  berthingUploadValidate: (terminal: string, file: File) => {
    const f = new FormData();
    f.append("file", file);
    if (terminal) f.append("terminal", terminal);
    return postForm<any>("/api/berthing/validate", f);
  },
  berthingUpload: (terminal: string, file: File) => {
    const f = new FormData();
    f.append("file", file);
    if (terminal) f.append("terminal", terminal);
    return postForm<any>("/api/berthing/upload", f);
  },
  berthingUploads: (params?: { terminal?: string; status?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && qs.set(k, String(v)));
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/berthing/uploads${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  berthingUploadDetail: (fileId: number) => http<any>(`/api/berthing/uploads/${fileId}`),

  // --- Berthing Full Extract (module 7 sub-module) — verbatim every-table PDF capture ---
  berthingExtract: (file: File) => {
    const f = new FormData();
    f.append("file", file);
    return postForm<any>("/api/berthing/extract", f);
  },
  berthingExtractImport: (file: File) => {
    const f = new FormData();
    f.append("file", file);
    return postForm<any>("/api/berthing/extract/import", f);
  },
  berthingDocuments: (params?: { terminal?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && qs.set(k, String(v)));
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/berthing/documents${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  berthingDocumentTables: (documentId: number) =>
    http<any>(`/api/berthing/documents/${documentId}/tables`),
  berthingDocumentFullView: (documentId: number) =>
    http<{
      document_id: number;
      file_name: string;
      terminal: string;
      report_date: string | null;
      page_count: number;
      table_count: number;
      row_count: number;
      pdf_hash?: string;
      pdf_available?: boolean;
      tables: {
        table_name: string;
        columns: string[];
        rows: Record<string, any>[];
        row_count: number;
        extraction_note: string | null;
      }[];
    }>(`/api/berthing/documents/${documentId}/full-view`),
  /** Original source PDF for a verbatim berthing document (opens inline). */
  berthingDocumentPdfUrl: (documentId: number) => `/api/berthing/documents/${documentId}/pdf`,
  berthingOpenSourcePdf: async (documentId: number, filename?: string) => {
    const token = getToken();
    const authHeader: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
    const res = await fetch(`/api/berthing/documents/${documentId}/pdf`, {
      headers: { ...authHeader, "x-data-mode": getDataSourceMode() },
      signal: timeoutSignal(UPLOAD_TIMEOUT_MS),
    });
    if (!res.ok) {
      let detail: any;
      try {
        detail = await res.json();
      } catch {
        /* ignore */
      }
      throw new Error(
        `${res.status} ${res.statusText}${detail ? ` — ${JSON.stringify(detail)}` : ""}`,
      );
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    // Prefer a new tab (inline disposition) so the evaluator can compare side-by-side.
    const w = window.open(url, "_blank", "noopener,noreferrer");
    if (!w) {
      // Popup blocked — fall back to a download.
      const a = document.createElement("a");
      a.href = url;
      a.download = filename || `berthing-${documentId}.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    }
    // Revoke after the browser has had time to load the blob URL.
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  },

  // --- Transporters & Drivers Data Upload (UC-III sub-module) — mirrors the cfs-ecy helpers ---
  tdUploadDownloadTemplate: (entity: string) =>
    downloadFile(
      `/api/td-upload/templates/${entity}`,
      `${entity.toLowerCase()}_upload_template.csv`,
    ),
  tdUploadValidate: (entity: string, file: File) => {
    const f = new FormData();
    f.append("file", file);
    f.append("entity", entity);
    return postForm<any>("/api/td-upload/validate", f);
  },
  tdUpload: (entity: string, file: File) => {
    const f = new FormData();
    f.append("file", file);
    f.append("entity", entity);
    return postForm<any>("/api/td-upload/upload", f);
  },
  tdUploads: (params?: { entity?: string; status?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && qs.set(k, String(v)));
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/td-upload/uploads${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  tdUploadDetail: (fileId: number) => http<any>(`/api/td-upload/uploads/${fileId}`),

  // --- Performance & Daily Reports (module 12, read-only) ---
  perfTerminals: () => http<{ items: any[]; count: number }>(`/api/performance/terminals`),
  perfMeta: () =>
    http<{ report_dates: string[]; latest_report_date: string | null; ldb_months: string[] }>(
      `/api/performance/meta`,
    ),
  // --- Performance Data Upload (module 12 sub-module, admin-only) ---
  perfDownloadTemplate: (reportType: string) =>
    downloadFile(`/api/performance/templates/${reportType}`, `${reportType}_template.csv`),
  perfUploadValidate: (reportType: string, file: File) => {
    const f = new FormData();
    f.append("report_type", reportType);
    f.append("file", file);
    return postForm<any>(`/api/performance/validate`, f);
  },
  perfUploadImport: (reportType: string, file: File) => {
    const f = new FormData();
    f.append("report_type", reportType);
    f.append("file", file);
    return postForm<any>(`/api/performance/upload`, f);
  },
  perfUploads: (params?: {
    report_type?: string;
    status?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/performance/uploads${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  perfKpi: (date?: string) => {
    const qs = new URLSearchParams();
    if (date) qs.set("date", date);
    return http<{
      report_date: string;
      prev_report_date: string | null;
      metrics: Record<string, number | null>;
      deltas: Record<string, number>;
    }>(`/api/performance/kpi${qs.toString() ? `?${qs}` : ""}`);
  },
  perfDaily: (date: string) => http<any>(`/api/performance/daily?date=${encodeURIComponent(date)}`),
  perfTraffic: (params?: {
    from?: string;
    to?: string;
    terminal?: string;
    period?: string;
    sort?: string;
    direction?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/performance/daily/traffic${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  perfStatus: (params?: { date?: string; terminal?: string; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/performance/daily/status${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  perfVessels: (params?: { date?: string; terminal?: string; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/performance/daily/vessels${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  perfMonthly: (params?: {
    fiscal_year?: string;
    terminal?: string;
    from?: string;
    to?: string;
    sort?: string;
    direction?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/performance/monthly-teu${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  perfTrends: (params?: {
    metric?: string;
    grain?: string;
    terminal?: string;
    from?: string;
    to?: string;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{
      metric: string;
      grain: string;
      terminal: string | null;
      count: number;
      series: { t: string; terminal_code: string; value: number | null }[];
    }>(`/api/performance/trends${qs.toString() ? `?${qs}` : ""}`);
  },
  perfStats: (params?: { from?: string; to?: string }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{
      days: number;
      latest_kpi: any;
      daily: {
        day: string;
        total_teus: number | null;
        gate_in_teus: number | null;
        gate_out_teus: number | null;
        yard_occupancy_pct: number | null;
      }[];
    }>(`/api/performance/stats${qs.toString() ? `?${qs}` : ""}`);
  },
  perfDwell: (params?: { month?: string; terminal?: string; cycle?: string; segment?: string }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; count: number }>(
      `/api/performance/dwell${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  perfCfsIcd: (params?: {
    month?: string;
    facility_type?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/performance/cfs-icd${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  perfCongestion: (params?: { month?: string; cycle?: string }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; count: number }>(
      `/api/performance/congestion${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  perfRoutes: (params?: { month?: string; cycle?: string; transport_mode?: string }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; count: number }>(
      `/api/performance/routes${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  perfWeather: (params?: { month?: string; terminal?: string; cycle?: string }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; count: number }>(
      `/api/performance/weather${qs.toString() ? `?${qs}` : ""}`,
    );
  },

  // --- Camera AI (Features 3/4/5) ---
  cameraCounts: (params?: { camera_id?: string; gate_id?: string; limit?: number }) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    return http<{ count: number; counts: any[] }>(
      `/api/camera-ai/counts${q.toString() ? `?${q}` : ""}`,
    );
  },
  cameraSummary: () => http<any>("/api/camera-ai/summary"),
  cameraDashboard: () => http<any>("/api/camera-ai/dashboard"),
  cameraTrailers: (limit = 100) =>
    http<{ count: number; trailers: any[] }>(`/api/camera-ai/trailer?limit=${limit}`),
  cameraContainers: (limit = 100) =>
    http<{ count: number; containers: any[] }>(`/api/camera-ai/container?limit=${limit}`),
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
    return http<{ count: number; documents: any[] }>(
      `/api/ocr/documents${q.toString() ? `?${q}` : ""}`,
    );
  },
  ocrDocument: (id: number) => http<any>(`/api/ocr/documents/${id}`),
  ocrHealth: () => http<import("./types").OcrHealth>("/api/ocr/health"),
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
    http<any>(`/api/nvr/devices/${encodeURIComponent(id)}/channels`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // --- ECY TRT (Feature 8) ---
  trtSummary: () => http<any>("/api/trt/summary"),
  trtRecords: (params?: { status?: string; vehicle_id?: string; limit?: number }) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    return http<{ count: number; records: any[] }>(
      `/api/trt/records${q.toString() ? `?${q}` : ""}`,
    );
  },
  trtPhase: (body: Record<string, any>) =>
    http<any>("/api/trt/phase", { method: "POST", body: JSON.stringify(body) }),

  // --- Bottlenecks (Feature 9) ---
  bottlenecks: (top = 3) => http<any>(`/api/bottlenecks?top=${top}`),
  bottleneckSnapshot: () => http<any>("/api/bottlenecks/snapshot", { method: "POST" }),
  bottleneckHistory: (limit = 100) =>
    http<{ count: number; snapshots: any[] }>(`/api/bottlenecks/history?limit=${limit}`),

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
  ldbContainer: (no: string) =>
    http<any>(`/api/ldb/container/${encodeURIComponent(no)}`, undefined, LDB_TIMEOUT_MS),
  ldbMovements: (no: string) =>
    http<any>(`/api/ldb/container/${encodeURIComponent(no)}/movements`, undefined, LDB_TIMEOUT_MS),
  ldbTruck: (vehicleNumber: string) =>
    http<{
      source: string;
      tracking: {
        truckNumber: string;
        truckType?: string;
        alert?: string | null;
        latest?: any;
        events: Array<{
          eventName?: string;
          locName?: string;
          containerNumber?: string;
          eventTime?: string | number;
          eventTimeLabel?: string;
          dateMarker?: string;
          transportMode?: string;
          locLat?: string;
          locLong?: string;
        }>;
        terminals: Array<{ locName: string; events: any[] }>;
      };
    }>(`/api/ldb/truck/${encodeURIComponent(vehicleNumber)}`),
  ldbHealth: () => http<any>("/api/ldb/health"),
  rmsSlots: (params?: { gate_id?: string; date?: string }) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    return http<{ count: number; slots: any[] }>(
      `/api/rms-tas/slots${q.toString() ? `?${q}` : ""}`,
    );
  },
  rmsHealth: () => http<any>("/api/rms-tas/health"),

  // --- Weather (Open-Meteo weather + marine, LIVE→CACHED→SYNTHETIC) ---
  // Coordinates default to the configured JNPA port location on the backend, so
  // callers normally pass no params. The endpoint degrades instead of failing:
  // read `status` / `source` / `decision_path` for provenance.
  weatherCurrent: (params?: { latitude?: number; longitude?: number; forecast_hours?: number }) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    return http<import("./types").WeatherCurrent>(
      `/api/weather/current${q.toString() ? `?${q}` : ""}`,
    );
  },
  weatherHealth: () => http<import("./types").WeatherHealth>("/api/weather/health"),

  // --- Traffic (TomTom flow + incidents, LIVE→CACHED→DATABASE→SYNTHETIC) ---
  // Coordinates default to the configured JNPA port location on the backend, so
  // callers normally pass no params. The endpoint degrades instead of failing:
  // read `status` / `source` / `decision_path` for provenance. The TomTom key
  // stays backend-only — the browser only ever talks to the gateway.
  trafficCurrent: (params?: { latitude?: number; longitude?: number }) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    return http<import("./types").TrafficCurrent>(
      `/api/traffic/current${q.toString() ? `?${q}` : ""}`,
    );
  },
  trafficHealth: () => http<import("./types").TrafficHealth>("/api/traffic/health"),

  // --- Air quality (OpenAQ, LIVE→CACHED→DATABASE→SYNTHETIC) ---
  // Coordinates default to the configured JNPA port location on the backend, so
  // callers normally pass no params. The endpoint degrades instead of failing:
  // read `status` / `source` / `decision_path` for provenance. The browser
  // only ever talks to the gateway — never to api.openaq.org.
  airQualityCurrent: (params?: { latitude?: number; longitude?: number }) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    return http<import("./types").AirQualityCurrent>(
      `/api/air-quality/current${q.toString() ? `?${q}` : ""}`,
    );
  },
  airQualityHealth: () => http<import("./types").AirQualityHealth>("/api/air-quality/health"),

  // --- Logistics (ULIP, LIVE→CACHED→DATABASE→FALLBACK) ---
  // The endpoints degrade instead of failing: read `status` / `source` /
  // `decision_path` for provenance. The FALLBACK rung is explicitly empty
  // (data_available: false) — the surface never fabricates shipment data.
  // The browser only ever talks to the gateway — never to the ULIP platform.
  logisticsCurrent: () => http<import("./types").LogisticsCurrent>("/api/logistics/current"),
  // A container reference resolves through ULIP LDB/01, so this shares the
  // wider LDB budget; a vehicle reference goes to FASTAG/01 and returns in
  // well under a second either way.
  logisticsTracking: (refId: string) =>
    http<import("./types").LogisticsTracking>(
      `/api/logistics/tracking/${encodeURIComponent(refId)}`,
      undefined,
      LDB_TIMEOUT_MS,
    ),
  logisticsEvents: (params?: {
    ref_id?: string;
    event_type?: string;
    limit?: number;
    offset?: number;
  }) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    return http<import("./types").LogisticsEventsPage>(
      `/api/logistics/events${q.toString() ? `?${q}` : ""}`,
    );
  },
  logisticsHealth: () => http<import("./types").LogisticsHealth>("/api/logistics/health"),

  // --- Bhuvan WMS (ISRO/NRSC geospatial layer, control-plane only) ---
  // The gateway never proxies imagery: /layers returns the WMS endpoint +
  // named layers (validated server-side via GetCapabilities) and the ArcGIS
  // WMSLayer renders GetMap tiles from the Bhuvan server directly. The raw
  // answer is validated by map/bhuvan.parseBhuvanConfig before use.
  bhuvanHealth: () => http<import("@/map/bhuvan").BhuvanHealth>("/api/bhuvan/health"),
  bhuvanLayers: () => http<unknown>("/api/bhuvan/layers"),
  rmsSeed: (body: Record<string, any>) =>
    http<any>("/api/rms-tas/seed", { method: "POST", body: JSON.stringify(body) }),
  rmsBook: (body: Record<string, any>) =>
    http<any>("/api/rms-tas/book", { method: "POST", body: JSON.stringify(body) }),

  // --- TT Double Trip (Feature 15) ---
  doubleTripStatistics: () => http<any>("/api/double-trip/statistics"),
  doubleTripCycles: (params?: { vehicle_id?: string; limit?: number }) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    return http<{ count: number; cycles: any[] }>(
      `/api/double-trip/cycles${q.toString() ? `?${q}` : ""}`,
    );
  },
  doubleTripStart: (body: Record<string, any>) =>
    http<any>("/api/double-trip/start", { method: "POST", body: JSON.stringify(body) }),
  doubleTripComplete: (tripId: number) =>
    http<any>(`/api/double-trip/${tripId}/complete`, { method: "POST" }),

  // --- Shipping Lines (module 4: IAL/EAL advance lists + EDO delivery orders) ---
  // Fully server-driven: every filter, search and page is resolved by the backend
  // (GET /api/shipping-lines) so they span the entire dataset, not a loaded page.
  shippingLinesSummary: () => http<any>("/api/shipping-lines/summary"),
  shippingLinesList: (params?: {
    list_type?: string;
    terminal?: string;
    category?: string;
    freight_kind?: string;
    shipping_line?: string;
    container?: string;
    bl?: string;
    q?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/shipping-lines${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  shippingLinesContainer: (containerNo: string) =>
    http<{ container_no: string; summary: any; advance_lists: any[]; delivery_orders: any[] }>(
      `/api/shipping-lines/container/${encodeURIComponent(containerNo)}`,
    ),
  shippingLinesByBl: (bl: string, params?: { limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && qs.set(k, String(v)));
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/shipping-lines/bl/${encodeURIComponent(bl)}${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  shippingLinesLines: (params?: { limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && qs.set(k, String(v)));
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/shipping-lines/lines${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  shippingLinesDeliveryOrders: (params?: {
    container?: string;
    vehicle?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/shipping-lines/delivery-orders${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  shippingLinesMessages: (params?: {
    list_type?: string;
    terminal?: string;
    import_status?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(
      ([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)),
    );
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/shipping-lines/messages${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  // Cargo enrichment (module-4 soft link): shipping-line facts for one cargo container.
  // 404s when the container is not a cargo record; callers fall back to
  // shippingLinesContainer for a non-cargo container-detail view.
  cargoShippingLine: (containerNo: string) =>
    http<{
      container_number: string;
      shipping_line: any;
      advance_lists: any[];
      delivery_orders: any[];
    }>(`/api/cargo/${encodeURIComponent(containerNo)}/shipping-line`),

  // --- Shipping Lines Data Upload (module 4 sub-module) — mirrors the perf upload helpers ---
  shippingLinesDownloadTemplate: (listType: string) =>
    downloadFile(
      `/api/shipping-lines/templates/${listType}`,
      `shipping_lines_${listType}_template.csv`,
    ),
  shippingLinesUploadValidate: (listType: string, file: File) => {
    const f = new FormData();
    f.append("file", file);
    f.append("list_type", listType);
    return postForm<any>("/api/shipping-lines/validate", f);
  },
  shippingLinesUpload: (listType: string, file: File) => {
    const f = new FormData();
    f.append("file", file);
    f.append("list_type", listType);
    return postForm<any>("/api/shipping-lines/upload", f);
  },
  shippingLinesUploads: (params?: { list_type?: string; status?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => v !== undefined && qs.set(k, String(v)));
    return http<{ items: any[]; total: number; limit: number; offset: number; count: number }>(
      `/api/shipping-lines/uploads${qs.toString() ? `?${qs}` : ""}`,
    );
  },
  shippingLinesUploadDetail: (fileId: number) => http<any>(`/api/shipping-lines/uploads/${fileId}`),

  // --- UC-3 Cargo What-If simulation (JNPA Notice 05 Aug 2026) ---
  // Read-only analytical layer: POST is used because the parameter set is a body,
  // NOT because anything is mutated. Every response carries the Notice §1
  // contract (method / result+figures / assumptions / queries), so the UI renders
  // the evidence beside the answer rather than the answer alone.
  simulateScenarios: () => http<SimScenarioCatalog>("/api/cargo/simulate/scenarios"),
  simulate: (scenario: string, body: Record<string, unknown>) =>
    http<SimulationResult>(`/api/cargo/simulate/${scenario}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  gateHourlyProfile: (params: {
    from: string;
    to: string;
    terminal?: string;
    gate_id?: string;
    group_by?: "hour" | "day";
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)));
    return http<GateHourlyProfile>(`/api/gate/hourly-profile?${qs}`);
  },

  // --- UC3-021 Gate & Lane Board -------------------------------------------
  // The queue on these cards is COUNTED by video analytics; the gateway returns
  // queue_vehicles: null (queue_status "NO_OBSERVATION") when no camera has seen
  // the gate, rather than substituting a throughput estimate.
  gateBoard: (windowMinutes = 60) =>
    http<import("./types").GateBoardResponse>(
      `/api/gate-board/gates?window_minutes=${windowMinutes}`,
    ),
  gateBoardLanes: (gateId?: string) =>
    http<{ lanes: import("./types").GateLane[]; count: number }>(
      `/api/gate-board/lanes${gateId ? `?gate_id=${encodeURIComponent(gateId)}` : ""}`,
    ),
  gateBoardTicker: (limit = 25) =>
    http<{ confirmations: import("./types").GateConfirmation[]; count: number }>(
      `/api/gate-board/ticker?limit=${limit}`,
    ),
  /** Impact simulation only — writes nothing, commands nothing. */
  laneReassignPreview: (laneId: string, toLaneType: string) =>
    http<import("./types").LaneReassignPreview>(
      `/api/gate-board/lanes/${encodeURIComponent(laneId)}/preview`,
      { method: "POST", body: JSON.stringify({ to_lane_type: toLaneType }) },
    ),
  /** Raises a task for the gate supervisor. Never actuates gate equipment. */
  laneReassignApply: (laneId: string, toLaneType: string, reason?: string) =>
    http<{
      task: import("./types").LaneReassignTask;
      preview: import("./types").LaneReassignPreview;
      lane_state_changed: false;
      sends_equipment_command: false;
      note: string;
    }>(`/api/gate-board/lanes/${encodeURIComponent(laneId)}/reassign`, {
      method: "POST",
      body: JSON.stringify({ to_lane_type: toLaneType, reason }),
    }),
  laneTasks: (status?: string, limit = 50) =>
    http<{ tasks: import("./types").LaneReassignTask[]; count: number }>(
      `/api/gate-board/tasks?limit=${limit}${status ? `&status=${status}` : ""}`,
    ),
  laneTaskAck: (taskId: string) =>
    http<import("./types").LaneReassignTask>(
      `/api/gate-board/tasks/${encodeURIComponent(taskId)}/ack`,
      { method: "POST" },
    ),

  // --- UC3-020 corridor congestion heatmap ---------------------------------
  // offset_minutes is the slider: -360 … 0 … +120. The server clamps it and says
  // so, so a caller cannot get a 12-hour forecast dressed as a 15-minute one.
  corridorHeatmap: (offsetMinutes = 0) =>
    http<import("./types").CorridorHeatmapResponse>(
      `/api/corridor-heatmap?offset_minutes=${offsetMinutes}`,
    ),

  // --- UC3-023 camera degraded mode (EC-6) ---------------------------------
  // Reads each camera's ACTUAL cascade rung, including a rung forced through
  // /api/control/fault — never a UI-only status.
  gateDegradedMode: () =>
    http<import("./types").DegradedModeResponse>("/api/gate-board/degraded-mode"),
  /** The project's existing fault console; the drill uses it, not a new mechanism. */
  injectFault: (domain: string, rung: string) =>
    http<Record<string, unknown>>(`/api/control/fault/${domain}`, {
      method: "POST",
      body: JSON.stringify({ rung }),
    }),
  clearFault: (domain: string) =>
    http<{ cleared: string; reconciliation?: Record<string, unknown> }>(
      `/api/control/fault/${domain}`,
      { method: "DELETE" },
    ),

  // --- UC3-027 CPP metered release (flow F-06) -----------------------------
  cppBoard: () => http<import("./types").CppBoardResponse>("/api/cpp/board"),
  /** METERED throttles only the congested terminal; UNIFORM is the do-nothing arm. */
  cppRecompute: (mode: "METERED" | "UNIFORM" = "METERED", persist = true) =>
    http<{
      mode: string;
      plans: import("./types").CppReleasePlan[];
      count: number;
      persisted: number;
      recompute_budget_seconds: number;
      simulated: boolean;
      note: string;
    }>("/api/cpp/release/recompute", {
      method: "POST",
      body: JSON.stringify({ mode, persist }),
    }),
  cppAdvice: (terminal?: string) =>
    http<{
      advice: Array<{
        terminal_code: string;
        text: string;
        hold_minutes: number;
        gate_queue_vehicles: number;
        clearing_rate_vph: number;
        simulated: boolean;
      }>;
      count: number;
    }>(`/api/cpp/advice${terminal ? `?terminal=${encodeURIComponent(terminal)}` : ""}`),

  // --- UC3-040 Auto-LEO four-way join --------------------------------------
  autoLeoBoard: (limit = 50, sourceMode?: string) =>
    http<import("./types").AutoLeoBoardResponse>(
      `/api/auto-leo/board?limit=${limit}${sourceMode ? `&source_mode=${sourceMode}` : ""}`,
    ),
  autoLeoContainer: (containerNo: string) =>
    http<import("./types").AutoLeoRow>(
      `/api/auto-leo/container/${encodeURIComponent(containerNo)}`,
    ),

  // --- UC3-024 trip resolver / UC3-025 visit timeline ----------------------
  // One box, five key kinds, one trip. The resolver never picks between
  // candidates: several matches come back as status "AMBIGUOUS".
  tripSearch: (q: string) =>
    http<import("./types").TripSearchResponse>(`/api/trip/search?q=${encodeURIComponent(q)}`),
  trip: (tripId: string) =>
    http<import("./types").TripDetail>(`/api/trip/${encodeURIComponent(tripId)}`),

  // --- UC3-035 dual turnaround definitions ---------------------------------
  // Returns BOTH arms; there is deliberately no parameter to ask for one
  // (UI-122: neither may be displayed alone anywhere in the product).
  kpiDualTat: () => http<import("./types").DualTatResponse>("/api/kpi/dual-tat"),
  /** Daily average, median, P90 and peak-hour ratio, all computed in the DB. */
  kpiDistribution: (windowHours = 24) =>
    http<import("./types").KpiDistributionResponse>(
      `/api/kpi/distribution?window_hours=${windowHours}`,
    ),

  // --- UC3-028 violation queue / UC3-029 hash-chained audit ----------------
  // Filtering is server-side: a queue that only filters what it already
  // downloaded stops being correct past the first page.
  violationQueue: (opts: { status?: string; kind?: string; plate?: string } = {}) => {
    const q = new URLSearchParams();
    if (opts.status) q.set("status", opts.status);
    if (opts.kind) q.set("kind", opts.kind);
    if (opts.plate) q.set("plate", opts.plate);
    const qs = q.toString();
    return http<import("./types").ViolationQueueResponse>(
      `/api/violations/cases${qs ? `?${qs}` : ""}`,
    );
  },
  /** Fires every rung the dwell has earned (N/2N/3N). Idempotent per rung. */
  violationEscalate: (
    caseId: string,
    body: {
      dwell_minutes: number;
      zone_id?: string;
      n_minutes?: number;
    },
  ) =>
    http<import("./types").EscalateResult>(
      `/api/violations/cases/${encodeURIComponent(caseId)}/escalate`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  violationNotifications: (caseId: string) =>
    http<import("./types").CaseNotifications>(
      `/api/violations/cases/${encodeURIComponent(caseId)}/notifications`,
    ),
  violationFieldVerification: (caseId: string, zoneId?: string) =>
    http<{ task: import("./types").FieldVerificationTask; note: string }>(
      `/api/violations/cases/${encodeURIComponent(caseId)}/field-verification`,
      { method: "POST", body: JSON.stringify({ zone_id: zoneId }) },
    ),
  violationCase: (caseId: string) =>
    http<import("./types").ViolationCaseBundle>(
      `/api/violations/cases/${encodeURIComponent(caseId)}`,
    ),
  /** Recomputes the append-only chain server-side; reports the first broken link. */
  violationVerifyChain: (caseId: string) =>
    http<import("./types").ChainVerification>(
      `/api/violations/cases/${encodeURIComponent(caseId)}/verify-chain`,
    ),
  /** Illegal hops are rejected by the server with 409 — the error is surfaced. */
  violationTransition: (caseId: string, toStatus: string, paymentRef?: string) =>
    http<Record<string, unknown>>(
      `/api/violations/cases/${encodeURIComponent(caseId)}/transition`,
      { method: "POST", body: JSON.stringify({ to_status: toStatus, payment_ref: paymentRef }) },
    ),

  // --- UC3-036 carbon method + idle delta ----------------------------------
  carbonMethod: () => http<import("./types").CarbonMethodResponse>("/api/carbon/method"),
  carbonIdleDelta: (body: {
    scenario?: string;
    baseline_idle_minutes: number;
    scenario_idle_minutes: number;
    vehicle_class?: string;
  }) =>
    http<import("./types").CarbonIdleDelta>("/api/carbon/idle-delta", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // --- SecureVision (proxied vendor: /api/sv/*) ----------------------------
  // The browser NEVER talks to svapidev.phylon.in and never holds a vendor
  // token: SecureVision authenticates at /api/auth/login — the same relative
  // path this app's own sign-in uses — so the gateway owns that exchange and
  // exposes everything under /api/sv/* behind the EXISTING JNPA bearer.
  svHealth: () => http<import("./securevision").SvHealth>("/api/sv/health"),
  svCameraMap: () =>
    http<{
      configured: boolean;
      count: number;
      cameras: import("./securevision").SvCameraMapping[];
    }>("/api/sv/cameras"),
  svAnalyses: (limit = 50) =>
    http<import("./securevision").SvAnalysisList>(`/api/sv/analyses?limit=${limit}`),
  svUploadVideo: (file: File, cameraCode: string) => {
    const f = new FormData();
    f.append("file", file);
    f.append("camera_code", cameraCode);
    return postForm<import("./securevision").SvAnalysis>("/api/sv/analytics/video/upload", f);
  },
  /** One single-envelope analyzer: i01 | i02 | i09 | i12. */
  svIncident: (analysisId: string, code: "i01" | "i02" | "i09" | "i12", strong = false) =>
    http<import("./securevision").SvIncident>(
      `/api/sv/analytics/incident/${code}?analysis_id=${encodeURIComponent(analysisId)}&strong=${strong}`,
    ),
  /** I-07 answers one verdict per person, so it has its own shape. */
  svIncidentPersons: (analysisId: string) =>
    http<import("./securevision").SvPersonResult>(
      `/api/sv/analytics/incident/i07?analysis_id=${encodeURIComponent(analysisId)}`,
    ),
  svIncidentAll: (analysisId: string, strong = false) =>
    http<import("./securevision").SvCombinedReport>(
      `/api/sv/analytics/incident/all?analysis_id=${encodeURIComponent(analysisId)}&strong=${strong}`,
    ),
  svDeleteAnalysis: (analysisId: string) =>
    http<void>(`/api/sv/analytics/video/${encodeURIComponent(analysisId)}`, { method: "DELETE" }),
  /** Mints the short-lived credential the MJPEG <img> carries in its URL — an
   *  <img> cannot send an Authorization header (same reason /api/ws takes a
   *  ?token=). The vendor's own token never leaves the gateway. */
  svStreamTicket: (analysisId: string) =>
    http<import("./securevision").SvStreamTicket>(
      `/api/sv/analytics/video/${encodeURIComponent(analysisId)}/stream-ticket`,
      { method: "POST" },
    ),
  svFaces: () =>
    http<{ persons: import("./securevision").SvPerson[]; count: number }>("/api/sv/faces"),
  svFaceEvents: (params?: { limit?: number; authorized?: boolean }) => {
    const q = new URLSearchParams();
    if (params?.limit != null) q.set("limit", String(params.limit));
    if (params?.authorized != null) q.set("authorized", String(params.authorized));
    const qs = q.toString();
    return http<{ events: import("./securevision").SvFaceEvent[]; count: number }>(
      `/api/sv/faces/events${qs ? `?${qs}` : ""}`,
    );
  },
  svFaceStatus: () => http<import("./securevision").SvFaceModelStatus>("/api/sv/faces/status"),
  svEnrollFace: (input: import("./securevision").SvEnrollInput) => {
    const f = new FormData();
    f.append("person_id", input.person_id);
    f.append("name", input.name);
    if (input.role) f.append("role", input.role);
    if (input.department) f.append("department", input.department);
    // Repeated "files" parts: SecureVision averages several photos into one
    // more robust embedding.
    input.photos.forEach((photo, i) => f.append("files", photo, `face_${i + 1}.jpg`));
    return postForm<import("./securevision").SvPerson>("/api/sv/faces", f);
  },
  svUpdateFace: (
    personPk: number,
    patch: { name?: string; role?: string; department?: string; is_active?: boolean },
  ) =>
    http<import("./securevision").SvPerson>(`/api/sv/faces/${personPk}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  svDeleteFace: (personPk: number) => http<void>(`/api/sv/faces/${personPk}`, { method: "DELETE" }),
};

// --- What-If simulation types ------------------------------------------------
// Mirror services/cargo/simulation/base.py::SimulationResult exactly. Kept in one
// place so a backend contract change surfaces as a TypeScript error rather than
// as an undefined at render time.

/** Where a value came from. The distinction JNPA Notice §1.c asks to be declared:
 *  ASSUMED means "the data does not carry this, here is what we used and why". */
export type SimAssumptionSource = "MEASURED" | "DERIVED" | "ASSUMED" | "PARAMETER";

export interface SimAssumption {
  field: string;
  value: unknown;
  reason: string;
  source: SimAssumptionSource;
}

/** One query the answer rests on — Notice §1.d ("so the working can be traced").
 *  `error` is set when the query FAILED rather than returned nothing; the two are
 *  indistinguishable by row count and must never be conflated in the UI. */
export interface SimQueryTrace {
  purpose: string;
  sql: string;
  params: Record<string, unknown>;
  api?: string;
  row_count?: number;
  error?: string;
}

export interface SimRecommendation {
  action: string;
  reason: string;
  [detail: string]: unknown;
}

export interface SimulationResult {
  scenario: string;
  method: string;
  result: Record<string, any>;
  // Booleans are part of this contract: channel-closure reports
  // `berth_lock_reached` and modal-shift `gate_absorbs_load` as figures.
  figures: Record<string, number | string | boolean | null>;
  assumptions: SimAssumption[];
  queries: SimQueryTrace[];
  recommendations: SimRecommendation[];
  /** False when a required input table was empty or a query failed. The UI must
   *  render this as a first-class state with `notes`, never as a blank panel. */
  data_available: boolean;
  notes: string[];
}

export interface SimScenarioEntry {
  scenario: string;
  jnpa_reference: string;
  question: string;
  required: string[];
  optional: string[];
  reads: string[];
}

export interface SimScenarioCatalog {
  count: number;
  scenarios: SimScenarioEntry[];
  contract: Record<string, string>;
}

export interface GateHourlyBucket {
  bucket: string;
  arrivals: number;
  completed?: number;
  unique_trucks?: number;
  avg_tat_min?: number | null;
}

export interface GateHourlyProfile {
  window: { from: string; to: string };
  group_by: string;
  /** Which table answered: 'core.eir', 'core.gate_event' or 'NONE'. */
  source: string;
  count: number;
  total_arrivals: number;
  peak_bucket: string | null;
  peak_arrivals: number;
  mean_per_bucket: number;
  buckets: GateHourlyBucket[];
  notes: string[];
  assumptions: SimAssumption[];
  queries: SimQueryTrace[];
}

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

// ===================================================================== UC-III
// Types for the UC-III lifecycle surface (gate documents, job spine, ECY→CFS
// chains). Kept next to the helpers above so the screens import one module.

export interface EcyCfsChainLeg {
  seq: number;
  leg: string;
  label: string;
  ts: string | null;
  duration_hours?: number | null;
  present: boolean;
  note?: string;
}
export interface EcyCfsChain {
  id: number;
  container_number: string;
  ecy_out_ts: string | null;
  cfs_in_ts: string | null;
  cfs_out_ts: string | null;
  ecy_in_ts: string | null;
  transit_hours: number | null;
  dwell_hours: number | null;
  cycle_hours: number | null;
  chain_status: "COMPLETE" | "PARTIAL" | "ORPHAN";
  legs_present: number;
  event_count: number;
  has_anomaly: boolean;
  anomaly_codes: string[];
  anomaly_labels?: string[];
  anomaly_detail: Record<string, unknown>;
  legs?: EcyCfsChainLeg[];
}
export interface EcyCfsChainStats {
  chains: number;
  complete_chains: number;
  partial_chains: number;
  anomaly_chains: number;
  avg_transit_hours: number | null;
  avg_dwell_hours: number | null;
  avg_cycle_hours: number | null;
  median_cycle_hours: number | null;
  by_anomaly: { code: string; chains: number }[];
  anomaly_labels: Record<string, string>;
  last_rebuilt_at: string | null;
}

export interface GateDocSummary {
  eir: number;
  pin_tickets: number;
  pin_legs: number;
  dual_move_tickets: number;
  form13: number;
  containerless_docs: number;
  eir_with_tat: number;
  files: number;
}
/** One REAL gate document as filed, from `core.gate_document` (UC3-002).
 *  A field the source slip does not print comes back null — never inferred. */
// ---- UC3-004 vehicle -> transporter registry (MIXED provenance) -------------
/** Gap G6: the supplied masters carry no plates, so a mapping is either read
 *  off a REAL gate document (DOCUMENT_EVIDENCED, with source_ref) or generated
 *  and labelled SYNTHETIC under assumption A-G6. Never both, never neither. */
export type VehicleProvenance = "DOCUMENT_EVIDENCED" | "SYNTHETIC";

export interface VehicleMapping {
  id: number;
  vehicle_no: string;
  vehicle_no_norm: string;
  driver_id: string | null;
  provenance: VehicleProvenance;
  /** Present only on SYNTHETIC rows (enforced by a DB CHECK). */
  assumption_ref: string | null;
  /** Present only on DOCUMENT_EVIDENCED rows: the gate document it came from. */
  source_ref: string | null;
  transporter_id: number;
  company_id: number;
  transporter: string;
  transporter_contact: string | null;
  is_synthetic: boolean;
  seed: string | null;
  assumption_text: string | null;
}
export interface VehicleMappingPage {
  items: VehicleMapping[];
  total: number;
  limit: number;
  offset: number;
  count: number;
}
export interface VehicleRegistrySummary {
  total: number;
  document_evidenced: number;
  synthetic: number;
  assumption_ref: string;
  assumption_text: string;
  seed: string;
}

// ---- UC3-005 NH-348 corridor simulation (SIMULATED only) --------------------
export interface CorridorSimSegment {
  segment_code: string;
  trucks: number;
  inbound: number;
  outbound: number;
}
export interface CorridorSimSummary {
  run: {
    run_id: string;
    corridor: string;
    seed: string;
    seed_version: string;
    config_sha256: string;
    truck_count: number;
    segment_count: number;
    calibration_from: string;
    calibration_to: string;
    anchor_date: string;
    anchor_in_teu: number;
    anchor_out_teu: number;
    anchor_total_teu: number;
    calibration_note: string | null;
    frozen_at: string;
    simulated: boolean;
  };
  simulated: boolean;
  provenance: string;
  trucks_total: number;
  inbound: number;
  outbound: number;
  segments: CorridorSimSegment[];
  segment_count: number;
  states: { state: string; trucks: number }[];
  calibration: {
    anchor_date: string;
    anchor_in_teu: number;
    anchor_out_teu: number;
    anchor_total_teu: number;
    window_from: string;
    window_to: string;
    note: string | null;
  };
  reproducibility: { seed: string; seed_version: string; config_sha256: string };
}
export interface CorridorSimTruck {
  truck_uid: string;
  truck_no: string;
  segment_code: string;
  direction: "IN" | "OUT";
  state: string;
  replay_ts: string;
  simulated: boolean;
  provenance: string;
}
export interface CorridorSimTruckPage {
  items: CorridorSimTruck[];
  count: number;
  limit: number;
  offset: number;
  simulated: boolean;
  provenance: string;
}

export interface GateSourceDoc {
  doc_id: number;
  doc_category: "EIR" | "FORM13" | "PIN_TICKET";
  doc_variant: string;
  doc_ref: string | null;
  pin_no: string | null;
  visit_id: string | null;
  doc_ts: string | null;
  container_no: string | null;
  iso_code: string | null;
  load_status: string | null;
  gross_weight_kg: number | null;
  seal1: string | null;
  seal2: string | null;
  vehicle_no: string | null;
  bat_no: string | null;
  driver_name: string | null;
  driver_licence: string | null;
  transporter_name: string | null;
  truck_in_ts: string | null;
  truck_out_ts: string | null;
  gate_no: string | null;
  yard_position: string | null;
  vessel_name: string | null;
  voyage: string | null;
  pol: string | null;
  pod: string | null;
  booking_no: string | null;
  cfs: string | null;
  group_code: string | null;
  /** Verbatim parsed payload, exactly as the source file supplies it. */
  attrs: Record<string, unknown> | null;
  terminal: string | null;
  /** Same-origin URL of the original scan, or null when none is linked. */
  evidence_uri: string | null;
  image_file: string | null;
  data_origin: string | null;
}
export interface GateSourceDocPage {
  items: GateSourceDoc[];
  total: number;
  limit: number;
  offset: number;
  count: number;
  terminals: string[];
  terminal_count: number;
  first_doc_ts: string | null;
  last_doc_ts: string | null;
}
export interface GateDocBundle {
  container_no?: string;
  truck_no?: string;
  eir: any[];
  pin: any[];
  form13: any[];
  total: number;
  terminals?: string[];
  tat_samples?: {
    eir_no: string | null;
    terminal: string | null;
    container_number: string | null;
    truck_in_time: string | null;
    truck_out_time: string | null;
    tat_minutes: number | null;
  }[];
}
export interface GateDocTat {
  samples: number;
  avg_tat_min: number | null;
  median_tat_min: number | null;
  min_tat_min: number | null;
  max_tat_min: number | null;
  source: string;
  by_terminal: { terminal: string; samples: number; avg_tat_min: number }[];
}

export type JobStatus =
  | "PENDING_ASSIGNMENT"
  | "ASSIGNED"
  | "ACCEPTED"
  | "AT_GATE"
  | "IN_YARD"
  | "PICKED_UP"
  | "DROPPED"
  | "COMPLETED"
  | "CANCELLED";
export interface ContainerJob {
  id: number;
  container_number: string | null;
  group_code: string | null;
  transporter_id: number | null;
  vehicle_id: string;
  vehicle_no: string | null;
  driver_id: string | null;
  driver_licence: string | null;
  move_type: string;
  document_type: string | null;
  document_reference: string | null;
  terminal: string | null;
  gate: string | null;
  status: JobStatus;
  assigned_by: string | null;
  assigned_at: string;
  accepted_at: string | null;
  completed_at: string | null;
  cancelled_reason: string | null;
  notes: string | null;
  /** Never true on a dispatched job — the discriminant against PendingHandoverEntry. */
  pending_handover?: false;
}
// A container UC-II has RELEASED that no truck has been dispatched against yet
// (GET /api/jobs?include_pending=true, GET /api/jobs/pending-handover). It is a
// queue entry, NOT a job: there is no job row behind it, so `id` and
// `vehicle_id` are null and the status is one core.container_job_assignment
// would reject. It is the INPUT to POST /api/jobs — a click on one belongs in
// the assignment panel, never in the job stepper.
export interface PendingHandoverEntry extends Omit<
  ContainerJob,
  "id" | "vehicle_id" | "pending_handover"
> {
  id: null;
  vehicle_id: null;
  pending_handover: true;
  lifecycle_status: string | null;
  customs_status: string | null;
  yard_block: string | null;
  vessel_name: string | null;
  released_at: string | null;
}
/** What GET /api/jobs returns when include_pending is on: jobs + queue entries. */
export type JobListItem = ContainerJob | PendingHandoverEntry;
export interface JobEvent {
  id: number;
  event: string;
  old_status: string | null;
  new_status: string | null;
  actor: string | null;
  actor_role: string | null;
  detail: Record<string, unknown>;
  created_at: string;
}
export interface JobCheck {
  check: string;
  ok: boolean;
  detail: string;
  [k: string]: unknown;
}
// An enrolled driver from core.driver_identity (GET /api/identity/drivers).
// `license_no` is what the PDP permit check is resolved through, so a driver
// without one can never clear validation.
export interface ActiveDriver {
  driver_id: string;
  name?: string | null;
  license_no?: string | null;
  photo_url?: string | null;
}
export interface JobAssignInput {
  container_number?: string;
  group_code?: string;
  vehicle_id?: string;
  vehicle_no?: string;
  driver_id?: string;
  driver_licence?: string;
  move_type: string;
  document_type?: string;
  document_reference?: string;
  terminal?: string;
  gate?: string;
  notes?: string;
}

export interface ScannerMachine {
  machine_code: string;
  machine_class: "DRIVE_THROUGH" | "MOBILE" | "FIXED";
  machine_type: string | null;
  location_code: string | null;
  customs_house: string | null;
  terminal: string | null;
  lane: string | null;
  active: boolean;
}
export interface ScanStatus {
  container_number: string;
  scan_required: boolean;
  rms_selection: Record<string, unknown> | null;
  machine_code: string | null;
  machine_class: string | null;
  latest_scan: Record<string, unknown> | null;
  result: string | null;
  cleared: boolean;
  job_id: number | null;
}

// ---- UC3 Email Processing ------------------------------------------------
export type EmailStatus = "UNPROCESSED" | "PROCESSING" | "PROCESSED" | "FAILED" | "NEEDS_REVIEW";

/** Mailbox posture. Deliberately has NO password field — the server never sends one. */
export interface EmailHealth {
  connected: boolean;
  message: string;
  ledger?: boolean;
  mailbox?: {
    host: string;
    port: number;
    /** Masked, e.g. `o*****s@example.com`. */
    user: string;
    security: string;
    mailbox: string;
    subject_prefix: string;
    enabled: boolean;
    configured: boolean;
  };
}

export interface EmailMessage {
  id: number;
  message_id: string;
  subject: string | null;
  sender: string | null;
  recipients: string | null;
  cc: string | null;
  received_at: string | null;
  body_preview: string | null;
  attachment_count: number;
  processing_status: EmailStatus;
  detected_type: string | null;
  target_master_table: string | null;
  records_detected: number;
  records_imported: number;
  records_failed: number;
  error_detail: string | null;
  processed_at: string | null;
  processed_by: string | null;
}

export interface EmailAttachment {
  id: number;
  filename: string;
  content_type: string | null;
  size_bytes: number;
  detected_format: string | null;
  detected_document_type: string | null;
  target_master_table: string | null;
  process_status: string;
  records_detected: number;
  records_imported: number;
  records_failed: number;
  error_detail: string | null;
}

export interface EmailMessageDetail extends EmailMessage {
  body_text: string | null;
  attachments: EmailAttachment[];
  errors: {
    id: number;
    attachment_id: number | null;
    record_ref: string | null;
    error_code: string;
    error_detail: string | null;
  }[];
}

/** Outcome of a preview (`committed:false`) or an import (`committed:true`). */
export interface EmailProcessResult {
  ok: boolean;
  status: EmailStatus | "PREVIEWED";
  committed?: boolean;
  already_processed?: boolean;
  message: string;
  detected_type: string | null;
  target_master_table: string | null;
  /** Populated on NEEDS_REVIEW: the tables this content might belong to. */
  candidates?: string[];
  reason?: string;
  records_detected: number;
  records_imported: number;
  records_failed: number;
  attachments: {
    filename: string;
    size_bytes?: number;
    content_type?: string;
    detected_format: string | null;
    document_type: string | null;
    master_table: string | null;
    confident: boolean;
    reason_code?: string | null;
    reason?: string | null;
    candidates?: string[];
    status?: string;
    records_detected?: number;
    records_imported?: number;
    records_failed?: number;
    error?: string;
  }[];
  errors?: { record_ref?: string | null; error_code?: string; error_detail?: string | null }[];
}
