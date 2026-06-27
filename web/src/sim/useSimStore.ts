// React bindings for the simStore. `useSimStore()` subscribes a component to
// the whole sim state via useSyncExternalStore, so panels and the map re-render
// the instant the Simulator page (this tab or another) pushes an update.

import { useSyncExternalStore } from "react";
import { simStore, type SimState } from "./simStore";

export function useSimStore(): SimState {
  return useSyncExternalStore(simStore.subscribe, simStore.getState, simStore.getState);
}

/**
 * A single dependency string a consumer can use to react whenever the simulator
 * advances (tick) or any lever changes — e.g. to invalidate React Query keys.
 */
export function useSimDep(): string {
  const s = useSimStore();
  return [
    s.tick,
    s.flowRate,
    s.vehicleInjection,
    s.scanQueue,
    s.parkingDelta,
    JSON.stringify(s.gates),
    JSON.stringify(s.segments),
    // status signature (not just length) so resolving an incident — which keeps
    // it in the list but flips OPEN→RESOLVED — still triggers a dashboard refetch.
    s.incidents.map((i) => i.status[0]).join(""),
    s.incidents.length,
    `${s.tour.scenarioId}:${s.tour.stepIndex}`,
  ].join("|");
}

/** True if the sim currently overrides anything (used to badge the dashboard). */
export function hasSimOverrides(s: SimState): boolean {
  return (
    s.running ||
    s.tour.scenarioId !== null ||
    Object.keys(s.gates).length > 0 ||
    Object.keys(s.segments).length > 0 ||
    s.flowRate !== 1 ||
    s.vehicleInjection > 0 ||
    s.scanQueue !== null ||
    s.parkingDelta !== 0 ||
    s.incidents.length > 0
  );
}
