// Firebase client seam — FCM token minting + foreground messages + Phone Auth.
//
// This is an ADDITIVE third notification transport, wired alongside the existing
// WebPush/VAPID subscription (lib/pwa.ts) and the WebSocket realtime worker.
// Everything here is gated on the VITE_FIREBASE_* build config being present: with no
// config the module is a clean no-op and the app runs exactly as before on
// WebPush + WebSocket.
//
// Crucially we do NOT register a second service worker. getToken() is given the
// app's EXISTING service-worker registration, so FCM delivers to the same SW the
// WebPush channel already uses (src/sw.ts normalises the FCM envelope). This is
// what avoids the "two service workers fighting over push" failure mode.

import { initializeApp, getApps, type FirebaseApp } from "firebase/app";

const firebaseConfig = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY as string | undefined,
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN as string | undefined,
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID as string | undefined,
  storageBucket: import.meta.env.VITE_FIREBASE_STORAGE_BUCKET as string | undefined,
  messagingSenderId: import.meta.env.VITE_FIREBASE_MESSAGING_SENDER_ID as string | undefined,
  appId: import.meta.env.VITE_FIREBASE_APP_ID as string | undefined,
};
// The Web-Push certificate (VAPID) key is OPTIONAL: when blank, the Firebase SDK
// falls back to FCM's default application-server key, so getToken still works.
// Provide a custom key (Cloud Messaging -> Web Push certificates) for production.
const VAPID_KEY = (import.meta.env.VITE_FIREBASE_VAPID_KEY as string | undefined) || undefined;

// FCM push needs the messaging config (the VAPID key is optional — see above).
export function isFcmConfigured(): boolean {
  return Boolean(
    firebaseConfig.apiKey &&
    firebaseConfig.projectId &&
    firebaseConfig.appId &&
    firebaseConfig.messagingSenderId,
  );
}

let app: FirebaseApp | null = null;
function ensureApp(): FirebaseApp | null {
  if (!firebaseConfig.apiKey || !firebaseConfig.projectId || !firebaseConfig.appId) return null;
  if (app) return app;
  app = getApps().length ? getApps()[0] : initializeApp(firebaseConfig as Record<string, string>);
  return app;
}

// ------------------------------------------------------------------ FCM push
// Mint an FCM registration token bound to the app's existing service worker.
// Returns null when FCM is not configured / unsupported / permission not granted.
export async function getFcmToken(registration: ServiceWorkerRegistration): Promise<string | null> {
  if (!isFcmConfigured()) {
    console.warn("[fcm] getFcmToken: FCM not configured (missing VITE_FIREBASE_* keys)");
    return null;
  }
  try {
    const { getMessaging, getToken, isSupported } = await import("firebase/messaging");
    const supported = await isSupported();
    console.log("[fcm] messaging isSupported:", supported);
    if (!supported) return null;
    const a = ensureApp();
    if (!a) {
      console.warn("[fcm] getFcmToken: ensureApp() returned null");
      return null;
    }
    const messaging = getMessaging(a);
    console.log("[fcm] getMessaging ready; vapidKey present:", Boolean(VAPID_KEY));
    const token = await getToken(messaging, {
      vapidKey: VAPID_KEY,
      serviceWorkerRegistration: registration,
    });
    console.log("[fcm] getToken raw result:", token ? "token received" : "EMPTY");
    return token || null;
  } catch (err) {
    console.warn("[fcm] getToken failed", err);
    return null;
  }
}

export interface FcmForegroundPayload {
  data?: Record<string, string>;
  notification?: { title?: string; body?: string };
}

// Subscribe to foreground FCM messages. Returns an unsubscribe fn (or a no-op).
export async function onForegroundMessage(
  cb: (payload: FcmForegroundPayload) => void,
): Promise<() => void> {
  if (!isFcmConfigured()) return () => undefined;
  try {
    const { getMessaging, onMessage, isSupported } = await import("firebase/messaging");
    if (!(await isSupported())) return () => undefined;
    const a = ensureApp();
    if (!a) return () => undefined;
    const messaging = getMessaging(a);
    return onMessage(messaging, (payload) => cb(payload as FcmForegroundPayload));
  } catch (err) {
    console.warn("fcm onMessage failed", err);
    return () => undefined;
  }
}
