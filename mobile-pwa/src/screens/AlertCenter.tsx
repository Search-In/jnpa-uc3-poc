import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";
import { cached } from "@/lib/store";
import { alertToNotification } from "@/lib/notify";
import { SkeletonCard } from "@/components/Skeleton";
import { IconPin, IconAlertTriangle } from "@/components/icons";

// Alert Center — category-based driver alert inbox. Consumes real /api/alerts
// (RDS-backed jnpa.alerts) and buckets each alert into a driver-friendly
// category. Works offline: last alerts are served from the IndexedDB cache when
// the network is down.

type Alert = {
  id: string;
  kind?: string;
  severity?: string;
  ts?: string;
  plate?: string | null;
  payload?: Record<string, any>;
};

type Cat = "all" | "traffic" | "parking" | "customs" | "geofence" | "ai" | "vehicle";

const CATS: { key: Cat; label: string; icon: string }[] = [
  { key: "all", label: "All", icon: "🔔" },
  { key: "traffic", label: "Traffic", icon: "🚦" },
  { key: "parking", label: "Parking", icon: "🅿" },
  { key: "customs", label: "Customs", icon: "🛃" },
  { key: "geofence", label: "Geo-fence", icon: "⬠" },
  { key: "ai", label: "AI", icon: "🎥" },
  { key: "vehicle", label: "Vehicle", icon: "🚛" },
];

function categoryOf(kind: string): Cat {
  const k = kind.toUpperCase();
  if (k.includes("CUSTOMS")) return "customs";
  if (k.includes("PARKING") || k.includes("OVERFLOW")) return "parking";
  if (k.includes("GEOFENCE") || k.includes("RESTRICTED") || k.includes("ZONE")) return "geofence";
  if (
    k.includes("WRONG") ||
    k.includes("QUEUE") ||
    k.includes("DENSITY") ||
    k.includes("ANPR") ||
    k.includes("AI")
  )
    return "ai";
  if (
    k.includes("PROVISIONAL") ||
    k.includes("VEHICLE") ||
    k.includes("CHALLAN") ||
    k.includes("BLACKLIST")
  )
    return "vehicle";
  return "traffic";
}

// Human "required action" per alert kind — plain language for a driver.
function actionFor(kind: string): string {
  const k = kind.toUpperCase();
  if (k.includes("NO_PARKING") || k.includes("ILLEGAL_PARKING"))
    return "Move your vehicle within 5 minutes";
  if (k.includes("RESTRICTED")) return "Leave the restricted zone immediately";
  if (k.includes("CUSTOMS")) return "Report to the customs desk";
  if (k.includes("WRONG")) return "Correct your direction of travel";
  if (k.includes("CONGESTION")) return "Expect delay — consider re-routing";
  if (k.includes("PROVISIONAL")) return "Complete verification at the gate";
  return "Acknowledge and proceed with caution";
}

export default function AlertCenter() {
  const { t } = useTranslation();
  const [cat, setCat] = useState<Cat>("all");
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [offline, setOffline] = useState(false);
  const [loading, setLoading] = useState(true);

  const load = async () => {
    const data = await cached<{ alerts?: Alert[] }>(
      "alerts",
      () => api.alerts({ limit: 100 }) as any,
    );
    if (data?.alerts) {
      setAlerts(data.alerts);
      setOffline(!navigator.onLine);
    } else {
      setOffline(true);
    }
    setLoading(false);
  };

  useEffect(() => {
    void load();
    const on = () => void load();
    window.addEventListener("online", on);
    const iv = window.setInterval(load, 20000);
    return () => {
      window.removeEventListener("online", on);
      window.clearInterval(iv);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const a of alerts) {
      const cc = categoryOf(a.kind || "");
      c[cc] = (c[cc] || 0) + 1;
    }
    return c;
  }, [alerts]);

  const filtered = useMemo(
    () => (cat === "all" ? alerts : alerts.filter((a) => categoryOf(a.kind || "") === cat)),
    [alerts, cat],
  );

  return (
    <div style={{ padding: 12 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 8,
        }}
      >
        <h2 style={{ fontSize: 16 }}>{t("alertCenter.title", { defaultValue: "Alerts" })}</h2>
        {offline && (
          <span className="muted" style={{ fontSize: 11 }}>
            ◐ offline (cached)
          </span>
        )}
      </div>

      <div
        style={{ display: "flex", gap: 6, overflowX: "auto", paddingBottom: 6, marginBottom: 10 }}
      >
        {CATS.map((c) => (
          <button
            key={c.key}
            onClick={() => setCat(c.key)}
            style={{
              whiteSpace: "nowrap",
              borderRadius: 999,
              padding: "6px 12px",
              fontSize: 13,
              border: "1px solid var(--border,#ccc)",
              background: cat === c.key ? "var(--blue,#06c)" : "transparent",
              color: cat === c.key ? "#fff" : "inherit",
            }}
          >
            {c.icon} {c.label}
            {c.key !== "all" && counts[c.key] ? ` (${counts[c.key]})` : ""}
          </button>
        ))}
      </div>

      {loading ? (
        <SkeletonCard lines={2} />
      ) : !filtered.length ? (
        <div className="empty">
          <div style={{ fontSize: 34, marginBottom: 6 }}>✅</div>
          {t("alertCenter.empty", { defaultValue: "No alerts right now. Drive safe." })}
        </div>
      ) : (
        filtered.map((a) => {
          const kind = a.kind || "ALERT";
          const crit = (a.severity || "").toLowerCase() === "critical";
          const p = a.payload || {};
          // Only show a driver-meaningful location (gate / zone / plate) — never a
          // raw device / segment / container id.
          const loc = p.gate_id || p.zone_id || a.plate || p.plate || null;
          const cat = alertToNotification(String(kind));
          return (
            <div
              key={a.id}
              className="card"
              style={{
                padding: 14,
                marginBottom: 10,
                borderLeft: `4px solid ${crit ? "var(--red,#c00)" : "var(--orange,#d80)"}`,
              }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 8 }}>
                <div
                  style={{ fontWeight: 700, fontSize: 16, display: "flex", alignItems: "center", gap: 6 }}
                >
                  {crit ? (
                    <span style={{ color: "var(--red)", flex: "none" }}>
                      <IconAlertTriangle size={17} />
                    </span>
                  ) : null}
                  {cat.title}
                </div>
                <div className="muted" style={{ fontSize: 12.5, flex: "none" }}>
                  {a.ts ? new Date(a.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""}
                </div>
              </div>
              {loc ? (
                <div
                  className="muted"
                  style={{ fontSize: 13, marginTop: 3, display: "flex", alignItems: "center", gap: 5 }}
                >
                  <IconPin size={13} />
                  {loc.toString().startsWith("G-")
                    ? `Gate ${loc.toString().replace(/^G-/, "")}`
                    : loc}
                </div>
              ) : null}
              <div
                style={{
                  fontSize: 14.5,
                  fontWeight: 600,
                  marginTop: 8,
                  color: crit ? "var(--red,#c00)" : "var(--text)",
                }}
              >
                → {actionFor(kind)}
              </div>
            </div>
          );
        })
      )}
    </div>
  );
}
