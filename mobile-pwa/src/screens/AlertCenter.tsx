import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";
import { cached } from "@/lib/store";

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
  if (k.includes("WRONG") || k.includes("QUEUE") || k.includes("DENSITY") || k.includes("ANPR") || k.includes("AI"))
    return "ai";
  if (k.includes("PROVISIONAL") || k.includes("VEHICLE") || k.includes("CHALLAN") || k.includes("BLACKLIST"))
    return "vehicle";
  return "traffic";
}

// Human "required action" per alert kind — plain language for a driver.
function actionFor(kind: string): string {
  const k = kind.toUpperCase();
  if (k.includes("NO_PARKING") || k.includes("ILLEGAL_PARKING")) return "Move your vehicle within 5 minutes";
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
    const data = await cached<{ alerts?: Alert[] }>("alerts", () => api.alerts({ limit: 100 }) as any);
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
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
        <h2 style={{ fontSize: 16 }}>{t("alertCenter.title", { defaultValue: "Alerts" })}</h2>
        {offline && <span className="muted" style={{ fontSize: 11 }}>◐ offline (cached)</span>}
      </div>

      <div style={{ display: "flex", gap: 6, overflowX: "auto", paddingBottom: 6, marginBottom: 10 }}>
        {CATS.map((c) => (
          <button
            key={c.key}
            onClick={() => setCat(c.key)}
            style={{
              whiteSpace: "nowrap", borderRadius: 999, padding: "6px 12px", fontSize: 13, border: "1px solid var(--border,#ccc)",
              background: cat === c.key ? "var(--blue,#06c)" : "transparent",
              color: cat === c.key ? "#fff" : "inherit",
            }}
          >
            {c.icon} {c.label}{c.key !== "all" && counts[c.key] ? ` (${counts[c.key]})` : ""}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="muted">Loading…</div>
      ) : !filtered.length ? (
        <div className="muted" style={{ padding: 24, textAlign: "center" }}>No alerts in this category.</div>
      ) : (
        filtered.map((a) => {
          const kind = a.kind || "ALERT";
          const crit = (a.severity || "").toLowerCase() === "critical";
          const p = a.payload || {};
          const loc =
            p.zone_id || p.gate_id || p.container_no || p.segment_id ||
            a.plate || p.plate || p.device_id || p.vehicle_id || "—";
          return (
            <div key={a.id} className="card" style={{ padding: 12, marginBottom: 8, borderLeft: `3px solid ${crit ? "var(--red,#c00)" : "var(--amber,#d80)"}` }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <div style={{ fontWeight: 600 }}>{kind.replace(/_/g, " ")}</div>
                <div className="muted" style={{ fontSize: 11 }}>{a.ts ? new Date(a.ts).toLocaleTimeString() : ""}</div>
              </div>
              <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>📍 {String(loc)}</div>
              <div style={{ fontSize: 13, marginTop: 6, color: crit ? "var(--red,#c00)" : undefined }}>
                → {actionFor(kind)}
              </div>
            </div>
          );
        })
      )}
    </div>
  );
}
