// Unified Launcher (UC-3 audit P2 Task 10) — a single "JNPA Digital Twin
// Platform" entry point listing the three use-case twins with a SHARED
// DATA_MODE. Rendered outside the dashboard Shell (like the Simulator) so it
// reads as the platform front door. Honest about wiring: UC-3 is this app;
// UC-2 is a separate twin reached via the cross-twin link; UC-1 is out of scope
// for this repository.

import { useNavigate } from "react-router-dom";
import { ArrowRight, Ship, Truck, Boxes } from "lucide-react";
import { DATA_MODE } from "@/data";
import { STATUS } from "@/lib/tokens";

type UcStatus = "active" | "external" | "planned";

interface Uc {
  id: string;
  title: string;
  subtitle: string;
  desc: string;
  icon: typeof Ship;
  status: UcStatus;
  to?: string; // internal route when active
  href?: string; // external twin when known
}

const UCS: Uc[] = [
  {
    id: "UC-1",
    title: "Vessel & Berth",
    subtitle: "Marine operations twin",
    desc: "Vessel scheduling, berth allocation and marine-side optimisation. Not deployed in this repository.",
    icon: Ship,
    status: "planned",
  },
  {
    id: "UC-2",
    title: "Cargo & DPD",
    subtitle: "Cargo / yard twin",
    desc: "Discharge, yard moves and Direct-Port-Delivery release. Separate twin; linked to UC-3 via the cargo.dpd_release cross-twin event.",
    icon: Boxes,
    status: "external",
  },
  {
    id: "UC-3",
    title: "Traffic & Decongestion",
    subtitle: "Corridor / gate twin",
    desc: "ANPR, congestion forecasting, gate/queue management, driver advisory and carbon impact. This application.",
    icon: Truck,
    status: "active",
    to: "/command-center",
  },
];

function statusMeta(s: UcStatus): { label: string; color: string } {
  if (s === "active") return { label: "Active", color: STATUS.ok };
  if (s === "external") return { label: "External twin", color: STATUS.info };
  return { label: "Not in scope", color: STATUS.unknown };
}

export default function Launcher() {
  const navigate = useNavigate();

  return (
    <div className="min-h-screen bg-background px-4 py-10 text-foreground">
      <div className="mx-auto max-w-5xl">
        <header className="mb-8 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">JNPA Digital Twin Platform</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              Unified entry point — select a use-case twin. Data mode is shared across the platform.
            </p>
          </div>
          <span
            className="rounded-md px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wide"
            style={{
              color: DATA_MODE === "live" ? STATUS.ok : STATUS.warning,
              backgroundColor: (DATA_MODE === "live" ? STATUS.ok : STATUS.warning) + "22",
            }}
            title="Shared DATA_MODE across all use cases"
          >
            data mode · {DATA_MODE}
          </span>
        </header>

        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          {UCS.map((uc) => {
            const meta = statusMeta(uc.status);
            const clickable = uc.status === "active" && uc.to;
            const Icon = uc.icon;
            return (
              <button
                key={uc.id}
                disabled={!clickable}
                onClick={() => clickable && navigate(uc.to!)}
                className={`flex flex-col rounded-xl border border-border bg-card p-5 text-left transition ${
                  clickable
                    ? "cursor-pointer hover:border-primary hover:shadow-md"
                    : "cursor-default opacity-80"
                }`}
              >
                <div className="mb-3 flex items-center justify-between">
                  <Icon size={22} style={{ color: meta.color }} />
                  <span
                    className="rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide"
                    style={{ color: meta.color, backgroundColor: meta.color + "22" }}
                  >
                    {meta.label}
                  </span>
                </div>
                <div className="text-[11px] font-semibold text-muted-foreground">{uc.id}</div>
                <div className="text-base font-semibold">{uc.title}</div>
                <div className="text-[12px] text-muted-foreground">{uc.subtitle}</div>
                <p className="mt-2 flex-1 text-[12px] leading-snug text-muted-foreground">
                  {uc.desc}
                </p>
                {clickable && (
                  <div className="mt-3 inline-flex items-center gap-1 text-[13px] font-semibold text-primary">
                    Open <ArrowRight size={14} />
                  </div>
                )}
              </button>
            );
          })}
        </div>

        <p className="mt-6 text-[11px] leading-snug text-muted-foreground">
          UC-3 is the deployed application in this repository. UC-2 runs as a separate twin and is
          linked to UC-3 through the{" "}
          <code className="rounded bg-muted px-1">cargo.dpd_release</code> cross-twin event (see the
          What-If Console TFC-3 scenario and Follow-the-Box). UC-1 is shown for platform
          completeness and is not part of this PoC.
        </p>
      </div>
    </div>
  );
}
