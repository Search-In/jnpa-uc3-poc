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
import { getPairing } from "@/lib/device";
import { RealtimeProvider, useRealtime } from "@/hooks/RealtimeContext";
import Pairing from "@/screens/Pairing";
import Trip from "@/screens/Trip";
import Reroute from "@/screens/Reroute";
import Inbox from "@/screens/Inbox";
import Profile from "@/screens/Profile";

// Uses hash routing so the PWA works under /pwa from nginx (no server rewrite
// rules needed) and deep-links from the service worker (#/reroute) just work.

function TabBar() {
  const { t } = useTranslation();
  const { unread, pendingReroute } = useRealtime();
  const tabs = [
    { to: "/trip", label: t("tabs.trip"), icon: "🛣" },
    { to: "/reroute", label: t("tabs.reroute"), icon: "↻", alert: !!pendingReroute },
    { to: "/inbox", label: t("tabs.inbox"), icon: "✉", badge: unread },
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
  const titleKey =
    {
      "/trip": "screens.trip",
      "/reroute": "screens.reroute",
      "/inbox": "screens.inbox",
      "/profile": "screens.vehicle",
    }[loc.pathname] || "screens.trip";
  return (
    <header className="topbar">
      <div>
        <h1>{t(titleKey)}</h1>
        <div className="sub">{t("app.subtitle")}</div>
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
      <HashRouter>
        <div className="app-shell">
          <RerouteInterrupt />
          <TopBar />
          <main className="content">
            <Routes>
              <Route path="/trip" element={<Trip deviceId={deviceId} />} />
              <Route path="/reroute" element={<Reroute />} />
              <Route path="/inbox" element={<Inbox />} />
              <Route path="/profile" element={<Profile deviceId={deviceId} plate={plate} />} />
              <Route path="*" element={<Navigate to="/trip" replace />} />
            </Routes>
          </main>
          <TabBar />
        </div>
      </HashRouter>
    </RealtimeProvider>
  );
}

export default function App() {
  const [pairing, setPairingState] = useState(() => getPairing());

  if (!pairing) {
    return (
      <div className="app-shell">
        <Pairing onPaired={(deviceId) => setPairingState({ deviceId })} />
      </div>
    );
  }
  return <PairedApp deviceId={pairing.deviceId} plate={pairing.plate} />;
}
