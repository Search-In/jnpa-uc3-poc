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

// Mint a DRIVER-scoped JWT bound to this device. Public endpoint (no auth
// required). Stores the token on success. Falls back to the dev-token seam for
// older gateways / local dev. Best-effort: returns false rather than throwing so
// pairing never hard-fails the UI.
export async function ensureDeviceToken(deviceId: string): Promise<boolean> {
  if (!tokenNeedsRefresh()) return true;
  try {
    const res = await fetch("/api/auth/device-token", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ device_id: deviceId, pairing_secret: PAIRING_SECRET }),
    });
    if (res.ok) {
      const data = (await res.json()) as { access_token?: string };
      if (data.access_token) {
        setToken(data.access_token);
        return true;
      }
    }
  } catch {
    /* fall through to dev-token */
  }
  // Local-dev fallback: the password-less dev-token seam (404 in prod-like envs).
  try {
    const res = await fetch("/api/auth/dev-token", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ role: "DRIVER", device_id: deviceId }),
    });
    if (res.ok) {
      const data = (await res.json()) as { access_token?: string };
      if (data.access_token) {
        setToken(data.access_token);
        return true;
      }
    }
  } catch {
    /* auth disabled or unreachable — proceed unauthenticated */
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
