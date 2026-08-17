import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useGatewaySocket } from "./useGatewaySocket";
import type { Alert, OperatorBanner, ScenarioStep, WsFrame } from "@/lib/types";
import { severityRank } from "@/lib/palette";
import { focusStore } from "@/lib/focusStore";
import { api } from "@/lib/api";

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
  // Latest operator-banner pushed by the gateway (fault-injection control
  // surface). The Demo Console reads this so a forced fault on one client lights
  // up the banner everywhere. Null until the first frame arrives.
  operatorBanner: OperatorBanner | null;
  subscribe: (fn: (f: WsFrame) => void) => () => void;
}

const Ctx = createContext<SocketCtx | null>(null);
const MAX_ALERTS = 100;

/**
 * Frames this provider deliberately does NOT act on (GAP-WS-02).
 *
 * Every one of these IS emitted by the gateway and was previously dropped by a
 * silent fall-through, which made "not handled" and "not noticed" look
 * identical. They stay unhandled here because each is already owned by the
 * screen that cares: Accidents, Road Bottlenecks, Camera AI, Double Trip, ECY
 * TRT and the TAS board each poll their own endpoint, and `reroute_ack` is
 * addressed to a single driver device rather than to this console.
 *
 * Listing them means an unrecognised frame now warns instead of disappearing —
 * if the gateway adds a type, this app says so rather than going quiet.
 *
 * `anpr` is absent on purpose: gateway/main.py builds `anpr_pump` with
 * `broadcast=False`, so ANPR reads are persisted and never sent to a socket.
 */
const IGNORED_FRAME_TYPES: ReadonlySet<string> = new Set([
  "hello",         // handshake, consumed by useGatewaySocket itself
  "traffic",       // the live map subscribes to raw frames directly
  "truck_position",// ditto
  "decision",      // Driver Advisory reads /api/decisions
  "violation_enforced", // Reports & Enforcement polls its own list
  "accident",
  "bottleneck",
  "camera_ai",
  "double_trip",
  "reroute_ack",
  "tas",
  "trt",
]);

export function SocketProvider({ children }: { children: ReactNode }) {
  const { status, subscribe } = useGatewaySocket();
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [scenarioSteps, setScenarioSteps] = useState<Record<string, ScenarioStep[]>>({});
  const [operatorBanner, setOperatorBanner] = useState<OperatorBanner | null>(null);
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
              (y.ts || "").localeCompare(x.ts || ""),
          );
        });
      } else if (frame.type === "scenario_step") {
        const s = frame.payload;
        setScenarioSteps((prev) => {
          const existing = prev[s.handle_id] ?? [];
          // de-dupe by step_no, keep ordered
          const merged = [...existing.filter((x) => x.step_no !== s.step_no), s].sort(
            (a, b) => a.step_no - b.step_no,
          );
          return { ...prev, [s.handle_id]: merged };
        });
      } else if (frame.type === "operator_banner") {
        // Latest-wins: the gateway pushes the full banner state on each change.
        setOperatorBanner(frame.payload);
      } else if (frame.type === "focus") {
        // A vessel/container/truck was selected in another app. applyRemote()
        // does NOT re-publish, so this cannot echo back and loop.
        const p = frame.payload;
        focusStore.applyRemote({
          vcn: p.vcn ?? undefined,
          viaNo: p.viaNo ?? undefined,
          imoNo: p.imoNo ?? undefined,
          vesselName: p.vesselName ?? undefined,
          containerNo: p.containerNo ?? undefined,
          vehicleNo: p.vehicleNo ?? undefined,
          igmNo: p.igmNo ?? undefined,
          fromDate: p.fromDate ?? undefined,
          toDate: p.toDate ?? undefined,
          asOf: p.asOf ?? undefined,
          origin: p.origin,
          nonce: p.nonce,
        });
      } else if (IGNORED_FRAME_TYPES.has(frame.type)) {
        // Deliberately not handled here — see IGNORED_FRAME_TYPES.
      } else {
        // A frame type nobody has accounted for. Not an error (the gateway may
        // ship a new one before this app learns it), but it must not vanish
        // without trace: an unhandled frame that looks identical to a handled
        // one is how "the live update stopped working" becomes unfindable.
        if (import.meta.env.DEV) {
          console.warn(
            `[ws] unhandled frame type "${(frame as { type: string }).type}" — ` +
              "add it to the WsFrame union in lib/types.ts and either handle it " +
              "here or list it in IGNORED_FRAME_TYPES.",
          );
        }
      }
    });
    return () => {
      unsubscribe();
    };
  }, [subscribe]);

  // Outbound half: carry a focus raised HERE to the other two dashboards. Fire
  // and forget — if the gateway is unreachable the local focus and the URL
  // grammar still work, which is the whole point of keeping those authoritative.
  useEffect(
    () =>
      focusStore.onPublish((f) => {
        void api.broadcastFocus(f).catch(() => {
          /* offline demo: local focus + deep link remain the source of truth */
        });
      }),
    [],
  );

  return (
    <Ctx.Provider value={{ status, alerts, scenarioSteps, operatorBanner, subscribe }}>
      {children}
    </Ctx.Provider>
  );
}

export function useSocket(): SocketCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useSocket must be used within SocketProvider");
  return v;
}
