// Thin fetch wrapper around the gateway's /api surface. The PWA always calls
// relative paths; the Vite dev proxy (dev) or the web/ nginx (prod, at /pwa)
// forwards to the gateway. Returns parsed JSON and throws on non-2xx.

import type { CorridorGeometry, Gate, TasSlot, TruckEnvelope, VahanEnvelope } from "./types";
import { getToken, setToken, tokenNeedsRefresh } from "./device";

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const res = await fetch(path, {
    headers: {
      "content-type": "application/json",
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...(init?.headers || {}),
    },
    ...init,
  });
  if (!res.ok) {
    let detail: any;
    try {
      detail = await res.json();
    } catch {
      /* non-json body */
    }
    const err = new Error(
      `${res.status} ${res.statusText}${detail ? ` — ${JSON.stringify(detail)}` : ""}`,
    );
    (err as any).status = res.status;
    throw err;
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// Build-time pairing secret. In a production deployment the gateway requires
// PWA_PAIRING_SECRET; the same value is injected into the bundle so the PWA can
// mint its DRIVER token at pairing. (This is the seam where a real OTP / device
// attestation flow would replace the shared secret post-award.)
const PAIRING_SECRET: string | undefined = import.meta.env.VITE_PWA_PAIRING_SECRET;

// POST a token-mint request; store + return true on a 2xx carrying access_token.
// Best-effort: a network error / non-2xx resolves to false (caller decides next).
// Logs the outcome so a failed mint is diagnosable in the field (it is otherwise
// invisible — the app would silently proceed unauthenticated).
async function mintToken(path: string, body: unknown): Promise<boolean> {
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res.ok) {
      const data = (await res.json()) as { access_token?: string };
      if (data.access_token) {
        setToken(data.access_token);
        return true;
      }
      console.error(`[auth] ${path} returned 200 but no access_token`);
      return false;
    }
    console.error(`[auth] ${path} -> ${res.status} ${res.statusText}`);
  } catch (err) {
    console.error(`[auth] ${path} request failed`, err);
  }
  return false;
}

// Mint a DRIVER-scoped JWT bound to this device. Public endpoint (no auth
// required). Best-effort: returns false rather than throwing so pairing never
// hard-fails the UI.
export async function ensureDeviceToken(deviceId: string): Promise<boolean> {
  if (!tokenNeedsRefresh()) return true;

  // Never POST without a device_id: JSON.stringify drops undefined-valued keys,
  // so a falsy deviceId would serialize to `{}` and the gateway answers 422
  // (DeviceTokenBody.device_id is required) — an otherwise invisible failure.
  const id = (deviceId ?? "").trim();
  if (!id) {
    console.error("[auth] ensureDeviceToken called without a device_id — skipping mint");
    return false;
  }

  // A production build MUST carry the pairing secret (Vite inlines
  // VITE_PWA_PAIRING_SECRET at build time). Without it /api/auth/device-token
  // returns 401 and there is no dev-token seam in prod — surface the misconfig
  // loudly instead of failing silently.
  if (import.meta.env.PROD && !PAIRING_SECRET) {
    console.error(
      "[auth] VITE_PWA_PAIRING_SECRET is missing from this production build — " +
        "/api/auth/device-token will 401. Rebuild the PWA with " +
        "VITE_PWA_PAIRING_SECRET set to the gateway's PWA_PAIRING_SECRET.",
    );
  }

  // Primary path (dev + prod): DRIVER-scoped device token.
  if (
    await mintToken("/api/auth/device-token", {
      device_id: id,
      pairing_secret: PAIRING_SECRET,
    })
  ) {
    return true;
  }

  // Dev-only fallback: the password-less seam 404s in any production-like env,
  // so never call it from a production bundle (keeps prod traffic off dev-token).
  if (import.meta.env.DEV) {
    return mintToken("/api/auth/dev-token", { role: "DRIVER", device_id: id });
  }
  return false;
}

export const api = {
  health: () => http<{ status: string }>("/healthz"),

  // --- live trip / position ---
  truck: (deviceId: string) => http<TruckEnvelope>(`/api/trucks/${encodeURIComponent(deviceId)}`),

  // --- geometry for the mini-map ---
  gates: () => http<{ gates: Gate[] }>("/api/gates"),
  corridor: () => http<CorridorGeometry>("/api/corridor"),

  // --- TAS slot book (next allocated gate window) ---
  tasSlots: (gateId?: string) =>
    http<{ slots: TasSlot[] }>(
      `/api/tas/slots${gateId ? `?gate_id=${encodeURIComponent(gateId)}` : ""}`,
    ),

  // --- re-route fallback polling + ACK round-trip ---
  latestReroute: (deviceId: string) =>
    http<{ device_id: string; advisory: any | null }>(
      `/api/trucks/${encodeURIComponent(deviceId)}/route/latest`,
    ),
  ackReroute: (deviceId: string, state: "ACK" | "DECLINE" = "ACK") =>
    http<{ acked: boolean; state: string }>(
      `/api/trucks/${encodeURIComponent(deviceId)}/route/ack`,
      { method: "POST", body: JSON.stringify({ state }) },
    ),

  // --- inbox: advisories / alerts / challans ---
  alerts: (params?: { since?: string; kind?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.since) q.set("since", params.since);
    if (params?.kind) q.set("kind", params.kind);
    if (params?.limit) q.set("limit", String(params.limit));
    return http<{ source: string; alerts: any[] }>(`/api/alerts${q.toString() ? `?${q}` : ""}`);
  },
  // Acknowledge an alert (NOTIF-5 ack-tracking).
  ackAlert: (alertId: string) =>
    http<{ id: string; ack: boolean; persisted: boolean }>(
      `/api/alerts/${encodeURIComponent(alertId)}/ack`,
      { method: "POST" },
    ),

  // Parking availability inside the geo-fenced port (SCOPE-R1 / IU2 driver view).
  parkingSummary: () =>
    http<{ total_capacity?: number; total_available?: number; facilities?: number }>(
      "/api/parking/summary",
    ),

  // --- profile / vehicle: VahanRecord ---
  vahanRc: (plate: string) => http<VahanEnvelope>(`/api/vahan/rc/${encodeURIComponent(plate)}`),
  fastag: (plate: string) =>
    http<{ plate: string; decision_path: string; record: Record<string, any> }>(
      `/api/vahan/fastag/${encodeURIComponent(plate)}`,
    ),

  // --- Driver face enrolment (Identity / C2) ---
  // Submit the completed profile + consented reference frames. The driver is NOT
  // activated immediately — an admin reviews and approves in the web portal.
  enrolRequest: (body: {
    driver_id: string;
    name: string;
    license_no?: string;
    mobile?: string;
    vehicle_no?: string;
    aadhaar?: string;
    emergency_contact?: string;
    consent: boolean;
    images: string[];
    documents?: { kind: string; image: string }[];
  }) =>
    http<{ submitted: boolean; status: string; driver_id: string; enrollment: any }>(
      "/api/identity/enrol-request",
      {
        method: "POST",
        body: JSON.stringify({ ...body, is_synthetic: true, purpose: "ENROLMENT" }),
      },
    ),
  // Poll the driver's own enrolment status (PENDING / ACTIVE / REJECTED / REENROLL).
  enrolStatus: (driverId: string) =>
    http<{ driver_id: string; status: string; rejection_reason?: string | null }>(
      `/api/identity/enrol-request/${encodeURIComponent(driverId)}`,
    ),

  // --- WebPush subscription ---
  vapidKey: () => http<{ key: string | null; configured: boolean }>("/api/push/vapid-public-key"),
  pushSubscribe: (deviceId: string, subscription: PushSubscriptionJSON) =>
    http<{ subscribed: boolean; total: number }>("/api/push/subscribe", {
      method: "POST",
      body: JSON.stringify({ device_id: deviceId, subscription }),
    }),
  pushTest: (deviceId: string) =>
    http<{ delivered: boolean }>(`/api/push/test/${encodeURIComponent(deviceId)}`, {
      method: "POST",
    }),
};
