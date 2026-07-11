// SimBridge — invisible glue that makes the dashboard refetch the instant the
// simulator advances. The SimAdapter wrap (data/index.ts) already overlays sim
// overrides on every read, but React Query caches results; without a nudge the
// dashboard would only pick up changes on its 5s refetch interval. This mounts
// once (in App, above the router) and invalidates the sim-affected query keys
// whenever the sim state changes — so a slider drag or a scenario step shows on
// the live board immediately.

import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useSimDep } from "./useSimStore";

// Coalesce invalidations. The sim tick engine wobbles gate/segment values every
// second (and a slider drag emits many changes in a burst), so an unthrottled
// bridge would fire a 10-key refetch cascade ~1×/s — a network storm in live
// mode that keeps the KPI strip and refresh spinner perpetually "loading". We
// throttle to at most one pass per window, always running a trailing pass so the
// board still settles on the final state.
const THROTTLE_MS = 1500;

// Query keys whose data the SimAdapter overlays (see data/index.ts + the
// getAdapter() usages in the dashboard screens/panels).
const SIM_AFFECTED_KEYS: (readonly unknown[])[] = [
  ["gates"],
  ["snapshots"],
  ["trucks"],
  ["traffic-predict"],
  ["alerts-seed"],
  ["kpi-strip"],
  ["kpi"], // ThroughputChart trend (["kpi", "throughput-trend"])
  ["parking-availability"],
  ["police"], // Traffic-Police Reports (["police", filters])
  ["tas-slots"], // TAS widget (["tas-slots", gateId])
];

export function SimBridge() {
  const queryClient = useQueryClient();
  const dep = useSimDep();
  const lastRunRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const invalidate = () => {
      lastRunRef.current = Date.now();
      for (const key of SIM_AFFECTED_KEYS) {
        // Prefix match so ["trucks", "AT_GATE_QUEUE"] and ["trucks", "live-map"]
        // are both invalidated by ["trucks"]. refetchType:"active" so only
        // mounted panels refetch — offscreen keys just go stale.
        void queryClient.invalidateQueries({ queryKey: key, refetchType: "active" });
      }
    };

    const sinceLast = Date.now() - lastRunRef.current;
    if (sinceLast >= THROTTLE_MS) {
      invalidate(); // leading edge: reflect the change immediately
    } else {
      // Trailing edge: fold this change into a single pass at the window's end.
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(invalidate, THROTTLE_MS - sinceLast);
    }

    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [dep, queryClient]);

  return null;
}
