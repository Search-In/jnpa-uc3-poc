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
import type { RecaptchaVerifier as RecaptchaVerifierType } from "firebase/auth";

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

// Phone Auth needs the api key + auth domain (VAPID not required).
export function isPhoneAuthConfigured(): boolean {
  return Boolean(firebaseConfig.apiKey && firebaseConfig.authDomain && firebaseConfig.projectId);
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
  if (!isFcmConfigured()) return null;
  try {
    const { getMessaging, getToken, isSupported } = await import("firebase/messaging");
    if (!(await isSupported())) return null;
    const a = ensureApp();
    if (!a) return null;
    const messaging = getMessaging(a);
    const token = await getToken(messaging, {
      vapidKey: VAPID_KEY,
      serviceWorkerRegistration: registration,
    });
    return token || null;
  } catch (err) {
    console.warn("fcm getToken failed", err);
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

// ---------------------------------------------------------------- Phone Auth
// A live phone-OTP confirmation handle (from signInWithPhoneNumber).
export interface PhoneOtpSession {
  confirm: (code: string) => Promise<string | null>; // -> Firebase ID token
}

// A single, reused RecaptchaVerifier. Recreating a verifier on the same
// container throws "reCAPTCHA has already been rendered in this element" on any
// retry, so we keep exactly one and clear it on failure/unmount.
let phoneVerifier: RecaptchaVerifierType | null = null;

// Tear down the shared verifier (call on failure and on component unmount).
export function clearPhoneVerifier(): void {
  try {
    phoneVerifier?.clear();
  } catch {
    /* already cleared / never rendered — ignore */
  }
  phoneVerifier = null;
}

// Start a phone-OTP sign-in. `recaptchaContainerId` is the id of a DOM node the
// invisible reCAPTCHA attaches to. Returns a session whose confirm() exchanges
// the SMS code for a Firebase ID token (which the gateway then verifies).
// Firebase "test phone numbers" (console-configured) skip real SMS for the demo.
export async function sendPhoneOtp(
  phoneE164: string,
  recaptchaContainerId: string,
): Promise<PhoneOtpSession | null> {
  if (!isPhoneAuthConfigured()) return null;
  const a = ensureApp();
  if (!a) return null;
  const { getAuth, RecaptchaVerifier, signInWithPhoneNumber } = await import("firebase/auth");
  const auth = getAuth(a);

  // DEV/localhost ONLY: bypass app-verification so console-configured test phone
  // numbers (e.g. +91 9999999999 / 654321) work without the reCAPTCHA
  // app-credential round-trip that fails on localhost. NEVER in production —
  // guarded by import.meta.env.DEV so prod builds keep real reCAPTCHA.
  if (import.meta.env.DEV) {
    auth.settings.appVerificationDisabledForTesting = true;
  }

  // Reuse one verifier across retries (see phoneVerifier above).
  if (!phoneVerifier) {
    phoneVerifier = new RecaptchaVerifier(auth, recaptchaContainerId, { size: "invisible" });
  }

  try {
    const confirmation = await signInWithPhoneNumber(auth, phoneE164, phoneVerifier);
    return {
      confirm: async (code: string) => {
        const cred = await confirmation.confirm(code);
        return (await cred.user.getIdToken()) || null;
      },
    };
  } catch (err) {
    // Reset the verifier so the next attempt starts clean, then rethrow the real
    // Firebase error so the caller can surface err.code / err.message.
    clearPhoneVerifier();
    throw err;
  }
}
