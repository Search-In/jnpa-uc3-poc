import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useGatewaySocket } from "./useGatewaySocket";
import type { Alert, ScenarioStep, WsFrame } from "@/lib/types";
import { severityRank } from "@/lib/palette";

// App-wide socket context: one /api/ws connection, a rolling buffer of the most
// recent alerts (so any screen can show "live alerts" without re-subscribing),
// and a passthrough subscribe() for screens that need raw frames (the live map
// listens for truck_position / traffic).

interface SocketCtx {
  status: "connecting" | "open" | "closed";
  alerts: Alert[];
  // Live scenario steps keyed by handle_id, ordered by step_no (the What-If
  // storyline). Survives navigation between screens while the socket stays up.
  scenarioSteps: Record<string, ScenarioStep[]>;
  subscribe: (fn: (f: WsFrame) => void) => () => void;
}

const Ctx = createContext<SocketCtx | null>(null);
const MAX_ALERTS = 100;

export function SocketProvider({ children }: { children: ReactNode }) {
  const { status, subscribe } = useGatewaySocket();
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [scenarioSteps, setScenarioSteps] = useState<Record<string, ScenarioStep[]>>({});
  const seen = useRef<Set<string>>(new Set());

  useEffect(() => {
    const unsubscribe = subscribe((frame) => {
      if (frame.type === "alert") {
        const a = frame.payload;
        if (a.id && seen.current.has(a.id)) return;
        if (a.id) seen.current.add(a.id);
        setAlerts((prev) => {
          const next = [a, ...prev].slice(0, MAX_ALERTS);
          // newest first, but bubble criticals up within the same recency window
          return next.sort(
            (x, y) =>
              severityRank(y.severity) - severityRank(x.severity) ||
              (y.ts || "").localeCompare(x.ts || "")
          );
        });
      } else if (frame.type === "scenario_step") {
        const s = frame.payload;
        setScenarioSteps((prev) => {
          const existing = prev[s.handle_id] ?? [];
          // de-dupe by step_no, keep ordered
          const merged = [...existing.filter((x) => x.step_no !== s.step_no), s].sort(
            (a, b) => a.step_no - b.step_no
          );
          return { ...prev, [s.handle_id]: merged };
        });
      }
    });
    return () => {
      unsubscribe();
    };
  }, [subscribe]);

  return (
    <Ctx.Provider value={{ status, alerts, scenarioSteps, subscribe }}>{children}</Ctx.Provider>
  );
}

export function useSocket(): SocketCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useSocket must be used within SocketProvider");
  return v;
}
