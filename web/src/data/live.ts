// LiveAdapter — talks to the gateway /api surface. The UI never imports this
// directly; it receives a DataAdapter from the selector in ./index.ts.

import { api } from "@/lib/api";
import { getToken } from "@/lib/auth";
import type {
  Alert,
  AutoLeoResult,
  CarbonRollup,
  DriverEnrollment,
  EmptyAllocation,
  FaultControlResult,
  FaultState,
  IdentityVerifyArg,
  IdentityVerifyResult,
  IdentityEnrolResult,
  KpiResult,
  ParkingFacility,
  ParkingSummary,
} from "@/lib/types";
import type { CongestionMetrics, DataAdapter, DataMode, OcrEval } from "./types";

// Attach the bearer token when a session exists (auth-enabled builds), mirroring
// lib/api.ts. When auth is disabled there is no token and the header is omitted.
function authHeaders(): Record<string, string> {
  const token = getToken();
  return {
    "content-type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: authHeaders() });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} (${path})`);
  return (await res.json()) as T;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} (${path})`);
  return (await res.json()) as T;
}

async function deleteJson<T>(path: string): Promise<T> {
  const res = await fetch(path, {
    method: "DELETE",
    headers: authHeaders(),
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
  downloadPolicePdf = (params?: any) => api.downloadPolicePdf(params);

  // --- vehicle violation detection ---
  violationCatalog = async () => (await api.violationCatalog()).violations;
  violationDetect = (image: Blob, gateId?: string) => api.violationDetect(image, gateId);
  violationCommit = (input: any) => api.violationCommit(input);
  violationEnforce = (
    image: Blob,
    opts?: { gateId?: string; zoneId?: string; violations?: string },
  ) => api.violationEnforce(image, opts);

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
  identityVerify = (
    driverId: string,
    arg?: "genuine" | "impostor" | "unknown" | IdentityVerifyArg,
  ): Promise<IdentityVerifyResult> => {
    const body = typeof arg === "string" ? { simulate: arg } : (arg ?? {});
    // is_synthetic=true + a lawful purpose satisfy the gateway DPDP guard (PoC
    // posture: consented/synthetic faces only).
    return postJson<IdentityVerifyResult>("/api/identity/verify", {
      driver_id: driverId,
      is_synthetic: true,
      purpose: "GATE_VERIFICATION",
      ...body,
    });
  };
  identityEnrol = (driverId: string, image: string): Promise<IdentityEnrolResult> =>
    postJson<IdentityEnrolResult>("/api/identity/enrol", {
      driver_id: driverId,
      image,
      is_synthetic: true,
      purpose: "ENROLMENT",
    });

  // --- Driver enrolment approval workflow ---
  enrollments = async (status?: string): Promise<DriverEnrollment[]> =>
    (
      await getJson<{ enrollments: DriverEnrollment[] }>(
        `/api/identity/enrollments${status ? `?status=${encodeURIComponent(status)}` : ""}`,
      )
    ).enrollments;
  enrollmentDetail = (driverId: string): Promise<DriverEnrollment> =>
    getJson<DriverEnrollment>(`/api/identity/enrollments/${encodeURIComponent(driverId)}`);
  approveEnrollment = (driverId: string) =>
    postJson<{ approved: boolean }>(
      `/api/identity/enrollments/${encodeURIComponent(driverId)}/approve`,
      {},
    );
  rejectEnrollment = (driverId: string, reason: string) =>
    postJson<{ rejected: boolean }>(
      `/api/identity/enrollments/${encodeURIComponent(driverId)}/reject`,
      { reason },
    );
  reenrollEnrollment = (driverId: string, reason?: string) =>
    postJson<{ reenroll: boolean }>(
      `/api/identity/enrollments/${encodeURIComponent(driverId)}/reenroll`,
      { reason: reason ?? "re-enrolment requested" },
    );
  parkingAvailability = async (minuteOfDay?: number): Promise<ParkingFacility[]> =>
    (
      await getJson<{ facilities: ParkingFacility[] }>(
        `/api/parking/availability${minuteOfDay != null ? `?minute_of_day=${minuteOfDay}` : ""}`,
      )
    ).facilities;
  parkingSummary = (minuteOfDay?: number): Promise<ParkingSummary> =>
    getJson<ParkingSummary>(
      `/api/parking/summary${minuteOfDay != null ? `?minute_of_day=${minuteOfDay}` : ""}`,
    );

  // --- Terminal Appointment System (TFC-1) ---
  tasSlots = async (gateId?: string) => (await api.tasSlots(gateId)).slots;

  // --- FASTag (ULIP) — /api/fastag/* ---
  fastagBalance = (rcNumber: string) => api.fastagBalance(rcNumber);
  fastagTransactions = (rcNumber: string) => api.fastagTransactions(rcNumber);
  tollEnroute = (body: import("@/lib/types").TollEnrouteInput) => api.tollEnroute(body);
  fastagHealth = () => api.fastagHealth();

  // --- Fault-injection control surface (Demo Console) --------------------
  // The three fallback chains the presenter can force a rung on. The gateway
  // recomputes severity + the operator banner on every force/clear.
  getFaults = (): Promise<FaultState> => getJson<FaultState>("/api/control/fault");
  forceFault = (domain: string, rung: string): Promise<FaultControlResult> =>
    postJson<FaultControlResult>(`/api/control/fault/${encodeURIComponent(domain)}`, { rung });
  clearFault = (domain?: string): Promise<FaultControlResult> =>
    deleteJson<FaultControlResult>(
      domain ? `/api/control/fault/${encodeURIComponent(domain)}` : "/api/control/fault",
    );

  // --- Realism probes ----------------------------------------------------
  // Both endpoints are optional on the gateway. Probe-don't-assume: on any
  // failure (404 / network) we degrade to null so the Demo Console shows the
  // static target/advisory note instead of erroring.
  ocrEval = async (): Promise<OcrEval | null> => {
    try {
      const d = await getJson<{
        clear_accuracy?: number;
        accuracy?: number;
        combined_weighted_accuracy_pct?: number;
        target_pct?: number;
        OCR_TARGET_MET?: boolean;
        degraded?: boolean;
      }>("/api/anpr/eval");
      // Prefer the explicit per-condition accuracy; fall back to the combined %.
      const acc =
        d.clear_accuracy ??
        d.accuracy ??
        (d.combined_weighted_accuracy_pct != null
          ? d.combined_weighted_accuracy_pct / 100
          : undefined);
      if (acc == null) return null;
      return {
        clear_accuracy: acc,
        target: d.target_pct != null ? d.target_pct / 100 : 0.95,
        target_met: d.OCR_TARGET_MET,
        degraded: d.degraded,
      };
    } catch {
      return null;
    }
  };
  congestionMetrics = async (): Promise<CongestionMetrics | null> => {
    for (const path of ["/api/traffic/metrics", "/api/congestion/metrics"]) {
      try {
        const d = await getJson<{ f1?: number; congestion_onset_f1?: number; target_f1?: number }>(
          path,
        );
        const f1 = d.f1 ?? d.congestion_onset_f1;
        if (f1 != null) {
          const target = d.target_f1 ?? 0.85;
          return { f1, target, target_met: f1 >= target };
        }
      } catch {
        /* try the next candidate, else fall through to null */
      }
    }
    return null;
  };
}
