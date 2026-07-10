import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useDriverSession } from "@/hooks/DriverSession";
import { useRealtime } from "@/hooks/RealtimeContext";
import MiniMap from "@/components/MiniMap";
import GpsStatus from "@/components/GpsStatus";
import { SkeletonCard } from "@/components/Skeleton";
import { IconTruck, IconNavigate, IconRoute, IconParking, IconBell } from "@/components/icons";
import { enablePush } from "@/lib/pwa";
import { notifyDriver } from "@/lib/notify";
import { api } from "@/lib/api";
import { cached } from "@/lib/store";
import { gateShort } from "@/lib/format";
import { statusFromState, trafficFromSpeed, type Tone } from "@/lib/driverLang";
import type { CorridorGeometry, Gate, TruckEnvelope } from "@/lib/types";

// Home — the driver COMMAND screen. Identity strip (avatar · vehicle · online),
// one big "current trip" card (destination, human status, ETA, Start Navigation)
// and a live map preview. All figures come from real gateway data; it shows a
// shimmer skeleton on first load and degrades to "—" rather than blocking.

const TONE: Record<Tone, { bg: string; fg: string }> = {
  moving: { bg: "rgba(0,122,90,.12)", fg: "#007a5a" },
  waiting: { bg: "rgba(31,120,194,.12)", fg: "#1f78c2" },
  idle: { bg: "rgba(181,107,0,.14)", fg: "#b56b00" },
  alert: { bg: "rgba(196,68,31,.12)", fg: "#c4441f" },
};

export default function Home({ deviceId, plate }: { deviceId: string; plate?: string | null }) {
  const { t } = useTranslation();
  const { session } = useDriverSession();
  const { unread, status: conn } = useRealtime();
  const navigate = useNavigate();

  const [truck, setTruck] = useState<TruckEnvelope | null>(null);
  const [corridor, setCorridor] = useState<CorridorGeometry | undefined>();
  const [gates, setGates] = useState<Gate[] | undefined>();
  const [parkingFree, setParkingFree] = useState<number | null>(null);
  // epoch-millis of the last successful truck fix — drives the live GPS pill.
  const [fixAt, setFixAt] = useState<number | null>(null);
  const [loaded, setLoaded] = useState(false);
  const prevParking = useRef<number | null>(null);
  // Show the "enable alerts" nudge only when the driver hasn't decided yet.
  const [askNotif, setAskNotif] = useState(
    () => typeof Notification !== "undefined" && Notification.permission === "default",
  );

  useEffect(() => {
    void cached<CorridorGeometry>("corridor", () => api.corridor()).then(
      (c) => c && setCorridor(c),
    );
    void cached<{ gates: Gate[] }>("gates", () => api.gates()).then((g) => g && setGates(g.gates));
  }, []);

  useEffect(() => {
    const poll = async () => {
      try {
        const env = await api.truck(deviceId);
        setTruck(env);
        // Only treat it as a live fix when a position actually came back. The
        // gateway record carries lat/lon either at the top level or nested
        // under `position`, so accept either shape.
        const r: any = env?.record ?? {};
        if (r.lat != null || r.position?.lat != null) setFixAt(Date.now());
      } catch {
        /* offline / no fix — the GPS pill goes stale/lost on its own */
      } finally {
        setLoaded(true);
      }
      try {
        const p = await api.parkingSummary();
        const free = p.total_available ?? null;
        setParkingFree(free);
        // Notify when parking frees up (was full/none, now has space) — the
        // "CPP Parking Slot available. Proceed within 10 minutes." advisory.
        if (free != null && free > 0 && prevParking.current === 0) {
          notifyDriver({
            category: "parking",
            title: t("parking.title", { defaultValue: "Parking available" }),
            body: `${free} ${t("parking.free", { defaultValue: "free" })} — ${t("home.parkingFree", { defaultValue: "Port parking" })}.`,
            href: "#/parking",
            tag: "parking-available",
          });
        }
        prevParking.current = free;
      } catch {
        /* ignore */
      }
    };
    void poll();
    const iv = window.setInterval(poll, 8000);
    return () => window.clearInterval(iv);
  }, [deviceId]);

  const rec: any = truck?.record ?? {};
  // The gateway telemetry record carries the fix as nested `position: {lat,lon}`
  // (truck-sim shape); accept a top-level lat/lon too for robustness.
  const truckPos: { lat: number; lon: number } | null =
    rec.position?.lat != null
      ? { lat: rec.position.lat, lon: rec.position.lon }
      : rec.lat != null
        ? { lat: rec.lat, lon: rec.lon }
        : null;
  const speed = typeof rec.speed_kmh === "number" ? rec.speed_kmh : null;
  const etaMin = (truck as any)?.eta_min ?? (rec.eta_s ? rec.eta_s / 60 : null);
  const remainingKm = (truck as any)?.remaining_km ?? rec.remaining_km ?? null;
  const state: string = rec.state || (truck as any)?.state || "";
  const gate = rec.gate_id ? gateShort(rec.gate_id) : null;

  // Driver-language status. An unread alert / elevated scrutiny always wins.
  const base = statusFromState(state, speed);
  const status =
    (truck as any)?.elevated_scrutiny || unread > 0
      ? { tone: "alert" as Tone, icon: "🔴", label: t("driverStatus.actionRequired") }
      : { tone: base.tone, icon: base.icon, label: t(`driverStatus.${base.key}`) };
  const tone = TONE[status.tone];
  const traffic = trafficFromSpeed(speed);

  const vehicle = plate || session.vehicle || null;
  const gpsFresh = fixAt != null && Date.now() - fixAt < 30_000;

  const enableAlerts = async () => {
    setAskNotif(false);
    try {
      if (typeof Notification !== "undefined" && Notification.permission === "default") {
        await Notification.requestPermission();
      }
      // Best-effort WebPush subscription so re-routes/alerts arrive backgrounded.
      void enablePush(deviceId);
    } catch {
      /* permission flow unavailable — foreground toasts still work */
    }
  };

  const statusSub =
    status.tone === "alert" && unread > 0
      ? `${unread} ${t("home.newAlerts", { defaultValue: "new alerts" })}`
      : speed != null
        ? `${Math.round(speed)} km/h`
        : "";

  return (
    <div style={{ paddingBottom: 24 }}>
      {/* Identity strip — avatar · vehicle number · online / GPS */}
      <div className="driver-strip">
        <span className="avatar">{(session.name || "D").trim().charAt(0).toUpperCase()}</span>
        <div style={{ minWidth: 0 }}>
          <div className="driver-plate selectable">
            <IconTruck size={20} /> {vehicle || t("common.noData", { defaultValue: "—" })}
          </div>
          <div className="driver-name">{session.name || t("home.driver", { defaultValue: "Driver" })}</div>
          <div className="driver-live">
            <span className={conn === "open" ? "on" : "off"}>
              <span className="live-dot" />
              {conn === "open"
                ? t("driverStatus.online", { defaultValue: "Online" })
                : t("driverStatus.offline", { defaultValue: "Offline" })}
            </span>
            <span className="muted">
              📍{" "}
              {gpsFresh
                ? t("driverStatus.gpsActive", { defaultValue: "GPS active" })
                : t("driverStatus.gpsSearching", { defaultValue: "GPS searching…" })}
            </span>
          </div>
        </div>
      </div>

      {/* One-tap prompt to turn on background alerts (congestion / re-route /
          parking / restricted-zone). Foreground toasts work without this; this
          unlocks lock-screen notifications when the app is backgrounded. */}
      {askNotif && (
        <div
          className="card"
          style={{ padding: 12, marginBottom: 12, display: "flex", alignItems: "center", gap: 12 }}
        >
          <span style={{ color: "var(--blue)", flex: "none" }}>
            <IconBell size={24} />
          </span>
          <div style={{ flex: 1, minWidth: 0, fontSize: 14 }}>
            {t("home.enableAlertsNote", {
              defaultValue: "Get alerts for congestion, re-routes and parking — even in the background.",
            })}
          </div>
          <button
            className="btn primary"
            style={{ width: "auto", padding: "10px 16px" }}
            onClick={enableAlerts}
          >
            {t("home.enableAlerts", { defaultValue: "Turn on" })}
          </button>
        </div>
      )}

      {/* CURRENT TRIP command card */}
      {!loaded && !truck ? (
        <SkeletonCard lines={4} />
      ) : (
        <div className="trip-card">
          <div className="trip-eyebrow">
            <span>{t("command.currentTrip", { defaultValue: "Current trip" })}</span>
            <GpsStatus at={fixAt} accuracyM={typeof rec.accuracy_m === "number" ? rec.accuracy_m : null} />
          </div>

          {gate ? (
            <div className="trip-dest">
              {t("command.headingTo", { defaultValue: "Heading to" })}&nbsp;<b>Gate {gate}</b>
            </div>
          ) : (
            <div className="trip-dest" style={{ fontSize: 18 }}>
              {t("command.noTrip", { defaultValue: "No active trip yet" })}
            </div>
          )}

          {/* Big driver-language status */}
          <div className="big-status" style={{ background: tone.bg }}>
            <span className="status-dot" style={{ background: tone.fg }} />
            <div>
              <div className="lab" style={{ color: tone.fg }}>
                {status.label}
              </div>
              {statusSub ? <div className="sub">{statusSub}</div> : null}
            </div>
            {traffic ? (
              <span className={`chip ${traffic.tone}`} style={{ marginLeft: "auto" }}>
                <span className="dot" />
                {t("traffic.label", { defaultValue: "Traffic" })}: {t(`traffic.${traffic.key}`)}
              </span>
            ) : null}
          </div>

          {/* Key metrics */}
          <div className="metric-row">
            <div className="metric">
              <div className="v">
                {etaMin != null ? Math.round(etaMin) : "—"}
                {etaMin != null ? <span className="u">min</span> : null}
              </div>
              <div className="k">{t("home.eta", { defaultValue: "ETA to gate" })}</div>
            </div>
            <div className="metric">
              <div className="v">
                {remainingKm != null ? Number(remainingKm).toFixed(1) : "—"}
                {remainingKm != null ? <span className="u">km</span> : null}
              </div>
              <div className="k">{t("home.distance", { defaultValue: "Distance" })}</div>
            </div>
            <div className="metric">
              <div className="v">
                {parkingFree != null ? parkingFree : "—"}
              </div>
              <div className="k">{t("home.parkingFree", { defaultValue: "Parking free" })}</div>
            </div>
          </div>

          <button className="nav-btn" onClick={() => navigate("/map")}>
            <IconNavigate size={20} /> {t("command.startNavigation", { defaultValue: "Start Navigation" })}
          </button>
          <div className="sub-actions">
            <button className="sub-action" onClick={() => navigate("/trip")}>
              <IconRoute size={18} /> {t("command.viewRoute", { defaultValue: "View Route" })}
            </button>
            <button className="sub-action" onClick={() => navigate("/parking")}>
              <IconParking size={18} /> {t("tabs.parking", { defaultValue: "Parking" })}
            </button>
          </div>
        </div>
      )}

      {session.status !== "ACTIVE" && (
        <button
          className="btn primary"
          style={{ width: "100%", marginBottom: 12 }}
          onClick={() => navigate("/enrol")}
        >
          {t("home.completeEnrol", { defaultValue: "Complete enrolment" })}
        </button>
      )}

      {/* Live map preview */}
      <div className="card" style={{ padding: 0, overflow: "hidden", marginBottom: 12 }}>
        <div style={{ padding: "12px 14px 0", fontSize: 13, fontWeight: 700, color: "var(--muted)" }}>
          {t("home.mapPreview", { defaultValue: "LIVE POSITION" })}
        </div>
        <div style={{ height: 180 }}>
          <MiniMap
            corridor={corridor}
            gates={gates}
            truck={truckPos}
            heading={typeof rec.heading === "number" ? rec.heading : null}
            targetGateId={rec.gate_id ?? null}
          />
        </div>
        <button
          className="btn ghost"
          style={{ width: "100%", borderRadius: 0 }}
          onClick={() => navigate("/map")}
        >
          {t("command.startNavigation", { defaultValue: "Start Navigation" })} ›
        </button>
      </div>
    </div>
  );
}
