// Unit tests for the simulator overlay logic + store. These lock in the
// business rules that drive the dashboard from the Simulator page so they can't
// silently regress (the visual map/KPI behaviour rides on top of these).

import { beforeEach, describe, expect, it } from "vitest";
import type { Alert, Gate, KpiResult, TasSlot, TrafficSnapshot, TruckDevice } from "@/lib/types";
import { simStore } from "./simStore";
import {
  applyAlerts,
  applyGates,
  applyKpis,
  applyPoliceReport,
  applySnapshots,
  applyTas,
  applyTrucks,
} from "./applySim";

const GATE: Gate = {
  id: "G-NSICT",
  name: "NSICT",
  lat: 18.95,
  lon: 72.95,
  target_vph: 60,
  throughput_60min: 50,
  utilisation: 0.8,
};

const SNAP: TrafficSnapshot = {
  segment_id: "SEG-03",
  ts: "2026-06-27T09:00:00Z",
  speed_kmh: 30,
  jam_factor: 2,
  source: "live",
};

const KPI_QUEUE: KpiResult = {
  key: "queue_length",
  label: "Queue Length",
  unit: "vehicles",
  value: 23,
  target: 25,
  baseline: 41,
  deltaPct: -43.9,
  direction: "lower_is_better",
  onTarget: true,
  trend: [40, 36, 30, 27, 25, 24, 23, 23],
};

beforeEach(() => {
  simStore.reset();
});

describe("applyGates", () => {
  it("is a pass-through when no lever is engaged", () => {
    expect(applyGates([GATE], simStore.getState())).toEqual([GATE]);
  });

  it("scales throughput by the global flow rate", () => {
    simStore.setFlowRate(2);
    const [g] = applyGates([GATE], simStore.getState());
    expect(g.throughput_60min).toBe(100);
  });

  it("overlays per-gate utilisation", () => {
    simStore.setGate("G-NSICT", { utilisation: 1.2 });
    const [g] = applyGates([GATE], simStore.getState());
    expect(g.utilisation).toBe(1.2);
  });
});

describe("applySnapshots", () => {
  it("overrides jam factor + speed for a congested segment", () => {
    simStore.setSegment("SEG-03", { jamFactor: 8, speedKmh: 6 });
    const [s] = applySnapshots([SNAP], simStore.getState());
    expect(s.jam_factor).toBe(8);
    expect(s.speed_kmh).toBe(6);
    expect(s.source).toBe("sim");
  });
});

describe("applyTrucks", () => {
  it("injects AT_GATE_QUEUE trucks to honour a gate queue length", () => {
    simStore.setGate("G-NSICT", { queueLength: 5, lat: GATE.lat, lon: GATE.lon });
    const out = applyTrucks([], simStore.getState(), "AT_GATE_QUEUE");
    const queued = out.filter((t) => t.gate_id === "G-NSICT" && t.state === "AT_GATE_QUEUE");
    expect(queued).toHaveLength(5);
  });

  it("adds the existing trucks toward the requested queue length", () => {
    const existing: TruckDevice[] = [
      {
        device_id: "T1",
        gate_id: "G-NSICT",
        state: "AT_GATE_QUEUE",
        position: { lat: GATE.lat, lon: GATE.lon },
        speed_kmh: 0,
        heading: 0,
        remaining_km: 0,
        eta_s: 0,
      },
    ];
    simStore.setGate("G-NSICT", { queueLength: 4 });
    const out = applyTrucks(existing, simStore.getState(), "AT_GATE_QUEUE");
    const queued = out.filter((t) => t.gate_id === "G-NSICT" && t.state === "AT_GATE_QUEUE");
    expect(queued).toHaveLength(4); // 1 existing + 3 injected
  });

  it("injects EN_ROUTE_TO_PORT trucks for vehicle injection", () => {
    simStore.setVehicleInjection(12);
    const out = applyTrucks([], simStore.getState(), "EN_ROUTE_TO_PORT");
    expect(out.filter((t) => t.state === "EN_ROUTE_TO_PORT")).toHaveLength(12);
  });
});

describe("applyAlerts", () => {
  it("prepends OPEN injected incidents to the feed", () => {
    const base: Alert[] = [{ id: "A1", ts: "t", kind: "X", severity: "info" }];
    simStore.injectIncident("WRONG_WAY", "REPORT_TO_POLICE", "G-NSICT");
    const out = applyAlerts(base, simStore.getState());
    expect(out[0].kind).toBe("WRONG_WAY");
    expect(out[out.length - 1].id).toBe("A1");
  });

  it("drops RESOLVED incidents from the active feed", () => {
    const base: Alert[] = [{ id: "A1", ts: "t", kind: "X", severity: "info" }];
    simStore.injectIncident("ACCIDENT", "critical", "G-NSICT");
    simStore.clearIncidents(); // resolve
    const out = applyAlerts(base, simStore.getState());
    expect(out).toEqual(base); // resolved incident no longer active
  });
});

describe("applyPoliceReport", () => {
  it("surfaces injected incidents as report rows with location + scenario", () => {
    simStore.injectIncident("ROAD_BLOCKAGE", "critical", "G-NSICT", "SEG-03");
    const out = applyPoliceReport([], simStore.getState());
    expect(out).toHaveLength(1);
    expect(out[0].kind).toBe("ROAD_BLOCKAGE");
    expect(out[0].payload?.status).toBe("OPEN");
    expect(out[0].payload?.location).toContain("NSICT");
  });

  it("keeps a cleared incident as a RESOLVED report row (no data loss)", () => {
    simStore.injectIncident("WRONG_WAY", "REPORT_TO_POLICE", "G-NSICT");
    simStore.clearIncidents();
    const out = applyPoliceReport([], simStore.getState());
    expect(out).toHaveLength(1);
    expect(out[0].payload?.status).toBe("RESOLVED");
    expect(out[0].ack).toBe(true);
  });

  it("honours the kind filter", () => {
    simStore.injectIncident("WRONG_WAY", "REPORT_TO_POLICE", "G-NSICT");
    simStore.injectIncident("CONGESTION", "warning", "G-BMCT");
    const out = applyPoliceReport([], simStore.getState(), { kind: "CONGESTION" });
    expect(out).toHaveLength(1);
    expect(out[0].kind).toBe("CONGESTION");
  });
});

describe("applyTas", () => {
  const slot: TasSlot = {
    slot_id: "TAS-G-NSICT-1",
    gate_id: "G-NSICT",
    start: "2026-06-27T10:00:00.000Z",
    status: "BOOKED",
  };

  it("reschedules slots when the gate queue is congested", () => {
    simStore.setGate("G-NSICT", { queueLength: 50 }); // > threshold 20
    const [out] = applyTas([slot], simStore.getState());
    expect(out.status).toBe("RESCHEDULED");
    expect(out.rescheduled_to).toBeTruthy();
    expect(new Date(out.rescheduled_to!).getTime()).toBeGreaterThan(new Date(slot.start).getTime());
  });

  it("leaves slots on schedule below the congestion threshold", () => {
    simStore.setGate("G-NSICT", { queueLength: 10 });
    expect(applyTas([slot], simStore.getState())).toEqual([slot]);
  });
});

describe("applyKpis", () => {
  it("worsens queue length under gate load and recomputes onTarget", () => {
    simStore.setGate("G-NSICT", { queueLength: 30 });
    const [k] = applyKpis([KPI_QUEUE], simStore.getState());
    expect(k.value).toBeGreaterThan(KPI_QUEUE.value); // lower_is_better → higher = worse
    expect(k.onTarget).toBe(false); // pushed above the 25 target
    expect(k.baseline).toBe(KPI_QUEUE.baseline); // baseline never touched
  });

  it("is a pass-through when idle", () => {
    expect(applyKpis([KPI_QUEUE], simStore.getState())).toEqual([KPI_QUEUE]);
  });
});

describe("simStore focus + scenarios", () => {
  it("records the last-touched asset when a gate is driven", () => {
    simStore.setGate("G-NSICT", { queueLength: 10 });
    expect(simStore.getState().lastTouched).toBe("G-NSICT");
  });

  it("clearGate removes the override so the gate returns to baseline", () => {
    simStore.setGate("G-NSICT", { queueLength: 10, utilisation: 0.5 });
    simStore.clearGate("G-NSICT");
    expect(simStore.getState().gates["G-NSICT"]).toBeUndefined();
    // overlay is now a pass-through for that gate
    expect(applyGates([GATE], simStore.getState())).toEqual([GATE]);
  });

  it("clearSegment removes a congestion override", () => {
    simStore.setSegment("SEG-03", { jamFactor: 8 });
    simStore.clearSegment("SEG-03");
    expect(simStore.getState().segments["SEG-03"]).toBeUndefined();
    expect(applySnapshots([SNAP], simStore.getState())).toEqual([SNAP]);
  });

  it("starting a scenario seeds the tour, highlights and focus", () => {
    simStore.startScenario("SIM-TFC1");
    const s = simStore.getState();
    expect(s.tour.scenarioId).toBe("SIM-TFC1");
    expect(s.tour.stepIndex).toBe(0);
    expect(s.highlights).toContain("G-NSICT");
    expect(s.lastTouched).toBe("G-NSICT");
    // step 0 patch drives the NSICT queue
    expect(s.gates["G-NSICT"]?.queueLength).toBeGreaterThan(0);
    simStore.stopScenario();
    expect(simStore.getState().tour.scenarioId).toBeNull();
  });
});
