// Service-worker registration + WebPush subscription helpers.
//
// We register the injectManifest SW ourselves (vite-plugin-pwa's injectRegister
// is null) so the registration is observable and we can wire push from the app.
// WebPush is best-effort: if the gateway has no VAPID key configured, or the
// browser denies notifications, we resolve quietly and the app falls back to the
// WebSocket reroute frame / in-app polling.

import { registerSW } from "virtual:pwa-register";
import { api } from "./api";
import { getFcmToken, isFcmConfigured, onForegroundMessage } from "./firebase";
import { notifyDriver, alertToNotification, type NotifyCategory } from "./notify";
import { getPairing } from "./device";

export function registerServiceWorker(): void {
  if (!("serviceWorker" in navigator)) return;
  registerSW({ immediate: true });
}

// Set up the FCM foreground handler exactly once. Firebase delivers foreground
// messages via onMessage (the SW push handler only fires when backgrounded), so
// we route them into the SAME in-app toast layer WebSocket alerts already use.
let fcmForegroundWired = false;
async function wireFcmForeground(): Promise<void> {
  if (fcmForegroundWired || !isFcmConfigured()) return;
  fcmForegroundWired = true;
  await onForegroundMessage((payload) => {
    const d = payload.data || {};
    const kind = d.type || d.kind || "";
    // Reuse the alert->driver-notification mapping so FCM messages look identical
    // to WS-sourced ones (category, deep-link, plain language).
    const mapped = alertToNotification(kind, d.body);
    notifyDriver({
      title: d.title || mapped.title,
      body: d.body || mapped.body,
      category: (d.category as NotifyCategory) || mapped.category,
      href: d.href || mapped.href,
      tag: d.tag || mapped.tag,
    });
  });
}

// Best-effort: mint an FCM token for this device and register it with the
// gateway. Independent of WebPush — runs whenever Firebase is configured and the
// user has granted notification permission. Returns true if a token registered.
export async function enableFcm(deviceId: string): Promise<boolean> {
  if (!isFcmConfigured() || !("serviceWorker" in navigator)) return false;
  if (Notification.permission !== "granted") return false;
  try {
    const reg = await navigator.serviceWorker.ready;
    const token = await getFcmToken(reg);
    if (!token) return false;
    const plate = getPairing()?.plate ?? undefined;
    await api.registerDevice(deviceId, token, { platform: "web", vehicleId: plate });
    await wireFcmForeground();
    return true;
  } catch (err) {
    console.warn("fcm enable failed", err);
    return false;
  }
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
    configured = { key: null, configured: false };
  }

  // One permission prompt covers both transports.
  let permission = Notification.permission;
  if (permission === "default") permission = await Notification.requestPermission();
  if (permission !== "granted") return "denied";

  // Firebase FCM leg (production transport) — best-effort, independent of VAPID.
  const fcmOk = await enableFcm(deviceId);

  // WebPush/VAPID leg (kept as-is). Skipped cleanly when VAPID is unconfigured.
  if (!configured.configured || !configured.key) {
    return fcmOk ? "subscribed" : "not-configured";
  }
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
    return fcmOk ? "subscribed" : "error";
  }
}
