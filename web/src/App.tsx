import { useState } from "react";
import { Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { Shell } from "@/components/layout/Shell";
import { useScenario } from "@/hooks/ScenarioContext";
import { authEnabled, canSeeScreen, getRole, type Role } from "@/lib/auth";
import { LoginGate } from "@/components/auth/LoginGate";
import CommandCenter from "@/screens/CommandCenter";
import AlertsCenter from "@/screens/AlertsCenter";
import LiveOperations from "@/screens/LiveOperations";
import DriverAdvisory from "@/screens/DriverAdvisory";
import GeoAnalytics from "@/screens/GeoAnalytics";
import PoliceReports from "@/screens/PoliceReports";
import Fastag from "@/screens/Fastag";
import GateCustoms from "@/screens/GateCustoms";
import Intelligence from "@/screens/Intelligence";
import FollowTheBox from "@/screens/FollowTheBox";
import ParkingManagement from "@/screens/ParkingManagement";
import SystemHealth from "@/screens/SystemHealth";
import WhatIfConsole from "@/screens/WhatIfConsole";
import DemoConsole from "@/screens/DemoConsole";
import DriverEnrollments from "@/screens/DriverEnrollments";
import VehicleManagement from "@/screens/VehicleManagement";
import CfsEcyMovements from "@/screens/CfsEcyMovements";
import ShippingLines from "@/screens/ShippingLines";
import Berthing from "@/screens/berthing/Berthing";
import PerformanceReports from "@/screens/PerformanceReports";
import WorkflowComposer from "@/screens/WorkflowComposer";
import SimulatorPage from "@/sim/SimulatorPage";
import Launcher from "@/screens/Launcher";
import { SimBridge } from "@/sim/SimBridge";
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
    <>
      <Routes>
        {/* Standalone, full-page Simulator — rendered OUTSIDE the dashboard Shell
            (no left nav / app header) so it feels like a separate application,
            exactly like the jnpa_poc_2 reference. It owns its own CalciteShell +
            navigation header. */}
        <Route
          path="/simulator"
          element={
            <Guard path="/simulator">
              <SimulatorPage />
            </Guard>
          }
        />

        {/* Unified Launcher — platform front door, rendered outside the Shell. */}
        <Route path="/launcher" element={<Launcher />} />

        {/* Everything else lives inside the dashboard Shell. */}
        <Route
          path="*"
          element={<DashboardShell scenario={scenario} reset={reset} navigate={navigate} />}
        />
      </Routes>

      {/* Simulator → dashboard bridge: invalidates the sim-affected React Query
          keys whenever a slider/scenario changes the sim state, so the live
          board reflects the simulator immediately. Mounted at the top level so
          it stays active for the dashboard tab regardless of the active route.
          Renders nothing. */}
      <SimBridge />
    </>
  );
}

/** The dashboard application shell + its routed screens. Separated from the
 *  standalone Simulator so `/simulator` can render without this chrome. */
function DashboardShell({
  scenario,
  reset,
  navigate,
}: {
  scenario: string;
  reset: () => void;
  navigate: (to: string) => void;
}) {
  return (
    <Shell onResetBaseline={reset} resetDisabled={scenario === "none"}>
      <main className="min-h-0 flex-1 overflow-hidden" style={{ height: "100%" }}>
        <Routes>
          <Route path="/" element={<Navigate to="/command-center" replace />} />
          <Route
            path="/command-center"
            element={
              <Guard path="/command-center">
                <CommandCenter />
              </Guard>
            }
          />
          <Route
            path="/alerts"
            element={
              <Guard path="/alerts">
                <AlertsCenter />
              </Guard>
            }
          />
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
                <GeoAnalytics defaultTab="zones" />
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
            path="/fastag"
            element={
              <Guard path="/fastag">
                <Fastag />
              </Guard>
            }
          />
          <Route
            path="/gate-customs"
            element={
              <Guard path="/gate-customs">
                <GateCustoms />
              </Guard>
            }
          />
          <Route
            path="/intelligence"
            element={
              <Guard path="/intelligence">
                <Intelligence />
              </Guard>
            }
          />
          <Route
            path="/follow-the-box"
            element={
              <Guard path="/follow-the-box">
                <FollowTheBox />
              </Guard>
            }
          />
          <Route
            path="/parking"
            element={
              <Guard path="/parking">
                <ParkingManagement />
              </Guard>
            }
          />
          <Route
            path="/geofence-events"
            element={
              <Guard path="/geofence-events">
                <GeoAnalytics defaultTab="events" />
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
            path="/workflows"
            element={
              <Guard path="/workflows">
                <WorkflowComposer />
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
          <Route
            path="/enrollments"
            element={
              <Guard path="/enrollments">
                <DriverEnrollments />
              </Guard>
            }
          />
          <Route
            path="/vehicles"
            element={
              <Guard path="/vehicles">
                <VehicleManagement />
              </Guard>
            }
          />
          <Route
            path="/cfs-ecy"
            element={
              <Guard path="/cfs-ecy">
                <CfsEcyMovements />
              </Guard>
            }
          />
          <Route
            path="/shipping-lines"
            element={
              <Guard path="/shipping-lines">
                <ShippingLines />
              </Guard>
            }
          />
          <Route
            path="/berthing"
            element={
              <Guard path="/berthing">
                <Berthing />
              </Guard>
            }
          />
          <Route
            path="/performance"
            element={
              <Guard path="/performance">
                <PerformanceReports />
              </Guard>
            }
          />
          {/* --- UC-III features now live inside their host screens; the old
              standalone routes redirect into the host (+tab) so deep-links and
              Command-Center/Demo shortcuts keep resolving (no sidebar entry). --- */}
          <Route path="/accidents" element={<Navigate to="/alerts?tab=accidents" replace />} />
          <Route
            path="/transporters"
            element={<Navigate to="/vehicles?tab=transporters" replace />}
          />
          <Route path="/camera-ai" element={<Navigate to="/gate-customs" replace />} />
          <Route path="/document-ocr" element={<Navigate to="/follow-the-box" replace />} />
          <Route path="/nvr" element={<Navigate to="/health" replace />} />
          <Route path="/trt" element={<Navigate to="/live?tab=trt" replace />} />
          <Route path="/bottlenecks" element={<Navigate to="/geofencing" replace />} />
          <Route path="/reefer" element={<Navigate to="/parking" replace />} />
          <Route path="/integrations" element={<Navigate to="/health" replace />} />
          <Route path="/double-trip" element={<Navigate to="/live?tab=double-trip" replace />} />
          <Route path="*" element={<Navigate to="/command-center" replace />} />
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
