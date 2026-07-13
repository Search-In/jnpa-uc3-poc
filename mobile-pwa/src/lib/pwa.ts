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
  console.log("[fcm] enableFcm entered", { deviceId });
  if (!isFcmConfigured() || !("serviceWorker" in navigator)) {
    console.warn("[fcm] aborting: not configured or no SW support", {
      configured: isFcmConfigured(),
      serviceWorker: "serviceWorker" in navigator,
    });
    return false;
  }

  // enableFcm runs right after pairing — BEFORE the user has tapped "Enable
  // alerts" — so notification permission is almost always still "default" at this
  // point. getToken() hard-requires "granted", so request it here instead of
  // silently bailing (the previous `!== "granted"` early return was why
  // register-device was never called). Called synchronously inside the click
  // handler's microtask, so the prompt still counts as user-initiated.
  let permission = Notification.permission;
  console.log("[fcm] permission (initial):", permission);
  if (permission === "default") {
    permission = await Notification.requestPermission();
    console.log("[fcm] permission (after request):", permission);
  }
  if (permission !== "granted") {
    console.warn("[fcm] aborting: notification permission not granted:", permission);
    return false;
  }

  try {
    console.log("[fcm] awaiting serviceWorker.ready…");
    const reg = await navigator.serviceWorker.ready;
    console.log("[fcm] service worker ready, scope:", reg?.scope);

    console.log("[fcm] getToken start");
    const token = await getFcmToken(reg);
    if (!token) {
      console.warn("[fcm] aborting: getFcmToken returned no token");
      return false;
    }
    console.log("[fcm] getToken success:", `${token.slice(0, 12)}…(${token.length} chars)`);

    const plate = getPairing()?.plate ?? undefined;
    console.log("[fcm] register-device request", { deviceId, platform: "web", vehicleId: plate });
    const res = await api.registerDevice(deviceId, token, { platform: "web", vehicleId: plate });
    console.log("[fcm] register-device response:", res);

    await wireFcmForeground();
    return true;
  } catch (err) {
    console.error("[fcm] enable failed", err);
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

  // One permission prompt covers both transports.
  let permission = Notification.permission;
  if (permission === "default") permission = await Notification.requestPermission();
  if (permission !== "granted") return "denied";

  // ---- WebPush/VAPID leg — the PRIMARY transport. Runs FIRST and is fully
  // independent of Firebase: when VAPID is configured we subscribe and store the
  // subscription regardless of whether FCM is configured or succeeds. This is the
  // leg that populates push_subscriptions.webpush.
  let webpushOk = false;
  let vapid: { key: string | null; configured: boolean };
  try {
    vapid = await api.vapidKey();
  } catch {
    vapid = { key: null, configured: false };
  }
  if (vapid.configured && vapid.key) {
    try {
      const reg = await navigator.serviceWorker.ready;
      const existing = await reg.pushManager.getSubscription();
      const sub =
        existing ??
        (await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(vapid.key),
        }));
      await api.pushSubscribe(deviceId, sub.toJSON() as PushSubscriptionJSON);
      webpushOk = true;
    } catch (err) {
      console.warn("push subscribe failed", err);
    }
  }

  // ---- Firebase FCM leg — OPTIONAL, best-effort. When Firebase is unconfigured
  // enableFcm() returns false immediately; any failure here is swallowed so it can
  // NEVER abort or roll back the WebPush registration above.
  let fcmOk = false;
  try {
    fcmOk = await enableFcm(deviceId);
  } catch (err) {
    console.warn("[fcm] enable threw (ignored, WebPush unaffected)", err);
  }

  if (webpushOk || fcmOk) return "subscribed";
  return vapid.configured ? "error" : "not-configured";
}
