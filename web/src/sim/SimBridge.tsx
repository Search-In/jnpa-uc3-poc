// SimBridge — invisible glue that makes the dashboard refetch the instant the
// simulator advances. The SimAdapter wrap (data/index.ts) already overlays sim
// overrides on every read, but React Query caches results; without a nudge the
// dashboard would only pick up changes on its 5s refetch interval. This mounts
// once (in App, above the router) and invalidates the sim-affected query keys
// whenever the sim state changes — so a slider drag or a scenario step shows on
// the live board immediately.

import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useSimDep } from "./useSimStore";

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

  useEffect(() => {
    for (const key of SIM_AFFECTED_KEYS) {
      // Prefix match so ["trucks", "AT_GATE_QUEUE"] and ["trucks", "live-map"]
      // are both invalidated by ["trucks"].
      void queryClient.invalidateQueries({ queryKey: key });
    }
  }, [dep, queryClient]);

  return null;
}
