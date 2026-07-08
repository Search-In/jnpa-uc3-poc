import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useDriverSession } from "@/hooks/DriverSession";
import { useRealtime } from "@/hooks/RealtimeContext";
import MiniMap from "@/components/MiniMap";
import { api } from "@/lib/api";
import { cached } from "@/lib/store";
import type { CorridorGeometry, Gate, TruckEnvelope } from "@/lib/types";

// Home — the driver dashboard shown right after login. Big status card, quick
// info tiles and a live map preview. All figures come from real gateway data
// (truck snapshot + parking summary); it degrades to "—" when a value is not
// yet available rather than blocking.

type Status = { tone: "moving" | "waiting" | "alert"; icon: string; label: string };

const TONE: Record<Status["tone"], { bg: string; fg: string }> = {
  moving: { bg: "rgba(22,163,74,.12)", fg: "#16a34a" },
  waiting: { bg: "rgba(217,119,6,.14)", fg: "#d97706" },
  alert: { bg: "rgba(220,38,38,.12)", fg: "#dc2626" },
};

export default function Home({ deviceId, plate }: { deviceId: string; plate?: string | null }) {
  const { t } = useTranslation();
  const { session } = useDriverSession();
  const { unread } = useRealtime();
  const navigate = useNavigate();

  const [truck, setTruck] = useState<TruckEnvelope | null>(null);
  const [corridor, setCorridor] = useState<CorridorGeometry | undefined>();
  const [gates, setGates] = useState<Gate[] | undefined>();
  const [parkingFree, setParkingFree] = useState<number | null>(null);

  useEffect(() => {
    void cached<CorridorGeometry>("corridor", () => api.corridor()).then(
      (c) => c && setCorridor(c),
    );
    void cached<{ gates: Gate[] }>("gates", () => api.gates()).then((g) => g && setGates(g.gates));
  }, []);

  useEffect(() => {
    const poll = async () => {
      try {
        setTruck(await api.truck(deviceId));
      } catch {
        /* offline / no fix */
      }
      try {
        const p = await api.parkingSummary();
        setParkingFree(p.total_available ?? null);
      } catch {
        /* ignore */
      }
    };
    void poll();
    const iv = window.setInterval(poll, 8000);
    return () => window.clearInterval(iv);
  }, [deviceId]);

  const rec: any = truck?.record ?? {};
  const speed = typeof rec.speed_kmh === "number" ? rec.speed_kmh : null;
  const etaMin = (truck as any)?.eta_min ?? (rec.eta_s ? rec.eta_s / 60 : null);
  const remainingKm = (truck as any)?.remaining_km ?? rec.remaining_km ?? null;
  const state: string = rec.state || (truck as any)?.state || "";

  const status: Status = (() => {
    if ((truck as any)?.elevated_scrutiny || unread > 0)
      return {
        tone: "alert",
        icon: "🔴",
        label: t("home.moving.alert", { defaultValue: "Alert" }),
      };
    if (/QUEUE|WAIT|GATE|IDLE|STOP/i.test(state) || (speed != null && speed <= 3))
      return {
        tone: "waiting",
        icon: "🟡",
        label: t("home.moving.waiting", { defaultValue: "Waiting" }),
      };
    return {
      tone: "moving",
      icon: "🟢",
      label: t("home.moving.moving", { defaultValue: "Moving" }),
    };
  })();

  const vehicle = plate || session.vehicle || null;
  const tone = TONE[status.tone];

  const tile = (label: string, value: string, sub?: string) => (
    <div
      style={{
        flex: "1 1 44%",
        background: "var(--surface2,#f1f4f9)",
        borderRadius: 14,
        padding: "12px 14px",
        minWidth: 0,
      }}
    >
      <div style={{ fontSize: 22, fontWeight: 700, lineHeight: 1.1 }}>{value}</div>
      <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
        {label}
        {sub ? ` · ${sub}` : ""}
      </div>
    </div>
  );

  return (
    <div style={{ padding: 12, paddingBottom: 24 }}>
      {/* Driver + vehicle header */}
      <div className="card" style={{ padding: 16, marginBottom: 12 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span
            style={{
              width: 46,
              height: 46,
              borderRadius: "50%",
              background: "var(--blue,#2563eb)",
              color: "#fff",
              display: "grid",
              placeItems: "center",
              fontSize: 20,
              fontWeight: 700,
            }}
          >
            {(session.name || "D").trim().charAt(0).toUpperCase()}
          </span>
          <div style={{ minWidth: 0 }}>
            <div className="muted" style={{ fontSize: 12 }}>
              {t("home.welcome")}
            </div>
            <div style={{ fontSize: 20, fontWeight: 700, lineHeight: 1.1 }}>
              {session.name || t("home.driver", { defaultValue: "Driver" })}
            </div>
            <div
              style={{ fontSize: 14, fontWeight: 600, color: "var(--blue,#2563eb)", marginTop: 2 }}
            >
              {vehicle || t("common.noData", { defaultValue: "—" })}
            </div>
          </div>
        </div>

        {/* Big status pill */}
        <div
          style={{
            marginTop: 14,
            background: tone.bg,
            borderRadius: 14,
            padding: "12px 16px",
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <span style={{ fontSize: 22 }}>{status.icon}</span>
          <div>
            <div style={{ fontSize: 18, fontWeight: 700, color: tone.fg }}>{status.label}</div>
            <div className="muted" style={{ fontSize: 12 }}>
              {status.tone === "moving" && speed != null ? `${Math.round(speed)} km/h` : ""}
              {status.tone === "alert" && unread > 0
                ? `${unread} ${t("home.newAlerts", { defaultValue: "new alerts" })}`
                : ""}
              {status.tone === "waiting"
                ? t("home.atGate", { defaultValue: "At gate / queue" })
                : ""}
            </div>
          </div>
        </div>

        {session.status !== "ACTIVE" && (
          <button
            className="btn primary"
            style={{ marginTop: 12, width: "100%" }}
            onClick={() => navigate("/enrol")}
          >
            {t("home.completeEnrol", { defaultValue: "Complete enrolment" })}
          </button>
        )}
      </div>

      {/* Quick info tiles */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 12 }}>
        {tile(
          t("home.eta", { defaultValue: "ETA to gate" }),
          etaMin != null ? `${Math.round(etaMin)}m` : "—",
        )}
        {tile(
          t("home.distance", { defaultValue: "Distance" }),
          remainingKm != null ? `${Number(remainingKm).toFixed(1)} km` : "—",
        )}
        {tile(
          t("home.speed", { defaultValue: "Speed" }),
          speed != null ? `${Math.round(speed)}` : "—",
          "km/h",
        )}
        {tile(
          t("home.parkingFree", { defaultValue: "Parking free" }),
          parkingFree != null ? String(parkingFree) : "—",
        )}
      </div>

      {/* Live map preview */}
      <div className="card" style={{ padding: 0, overflow: "hidden", marginBottom: 12 }}>
        <div
          style={{ padding: "10px 14px 0", fontSize: 12, fontWeight: 600, color: "var(--muted)" }}
        >
          {t("home.mapPreview", { defaultValue: "LIVE POSITION" })}
        </div>
        <div style={{ height: 180 }}>
          <MiniMap
            corridor={corridor}
            gates={gates}
            truck={rec.lat != null ? ({ lat: rec.lat, lon: rec.lon } as any) : null}
          />
        </div>
        <button
          className="btn ghost"
          style={{ width: "100%", borderRadius: 0 }}
          onClick={() => navigate("/map")}
        >
          {t("home.openMap", { defaultValue: "Open full map" })} ›
        </button>
      </div>

      {/* Primary actions */}
      <div style={{ display: "flex", gap: 8 }}>
        <button className="btn primary" style={{ flex: 1 }} onClick={() => navigate("/trip")}>
          🛣 {t("home.startTrip", { defaultValue: "Live Trip" })}
        </button>
        <button className="btn" style={{ flex: 1 }} onClick={() => navigate("/alerts")}>
          🔔 {t("home.alerts", { defaultValue: "Alerts" })}
          {unread > 0 ? ` (${unread})` : ""}
        </button>
      </div>
    </div>
  );
}
