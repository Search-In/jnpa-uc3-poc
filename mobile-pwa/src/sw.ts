/// <reference lib="webworker" />
// Custom service worker (injectManifest). Workbox injects the precache manifest
// at `self.__WB_MANIFEST`; we add the WebPush handlers the trucking-app needs:
//
//   * `push`             -> show a re-route / advisory notification AND forward
//                           the payload to any open client so the in-app banner
//                           appears immediately (push is the backgrounded path).
//   * `notificationclick`-> focus / open the PWA and deep-link to /reroute.
//
// Keeping the SW hand-written (vs generateSW) is what lets us own these events.

import { precacheAndRoute, cleanupOutdatedCaches } from "workbox-precaching";
import { clientsClaim } from "workbox-core";

declare const self: ServiceWorkerGlobalScope & {
  __WB_MANIFEST: { url: string; revision: string | null }[];
};

const BASE = self.registration.scope; // e.g. https://host/pwa/

// Precache the app shell so first paint is instant offline.
precacheAndRoute(self.__WB_MANIFEST || []);
cleanupOutdatedCaches();

self.addEventListener("install", () => {
  self.skipWaiting();
});
clientsClaim();

interface PushPayload {
  type?: string;
  title?: string;
  body?: string;
  device_id?: string;
  gate_id?: string | null;
  // Deep-link hash (e.g. "#/zones") set by locally-raised notifications
  // (lib/notify) so notificationclick can route to the right screen.
  href?: string;
  category?: string;
  [k: string]: unknown;
}

self.addEventListener("push", (event: PushEvent) => {
  let data: PushPayload = {};
  try {
    data = event.data ? (event.data.json() as PushPayload) : {};
  } catch {
    data = { title: "JNPA Trucking", body: event.data?.text() ?? "New advisory" };
  }

  const title = data.title || "JNPA Trucking — Advisory";
  const body =
    data.body ||
    (data.type === "reroute"
      ? `New gate assigned${data.gate_id ? `: ${data.gate_id}` : ""}.`
      : "You have a new advisory.");

  const show = self.registration.showNotification(title, {
    body,
    icon: `${BASE}icons/icon-192.png`,
    badge: `${BASE}icons/icon-192.png`,
    tag: data.type === "reroute" ? "jnpa-reroute" : "jnpa-advisory",
    requireInteraction: data.type === "reroute",
    data,
  } as NotificationOptions);

  // Forward to any open client so the in-app re-route banner fires even while
  // the page is foregrounded (push + WS are belt-and-braces for the 5 s SLA).
  const forward = self.clients
    .matchAll({ type: "window", includeUncontrolled: true })
    .then((clients) => {
      for (const c of clients) c.postMessage({ source: "push", frame: data });
    });

  event.waitUntil(Promise.all([show, forward]));
});

self.addEventListener("notificationclick", (event: NotificationEvent) => {
  event.notification.close();
  const data = (event.notification.data || {}) as PushPayload;
  // Honour an explicit deep-link hash first (locally-raised notifications), then
  // fall back to reroute → /reroute, everything else → /inbox.
  const hash = data.href
    ? data.href.startsWith("#")
      ? data.href
      : `#${data.href}`
    : data.type === "reroute"
      ? "#/reroute"
      : "#/inbox";
  const target = `${BASE}${hash}`;

  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const c of clients) {
        if ("focus" in c) {
          // Navigate the focused client to the deep-link, then focus it.
          c.postMessage({ source: "push", frame: data, navigate: hash });
          return c.focus();
        }
      }
      return self.clients.openWindow(target);
    }),
  );
});
