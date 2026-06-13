import { NavLink } from "react-router-dom";
import {
  Activity,
  Route as RouteIcon,
  Shapes,
  FileText,
  HeartPulse,
  FlaskConical,
} from "lucide-react";
import { cn } from "@/lib/utils";

export const NAV = [
  { to: "/live", label: "Live Operations", icon: Activity },
  { to: "/advisory", label: "Driver Advisory", icon: RouteIcon },
  { to: "/geofencing", label: "Geo-fencing Manager", icon: Shapes },
  { to: "/reports", label: "Traffic-Police Reports", icon: FileText },
  { to: "/health", label: "System Health", icon: HeartPulse },
  { to: "/what-if", label: "What-If Console", icon: FlaskConical },
] as const;

export function Sidebar() {
  return (
    <nav
      aria-label="Primary"
      className="flex w-60 shrink-0 flex-col border-r border-border bg-card/50"
    >
      <div className="flex items-center gap-2 border-b border-border px-4 py-4">
        <div className="grid h-8 w-8 place-items-center rounded-md bg-primary text-primary-foreground font-bold">
          J
        </div>
        <div className="leading-tight">
          <div className="text-sm font-semibold">JNPA UC-III</div>
          <div className="text-[11px] text-muted-foreground">Traffic Control Room</div>
        </div>
      </div>
      <ul className="flex flex-col gap-1 p-2">
        {NAV.map(({ to, label, icon: Icon }) => (
          <li key={to}>
            <NavLink
              to={to}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
                  isActive
                    ? "bg-primary/15 text-primary"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground"
                )
              }
            >
              <Icon className="h-4 w-4 shrink-0" aria-hidden />
              <span>{label}</span>
            </NavLink>
          </li>
        ))}
      </ul>
      <div className="mt-auto p-3 text-[11px] text-muted-foreground">
        NH-348 · JNPA → Karal Phata
      </div>
    </nav>
  );
}
