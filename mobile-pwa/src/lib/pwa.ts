// Service-worker registration + WebPush subscription helpers.
//
// We register the injectManifest SW ourselves (vite-plugin-pwa's injectRegister
// is null) so the registration is observable and we can wire push from the app.
// WebPush is best-effort: if the gateway has no VAPID key configured, or the
// browser denies notifications, we resolve quietly and the app falls back to the
// WebSocket reroute frame / in-app polling.

import { registerSW } from "virtual:pwa-register";
import { api } from "./api";

export function registerServiceWorker(): void {
  if (!("serviceWorker" in navigator)) return;
  registerSW({ immediate: true });
}

function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  // Back the view with a plain ArrayBuffer so the type matches BufferSource
  // (some lib.dom typings reject ArrayBufferLike for applicationServerKey).
  const buf = new ArrayBuffer(raw.length);
  const out = new Uint8Array(buf);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

export type PushState = "subscribed" | "denied" | "unsupported" | "not-configured" | "error";

// Ask for notification permission and subscribe this device for WebPush. Stores
// the subscription against the device_id on the gateway. Returns the resulting
// state so the UI can show "push enabled" vs the WS/polling fallback.
export async function enablePush(deviceId: string): Promise<PushState> {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return "unsupported";

  let configured: { key: string | null; configured: boolean };
  try {
    configured = await api.vapidKey();
  } catch {
    return "error";
  }
  if (!configured.configured || !configured.key) return "not-configured";

  let permission = Notification.permission;
  if (permission === "default") permission = await Notification.requestPermission();
  if (permission !== "granted") return "denied";

  try {
    const reg = await navigator.serviceWorker.ready;
    const existing = await reg.pushManager.getSubscription();
    const sub =
      existing ??
      (await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(configured.key),
      }));
    await api.pushSubscribe(deviceId, sub.toJSON() as PushSubscriptionJSON);
    return "subscribed";
  } catch (err) {
    console.warn("push subscribe failed", err);
    return "error";
  }
}
