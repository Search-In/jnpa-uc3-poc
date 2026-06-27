// SimAdapter — a transparent wrapper around the real DataAdapter (mock or live)
// that overlays the current simStore overrides onto every sim-affected read.
// Because the whole UI binds to getAdapter(), wrapping it here means *every*
// screen — Live Operations tiles, the map, the KPI strip, the alerts drawer —
// reflects the live sim state automatically, with no per-panel changes.
//
// It reads simStore.getState() at call time, so each React Query refetch picks
// up the latest overrides; SimBridge invalidates the relevant query keys when
// the sim advances so updates feel live. Writes/scenarios pass straight through.
//
// Implemented as a Proxy so the wrapper never drifts as the (large) DataAdapter
// interface grows — only the sim-affected methods are overridden; everything
// else is delegated to the base adapter unchanged.

import type { DataAdapter } from "@/data";
import { simStore } from "./simStore";
import {
  applyAlerts,
  applyGates,
  applyKpis,
  applyParking,
  applyPoliceReport,
  applyPredict,
  applySnapshots,
  applyTas,
  applyTrucks,
} from "./applySim";

/** Wrap a base adapter so sim overrides overlay every relevant read. */
export function makeSimAdapter(base: DataAdapter): DataAdapter {
  // The overrides each read the live sim state at call time and overlay the
  // base adapter's result. Keep these aligned with applySim.ts.
  const overrides: Partial<DataAdapter> = {
    async gates() {
      return applyGates(await base.gates(), simStore.getState());
    },
    async trafficSnapshots() {
      return applySnapshots(await base.trafficSnapshots(), simStore.getState());
    },
    async trucks(state?: string, limit?: number) {
      return applyTrucks(await base.trucks(state, limit), simStore.getState(), state);
    },
    async trafficPredict(horizon?: number) {
      return applyPredict(await base.trafficPredict(horizon), simStore.getState());
    },
    async alerts(params?: { since?: string; kind?: string; limit?: number }) {
      return applyAlerts(await base.alerts(params), simStore.getState());
    },
    async kpiStrip() {
      return applyKpis(await base.kpiStrip(), simStore.getState());
    },
    async parkingAvailability(minuteOfDay?: number) {
      return applyParking(await base.parkingAvailability(minuteOfDay), simStore.getState());
    },
    async policeReport(params?: Record<string, string | undefined>) {
      return applyPoliceReport(await base.policeReport(params), simStore.getState(), params);
    },
    async tasSlots(gateId?: string) {
      return applyTas(await base.tasSlots(gateId), simStore.getState());
    },
  };

  return new Proxy(base, {
    get(target, prop, receiver) {
      if (prop in overrides && prop !== "constructor") {
        return (overrides as Record<string | symbol, unknown>)[prop];
      }
      const value = Reflect.get(target, prop, receiver);
      // Bind methods to the base so `this` inside the real adapter stays correct.
      return typeof value === "function" ? value.bind(target) : value;
    },
  }) as DataAdapter;
}
