import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  HashRouter,
  Navigate,
  NavLink,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { getPairing, setPairing } from "@/lib/device";
import { ensureDeviceToken, api } from "@/lib/api";
import { RealtimeProvider, useRealtime } from "@/hooks/RealtimeContext";
import { DriverSessionProvider } from "@/hooks/DriverSession";
import Pairing from "@/screens/Pairing";
import Home from "@/screens/Home";
import Trip from "@/screens/Trip";
import Reroute from "@/screens/Reroute";
import Inbox from "@/screens/Inbox";
import Profile from "@/screens/Profile";
import Enrol from "@/screens/Enrol";
import Parking from "@/screens/Parking";
import Zones from "@/screens/Zones";
import MapView from "@/screens/MapView";
import AlertCenter from "@/screens/AlertCenter";

// Uses hash routing so the PWA works under /pwa from nginx (no server rewrite
// rules needed) and deep-links from the service worker (#/reroute) just work.

function TabBar() {
  const { t } = useTranslation();
  const { unread, pendingReroute } = useRealtime();
  // Production 6-tab bottom navigation (Phase 3): Home · Trip · Map · Parking ·
  // Alerts · Profile. Reroute/Inbox/Enrol/Zones remain reachable as routes.
  const tabs = [
    { to: "/home", label: t("tabs.home", { defaultValue: "Home" }), icon: "🏠" },
    { to: "/trip", label: t("tabs.trip"), icon: "🛣", alert: !!pendingReroute },
    { to: "/map", label: t("tabs.map", { defaultValue: "Map" }), icon: "🗺" },
    { to: "/parking", label: t("tabs.parking"), icon: "🅿" },
    { to: "/alerts", label: t("tabs.alerts", { defaultValue: "Alerts" }), icon: "🔔", badge: unread },
    { to: "/profile", label: t("tabs.vehicle"), icon: "🚛" },
  ];
  return (
    <nav className="tabbar">
      {tabs.map((t) => (
        <NavLink key={t.to} to={t.to} className={({ isActive }) => (isActive ? "active" : "")}>
          <span style={{ fontSize: 18, color: t.alert ? "var(--blue)" : undefined }}>{t.icon}</span>
          <span>{t.label}</span>
          {t.badge ? <span className="badge-dot">{t.badge > 9 ? "9+" : t.badge}</span> : null}
        </NavLink>
      ))}
    </nav>
  );
}

function TopBar() {
  const { t } = useTranslation();
  const { status } = useRealtime();
  const loc = useLocation();
  const navigate = useNavigate();
  const onHome = loc.pathname === "/home";
  const titleKey =
    {
      "/home": "screens.home",
      "/trip": "screens.trip",
      "/map": "screens.map",
      "/alerts": "screens.alerts",
      "/parking": "screens.parking",
      "/zones": "screens.zones",
      "/reroute": "screens.reroute",
      "/inbox": "screens.inbox",
      "/enrol": "screens.enrol",
      "/profile": "screens.vehicle",
    }[loc.pathname] || "screens.trip";
  return (
    <header className="topbar">
      <div className="topbar-title">
        {!onHome && (
          <button
            className="topbar-home"
            aria-label={t("screens.home")}
            onClick={() => navigate("/home")}
          >
            ‹
          </button>
        )}
        <div>
          <h1>{t(titleKey)}</h1>
          <div className="sub">{t("app.subtitle")}</div>
        </div>
      </div>
      <span style={{ fontSize: 11, color: status === "open" ? "var(--green)" : "var(--muted)" }}>
        {status === "open" ? "● " + t("common.live").toLowerCase() : "○ " + status}
      </span>
    </header>
  );
}

// When a re-route lands, full-screen the confirmation regardless of current tab.
function RerouteInterrupt() {
  const { pendingReroute } = useRealtime();
  const navigate = useNavigate();
  const loc = useLocation();
  useEffect(() => {
    if (pendingReroute && loc.pathname !== "/reroute") navigate("/reroute");
  }, [pendingReroute, loc.pathname, navigate]);
  return null;
}

function PairedApp({ deviceId, plate }: { deviceId: string; plate?: string | null }) {
  return (
    <RealtimeProvider deviceId={deviceId} plate={plate}>
      <DriverSessionProvider deviceId={deviceId} plate={plate}>
        <HashRouter>
          <div className="app-shell">
            <RerouteInterrupt />
            <TopBar />
            <main className="content">
              <Routes>
                <Route path="/home" element={<Home deviceId={deviceId} plate={plate} />} />
                <Route path="/trip" element={<Trip deviceId={deviceId} />} />
                <Route path="/parking" element={<Parking deviceId={deviceId} plate={plate} />} />
                <Route path="/map" element={<MapView deviceId={deviceId} />} />
                <Route path="/alerts" element={<AlertCenter />} />
                <Route path="/zones" element={<Zones deviceId={deviceId} plate={plate} />} />
                <Route path="/reroute" element={<Reroute />} />
                <Route path="/inbox" element={<Inbox />} />
                <Route path="/enrol" element={<Enrol deviceId={deviceId} plate={plate} />} />
                <Route path="/profile" element={<Profile deviceId={deviceId} plate={plate} />} />
                <Route path="*" element={<Navigate to="/home" replace />} />
              </Routes>
            </main>
            <TabBar />
          </div>
        </HashRouter>
      </DriverSessionProvider>
    </RealtimeProvider>
  );
}

type BootState = "pending" | "ready" | "auth-failed";

export default function App() {
  const [pairing, setPairingState] = useState(() => getPairing());
  // Acquire the DRIVER token BEFORE mounting the authed shell so the first API
  // calls (Trip/Profile) and the WebSocket carry a bearer when AUTH_ENABLED=true.
  // Critically, we mount the shell only once a token is actually obtained — never
  // merely because the mint attempt *completed*. Mounting without a token would
  // spam /api 401s and the WS handshake would be rejected ("Not enough segments").
  const [boot, setBoot] = useState<BootState>("pending");

  useEffect(() => {
    if (!pairing) return;
    let alive = true;
    let attempt = 0;
    const acquire = async () => {
      while (alive) {
        const ok = await ensureDeviceToken(pairing.deviceId);
        if (ok) {
          // Resolve the vehicle plate this device is bound to (robust to the
          // plate-less SECONDARY truck response) so Home/Parking/Profile show it.
          if (!pairing.plate) {
            try {
              const plate = await api.truckPlate(pairing.deviceId);
              if (plate && alive) {
                setPairing(pairing.deviceId, plate);
                setPairingState({ deviceId: pairing.deviceId, plate });
              }
            } catch {
              /* plate stays null — screens degrade gracefully */
            }
          }
          if (alive) setBoot("ready");
          return;
        }
        // In development the gateway typically runs with auth disabled (no token
        // needed), so proceed rather than block the local demo.
        if (import.meta.env.DEV) {
          if (alive) setBoot("ready");
          return;
        }
        // Production with a failed mint: do NOT open the authed shell. Surface the
        // state and retry with capped backoff so the app self-heals once the token
        // becomes obtainable (e.g. transient gateway blip) instead of 401-storming.
        if (alive) setBoot("auth-failed");
        attempt += 1;
        const delay = Math.min(1000 * 2 ** attempt, 15_000);
        await new Promise((r) => setTimeout(r, delay));
      }
    };
    void acquire();
    return () => {
      alive = false;
    };
  }, [pairing]);

  if (!pairing) {
    return (
      <div className="app-shell">
        <Pairing onPaired={(deviceId) => setPairingState({ deviceId })} />
      </div>
    );
  }
  if (boot === "auth-failed") {
    return (
      <div className="app-shell" style={{ padding: 24, textAlign: "center" }}>
        <p>Authorizing this device…</p>
        <p className="muted" style={{ fontSize: 13 }}>
          Could not obtain a session token. Retrying — check connectivity.
        </p>
      </div>
    );
  }
  if (boot === "pending") {
    return <div className="app-shell" aria-busy="true" />;
  }
  return <PairedApp deviceId={pairing.deviceId} plate={pairing.plate} />;
}
