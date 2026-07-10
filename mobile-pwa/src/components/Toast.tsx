import { useEffect, useState } from "react";
import type { NotifyCategory } from "@/lib/notify";

// Toast host — renders the foreground half of the driver notification layer
// (lib/notify). It listens for `jnpa:toast` events and shows a compact,
// tappable banner near the top of the screen. Tapping deep-links via the hash
// router and dismisses. Auto-dismisses after a few seconds (longer for
// emergencies). This is what a driver sees while the app is open; when the app
// is backgrounded, notify.ts routes to a real system notification instead.

interface ToastItem {
  id: number;
  title: string;
  body?: string;
  icon: string;
  category: NotifyCategory;
  href?: string;
}

let nextId = 1;

const TONE: Record<NotifyCategory, string> = {
  reroute: "var(--blue)",
  congestion: "var(--orange)",
  parking: "var(--green)",
  compliance: "var(--orange)",
  emergency: "var(--red)",
  info: "var(--muted)",
};

export default function Toast() {
  const [items, setItems] = useState<ToastItem[]>([]);

  useEffect(() => {
    const onToast = (ev: Event) => {
      const d = (ev as CustomEvent).detail || {};
      const id = nextId++;
      const item: ToastItem = {
        id,
        title: d.title || "Advisory",
        body: d.body,
        icon: d.icon || "🔔",
        category: (d.category as NotifyCategory) || "info",
        href: d.href,
      };
      setItems((cur) => [...cur, item].slice(-3)); // keep at most 3 stacked
      const ttl = item.category === "emergency" ? 10_000 : 6_000;
      window.setTimeout(() => {
        setItems((cur) => cur.filter((t) => t.id !== id));
      }, ttl);
    };
    window.addEventListener("jnpa:toast", onToast as EventListener);
    return () => window.removeEventListener("jnpa:toast", onToast as EventListener);
  }, []);

  const dismiss = (id: number) => setItems((cur) => cur.filter((t) => t.id !== id));

  const tap = (t: ToastItem) => {
    if (t.href) location.hash = t.href.replace(/^#/, "");
    dismiss(t.id);
  };

  if (!items.length) return null;

  return (
    <div className="toast-host" role="region" aria-live="polite" aria-label="Notifications">
      {items.map((t) => (
        <button
          key={t.id}
          className="toast"
          style={{ borderLeftColor: TONE[t.category] }}
          onClick={() => tap(t)}
        >
          <span className="toast-icon" aria-hidden>
            {t.icon}
          </span>
          <span className="toast-text">
            <span className="toast-title">{t.title}</span>
            {t.body ? <span className="toast-body">{t.body}</span> : null}
          </span>
          <span
            className="toast-close"
            aria-hidden
            onClick={(e) => {
              e.stopPropagation();
              dismiss(t.id);
            }}
          >
            ×
          </span>
        </button>
      ))}
    </div>
  );
}
