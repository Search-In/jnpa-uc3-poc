import { Navigate, Route, Routes } from "react-router-dom";
import { Sidebar } from "@/components/layout/Sidebar";
import { Header } from "@/components/layout/Header";
import LiveOperations from "@/screens/LiveOperations";
import DriverAdvisory from "@/screens/DriverAdvisory";
import GeofencingManager from "@/screens/GeofencingManager";
import PoliceReports from "@/screens/PoliceReports";
import SystemHealth from "@/screens/SystemHealth";
import WhatIfConsole from "@/screens/WhatIfConsole";

export default function App() {
  return (
    <div className="flex h-full w-full overflow-hidden">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <Header />
        <main className="min-h-0 flex-1 overflow-hidden">
          <Routes>
            <Route path="/" element={<Navigate to="/live" replace />} />
            <Route path="/live" element={<LiveOperations />} />
            <Route path="/advisory" element={<DriverAdvisory />} />
            <Route path="/geofencing" element={<GeofencingManager />} />
            <Route path="/reports" element={<PoliceReports />} />
            <Route path="/health" element={<SystemHealth />} />
            <Route path="/what-if" element={<WhatIfConsole />} />
            {/* /whatif alias (verification cmd: open http://localhost:3000/whatif) */}
            <Route path="/whatif" element={<WhatIfConsole />} />
            <Route path="*" element={<Navigate to="/live" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
