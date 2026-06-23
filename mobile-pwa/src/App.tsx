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
import { ensureDeviceToken } from "@/lib/api";
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
