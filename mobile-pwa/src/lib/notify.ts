// Driver notification layer.
//
// The gateway detects congestion, parking, compliance and restricted-zone
// events server-side but today only PUSHES re-routes to the driver (see
// gateway/routers/trucks.py); the other categories are broadcast to the
// control-room dashboard only. Until the backend also calls push.deliver() for
// them (tracked in the audit report), this module makes those categories
// actually reach the driver's device from the signals the PWA ALREADY receives
// live (WS `alert` frames, geo-fence events):
//
//   * foreground  -> an in-app toast (custom `jnpa:toast` event; rendered by
//                    components/Toast).
//   * backgrounded -> a real system notification via the service worker, so it
//                    shows on the lock screen / notification shade like FCM/push
//                    would (requires the driver to have granted permission).
//
// One code path, five categories — the same contract a Firebase/WebPush message
// would satisfy, minus the server round-trip.

export type NotifyCategory =
  | "reroute"
  | "congestion"
  | "parking"
  | "compliance"
  | "emergency"
  | "info";

export interface DriverNotification {
  title: string;
  body?: string;
  category?: NotifyCategory;
  /** collapse key so repeats of the same event replace rather than stack */
  tag?: string;
  /** hash route to open when the notification/toast is tapped, e.g. "#/alerts" */
  href?: string;
  data?: Record<string, unknown>;
}

const ICON: Record<NotifyCategory, string> = {
  reroute: "↻",
  congestion: "🚦",
  parking: "🅿️",
  compliance: "📄",
  emergency: "🚨",
  info: "🔔",
};

// De-dupe identical notifications fired within a short window (the same alert can
// arrive over both the WS frame and a poll backfill).
const recent = new Map<string, number>();
const DEDUPE_MS = 15_000;

function seenRecently(key: string): boolean {
  const now = Date.now();
  for (const [k, t] of recent) if (now - t > DEDUPE_MS) recent.delete(k);
  if (recent.has(key)) return true;
  recent.set(key, now);
  return false;
}

export function notifyDriver(n: DriverNotification): void {
  const category = n.category ?? "info";
  const key = n.tag || `${category}:${n.title}:${n.body ?? ""}`;
  if (seenRecently(key)) return;

  const icon = ICON[category];

  // Always emit the in-app toast; the Toast host decides whether the app is
  // foregrounded and only renders when it is worth showing.
  try {
    window.dispatchEvent(
      new CustomEvent("jnpa:toast", {
        detail: { ...n, category, icon },
      }),
    );
  } catch {
    /* no window (SSR / worker) — ignore */
  }

  // When the app is not visible, escalate to a real system notification so the
  // driver sees it on the lock screen. When visible, the toast is enough.
  const hidden = typeof document !== "undefined" && document.visibilityState !== "visible";
  if (!hidden) return;
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;

  navigator.serviceWorker?.ready
    .then((reg) => {
      const base = reg.scope; // e.g. https://host/pwa/
      return reg.showNotification(`${icon}  ${n.title}`, {
        body: n.body,
        tag: n.tag || key,
        badge: `${base}icons/icon-192.png`,
        icon: `${base}icons/icon-192.png`,
        data: { ...n.data, href: n.href, category },
        // Emergencies stay on screen until the driver dismisses them.
        requireInteraction: category === "emergency",
      } as NotificationOptions);
    })
    .catch(() => {
      /* SW not ready — the toast already covered the foreground case */
    });
}

// Map a backend alert `kind` to a driver-facing notification (category + plain
// language). Mirrors the buckets in screens/AlertCenter so the two stay aligned.
export function alertToNotification(kind: string, body?: string): DriverNotification {
  const k = (kind || "").toUpperCase();
  if (k.includes("RESTRICTED") || k.includes("EMERGENCY"))
    return {
      category: "emergency",
      title: "Restricted zone",
      body: body || "Leave the restricted zone immediately.",
      href: "#/zones",
      tag: `emergency:${k}`,
    };
  if (k.includes("NO_PARKING") || k.includes("ILLEGAL_PARKING"))
    return {
      category: "emergency",
      title: "No-parking violation",
      body: body || "Move your vehicle within 5 minutes.",
      href: "#/zones",
      tag: `noparking:${k}`,
    };
  if (k.includes("CONGESTION") || k.includes("QUEUE") || k.includes("DENSITY"))
    return {
      category: "congestion",
      title: "Congestion ahead",
      body: body || "Expect delay — an alternate route may be available.",
      href: "#/map",
      tag: `congestion:${k}`,
    };
  if (k.includes("PARKING") || k.includes("OVERFLOW"))
    return {
      category: "parking",
      title: "Parking update",
      body: body || "Parking availability has changed near you.",
      href: "#/parking",
      tag: `parking:${k}`,
    };
  if (
    k.includes("CUSTOMS") ||
    k.includes("PROVISIONAL") ||
    k.includes("SCRUTINY") ||
    k.includes("BLACKLIST") ||
    k.includes("CHALLAN")
  )
    return {
      category: "compliance",
      title: "Document / compliance",
      body: body || "Vehicle document verification pending.",
      href: "#/profile",
      tag: `compliance:${k}`,
    };
  if (k.includes("DEVIATION"))
    return {
      category: "reroute",
      title: "Route deviation",
      body: body || "You moved away from your assigned route.",
      href: "#/map",
      tag: `deviation:${k}`,
    };
  return {
    category: "info",
    title: kind.replace(/_/g, " ") || "Advisory",
    body,
    href: "#/alerts",
  };
}
