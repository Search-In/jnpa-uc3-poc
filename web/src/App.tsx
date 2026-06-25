import { useState } from "react";
import { Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { Shell } from "@/components/layout/Shell";
import { useScenario } from "@/hooks/ScenarioContext";
import { authEnabled, canSeeScreen, getRole, type Role } from "@/lib/auth";
import { LoginGate } from "@/components/auth/LoginGate";
import LiveOperations from "@/screens/LiveOperations";
import DriverAdvisory from "@/screens/DriverAdvisory";
import GeofencingManager from "@/screens/GeofencingManager";
import PoliceReports from "@/screens/PoliceReports";
import SystemHealth from "@/screens/SystemHealth";
import WhatIfConsole from "@/screens/WhatIfConsole";
import DemoConsole from "@/screens/DemoConsole";
import { GuidedTour } from "@/whatif/GuidedTour";

/** Guard a screen by role. When auth is disabled (demo build) it always renders;
 *  when enabled, an out-of-role screen redirects to Live Operations (which every
 *  role can see). This is defence-in-depth alongside the role-filtered nav — the
 *  gateway still enforces 403 on the data regardless of the UI. */
function Guard({ path, children }: { path: string; children: React.ReactNode }) {
  if (canSeeScreen(path)) return <>{children}</>;
  return <Navigate to="/live" replace />;
}

export default function App() {
  const { scenario, reset } = useScenario();
  const navigate = useNavigate();
  const [role, setRole] = useState<Role | null>(getRole());

  // Auth-enabled build with no session yet -> show the login gate (never mounted
  // in the default demo/mock build, where authEnabled() is false).
  if (authEnabled() && !role) {
    return <LoginGate onAuthed={(r) => setRole(r)} />;
  }

  return (
    <Shell onResetBaseline={reset} resetDisabled={scenario === "none"}>
      <main className="min-h-0 flex-1 overflow-hidden" style={{ height: "100%" }}>
        <Routes>
          <Route path="/" element={<Navigate to="/live" replace />} />
          <Route path="/live" element={<LiveOperations />} />
          <Route
            path="/advisory"
            element={
              <Guard path="/advisory">
                <DriverAdvisory />
              </Guard>
            }
          />
          <Route
            path="/geofencing"
            element={
              <Guard path="/geofencing">
                <GeofencingManager />
              </Guard>
            }
          />
          <Route
            path="/reports"
            element={
              <Guard path="/reports">
                <PoliceReports />
              </Guard>
            }
          />
          <Route
            path="/health"
            element={
              <Guard path="/health">
                <SystemHealth />
              </Guard>
            }
          />
          <Route
            path="/what-if"
            element={
              <Guard path="/what-if">
                <WhatIfConsole />
              </Guard>
            }
          />
          {/* /whatif alias (verification cmd: open http://localhost:3000/whatif) */}
          <Route
            path="/whatif"
            element={
              <Guard path="/whatif">
                <WhatIfConsole />
              </Guard>
            }
          />
          <Route
            path="/demo"
            element={
              <Guard path="/demo">
                <DemoConsole />
              </Guard>
            }
          />
          <Route path="*" element={<Navigate to="/live" replace />} />
        </Routes>
      </main>

      {/* Guided What-If runtime — mounted once, above the router outlet, so it
          survives view changes. It switches the visible view per scenario step
          via onView (the host-owned view switcher), exactly like the reference's
          Dashboard passing onTab={setActiveTab}. Here the host owns routes, so
          onView is navigate. Renders nothing unless a guided scenario is active. */}
      <GuidedTour onView={(view) => navigate(view)} />
    </Shell>
  );
}
