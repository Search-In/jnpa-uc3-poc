// Data-adapter CONTRACT test (UC1 parity). Asserts that MockAdapter implements
// every DataAdapter method and returns the right-shaped data. The same asserts
// would hold for LiveAdapter against a healthy gateway, so this file is the
// machine-checked statement of the adapter contract — not a mock-internals test.

import { describe, expect, it } from "vitest";
import { MockAdapter } from "./mock";

const a = new MockAdapter();

describe("DataAdapter contract — MockAdapter", () => {
  it("declares mock mode", () => {
    expect(a.mode).toBe("mock");
  });

  it("gates() is a non-empty array of {id,lat,lon}", async () => {
    const gates = await a.gates();
    expect(Array.isArray(gates) && gates.length > 0).toBe(true);
    for (const g of gates) {
      expect(typeof g.id).toBe("string");
      expect(typeof g.lat).toBe("number");
      expect(typeof g.lon).toBe("number");
      expect(typeof g.target_vph).toBe("number");
    }
  });

  it("corridor() returns a [lon,lat] polyline + SEG-* segments", async () => {
    const c = await a.corridor();
    expect(c.polyline.length).toBeGreaterThan(5);
    expect(c.segments.length).toBe(c.segment_count);
    expect(c.segments[0].id).toBe("SEG-00");
    // [lon, lat] order: lon ~73, lat ~18.9 near JNPA.
    const [lon, lat] = c.polyline[0];
    expect(lon).toBeGreaterThan(72);
    expect(lat).toBeGreaterThan(18);
  });

  it("trafficSnapshots() has per-segment speed + jam_factor", async () => {
    const snaps = await a.trafficSnapshots();
    expect(snaps.length).toBeGreaterThan(0);
    expect(
      snaps.every((s) => typeof s.speed_kmh === "number" && typeof s.jam_factor === "number"),
    ).toBe(true);
  });

  it("trafficPredict() uses the SYNTHETIC decision path with SEG predictions", async () => {
    const p = await a.trafficPredict();
    expect(p.decision_path).toBe("SYNTHETIC");
    expect(Object.keys(p.predictions).length).toBeGreaterThan(0);
    expect(Object.keys(p.predictions)[0].startsWith("SEG-")).toBe(true);
  });

  it("trucks() returns realistic devices and honours the state filter", async () => {
    const all = await a.trucks();
    expect(all.length).toBeGreaterThan(10);
    expect(
      all.every((t) => typeof t.position.lat === "number" && typeof t.position.lon === "number"),
    ).toBe(true);
    const queued = await a.trucks("AT_GATE_QUEUE");
    expect(queued.every((t) => t.state === "AT_GATE_QUEUE")).toBe(true);
  });

  it("reroute() and putZones() echo success", async () => {
    expect((await a.reroute("TRK-1000", { gate_id: "G-BMCT" })).rerouted).toBe(true);
    const zones = await a.zones();
    const saved = await a.putZones(zones);
    expect(saved.saved).toBe(true);
    expect(saved.count).toBe(zones.length);
  });

  it("alerts() is a mix of kinds with timestamps and severities", async () => {
    const alerts = await a.alerts();
    expect(alerts.length).toBeGreaterThan(0);
    const kinds = new Set(alerts.map((x) => x.kind));
    expect(kinds.size).toBeGreaterThan(1);
    expect(alerts.every((x) => typeof x.ts === "string" && x.severity != null)).toBe(true);
  });

  it("kpiStrip() returns the 7 KPIs with numeric value/target and boolean onTarget", async () => {
    const strip = await a.kpiStrip();
    expect(strip.length).toBe(7);
    const keys = strip.map((k) => k.key);
    expect(keys).toEqual([
      "gate_queue_wait",
      "gate_txn_time",
      "trt_empty_ecd",
      "tat_inside_port",
      "queue_length",
      "avg_dwell",
      "gate_throughput",
    ]);
    for (const k of strip) {
      expect(typeof k.value).toBe("number");
      expect(typeof k.target).toBe("number");
      expect(typeof k.onTarget).toBe("boolean");
      expect(k.trend.length).toBeGreaterThanOrEqual(2);
    }
    // deltaPct sign reads "moved the right way": lower_is_better improvement is negative.
    const wait = strip.find((k) => k.key === "gate_queue_wait")!;
    expect(wait.deltaPct).toBeLessThan(0);
  });

  it("sources() are mostly LIVE with 1-2 DEGRADED; cameras() mix paths", async () => {
    const sources = await a.sources();
    expect(sources.length).toBeGreaterThan(4);
    const degraded = sources.filter((s) => s.state === "DEGRADED").length;
    expect(degraded).toBeGreaterThanOrEqual(1);
    expect(degraded).toBeLessThanOrEqual(2);
    const cams = await a.cameras();
    const paths = new Set(cams.map((c) => c.decision_path));
    expect(paths.size).toBeGreaterThan(1);
  });

  it("decisions(), policeReport() and policePdfUrl() are shaped", async () => {
    expect((await a.decisions()).length).toBeGreaterThan(0);
    const incidents = await a.policeReport();
    expect(incidents.length).toBeGreaterThan(0);
    expect(incidents[0].challan).toBeTruthy();
    expect(typeof a.policePdfUrl()).toBe("string");
  });

  it("scenarios()/runScenario()/scenarioTimeline() form a 5-step chain; TFC-3 is cross-twin", async () => {
    const scns = await a.scenarios();
    expect(scns.length).toBe(3);
    const run = await a.runScenario("tfc3", {});
    expect(run.handle_id).toBe("tfc3-mock");
    expect(run.status).toBe("DONE");
    const tl = await a.scenarioTimeline(run.handle_id);
    expect(tl.steps.length).toBe(5);
    expect(
      tl.steps.every((s) => typeof s.step_no === "number" && typeof s.title === "string"),
    ).toBe(true);
    expect(tl.steps.some((s) => s.trigger === "cross-twin")).toBe(true);
  });

  it("emptyAllocations() + emptyTrtKpi() cover ECD/CFS and the trt_empty_ecd KPI", async () => {
    const allocs = await a.emptyAllocations();
    expect(allocs.length).toBeGreaterThanOrEqual(6);
    expect(
      allocs.every((x) => typeof x.distance_km === "number" && typeof x.est_trt_min === "number"),
    ).toBe(true);
    const kpi = await a.emptyTrtKpi();
    expect(kpi.key).toBe("trt_empty_ecd");
    expect(kpi.onTarget).toBe(true);
  });

  it("carbonRollup(): by_source.moving + idle ≈ total_kg", async () => {
    const c = await a.carbonRollup();
    expect(c.by_source.moving + c.by_source.idle).toBeCloseTo(c.total_kg, 5);
    expect(Object.keys(c.by_class).length).toBeGreaterThan(1);
    expect(c.vehicle_count).toBeGreaterThan(0);
  });

  it("leoQueue() has at least one blocked row with customs_flags; customsFlags() derives from them", async () => {
    const queue = await a.leoQueue();
    const blocked = queue.filter((r) => !r.leo_ready);
    expect(blocked.length).toBeGreaterThan(0);
    expect(blocked.every((r) => r.customs_flags.length > 0)).toBe(true);
    const flags = await a.customsFlags();
    expect(flags.length).toBe(blocked.length);
    expect(flags.every((f) => f.kind === "CUSTOMS_FLAG")).toBe(true);
  });

  it("identityVerify(): genuine→VERIFIED, impostor→REJECTED, unknown→PROVISIONAL(24h)", async () => {
    const gallery = await a.identityGallery();
    expect(gallery.length).toBeGreaterThanOrEqual(6);
    const ok = await a.identityVerify("DRV-1001", "genuine");
    expect(ok.decision).toBe("VERIFIED");
    expect(ok.score).toBeGreaterThan(0.9);
    expect((await a.identityVerify("x", "impostor")).decision).toBe("REJECTED");
    const prov = await a.identityVerify("x", "unknown");
    expect(prov.decision).toBe("PROVISIONAL");
    expect(prov.cure_window_h).toBe(24);
    expect(typeof prov.provisional_until).toBe("string");
  });

  it("parkingAvailability(): every facility available === capacity - occupied; summary agrees", async () => {
    const facilities = await a.parkingAvailability();
    expect(facilities.length).toBeGreaterThanOrEqual(4);
    for (const f of facilities) {
      expect(f.available).toBe(f.capacity - f.occupied);
    }
    const summary = await a.parkingSummary();
    expect(summary.total_capacity).toBe(facilities.reduce((s, f) => s + f.capacity, 0));
    expect(summary.total_available).toBe(facilities.reduce((s, f) => s + f.available, 0));
  });
});
