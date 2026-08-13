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
  // --- SecureVision -------------------------------------------------------
  // The vendor surface is part of the adapter contract like everything else, so
  // the mock build stays runnable with no SecureVision credential and the two
  // adapters cannot drift in shape.

  it("svHealth() reports posture without pretending to be live", async () => {
    const h = await a.svHealth();
    expect(h.integration).toBe("securevision");
    expect(["LIVE", "UNAVAILABLE", "NOT_CONFIGURED"]).toContain(h.status);
    // Clip analytics, not live CCTV — the mode is part of the contract.
    expect(h.mode).toBe("UPLOAD_CLIP_ANALYTICS");
    // Analysis METADATA is durable (core.video_analysis, migration 0143);
    // detection results and any person/face payload are not stored.
    expect(h.persistence).toBe("ANALYSIS_METADATA");
  });

  it("svAnalyses() is a durable, paginated history", async () => {
    const list = await a.svAnalyses();
    // The history is persisted (it survives a gateway/worker restart) and
    // reports its own size so the UI can page through ALL of it.
    expect(list.persisted).toBe(true);
    expect(list.degraded).toBe(false);
    expect(typeof list.total).toBe("number");
    expect(Array.isArray(list.analyses)).toBe(true);
    for (const an of list.analyses) {
      expect(typeof an.analysis_id).toBe("string");
      // Operational metadata only — no face/person field is part of the shape.
      expect(Object.keys(an).join(",")).not.toMatch(/face|embedding|biometric/i);
    }
  });

  it("svAnalyses() paginates: a later page never repeats the first", async () => {
    const first = await a.svAnalyses(1, 0);
    const second = await a.svAnalyses(1, 1);
    const ids = new Set(first.analyses.map((x) => x.analysis_id));
    for (const an of second.analyses) expect(ids.has(an.analysis_id)).toBe(false);
    expect(first.total).toBe(second.total);
  });

  it("svIncident() returns the per-analyzer blocks the screens read", async () => {
    const i01 = await a.svIncident("A1", "i01");
    expect(i01.incident_code).toBe("i01");
    expect(i01.source).toBe("SECUREVISION");
    expect(typeof i01.plate?.plate).toBe("string");

    const i02 = await a.svIncident("A1", "i02");
    expect(Array.isArray(i02.counts)).toBe(true);
    expect(i02.total_count).toBe((i02.counts ?? []).reduce((sum, c) => sum + (c.count ?? 0), 0));

    const i09 = await a.svIncident("A1", "i09");
    expect(["MATCH", "REVIEW", "UNKNOWN"]).toContain(i09.container?.agreement);

    const i12 = await a.svIncident("A1", "i12");
    expect(typeof i12.tamper?.tamper_state).toBe("string");
  });

  it("svIncidentPersons() carries all THREE verdicts and never invents names", async () => {
    const r = await a.svIncidentPersons("A1");
    expect(r.incident_code).toBe("i07");
    const statuses = r.persons.map((p) => p.person_status);
    expect(statuses).toContain("AUTHORIZED");
    expect(statuses).toContain("UNAUTHORIZED");
    expect(statuses).toContain("UNVERIFIED");
    for (const p of r.persons) {
      expect(["AUTHORIZED", "UNAUTHORIZED", "UNVERIFIED"]).toContain(p.person_status);
      // An unidentified person must not carry an identity.
      if (p.person_status !== "AUTHORIZED") expect(p.person_name).toBeNull();
    }
  });

  it("svIncidentAll() marks the narrative as AI-generated", async () => {
    const r = await a.svIncidentAll("A1");
    expect(r.narrative_provenance).toBe("AI_GENERATED");
    expect(r.ai_generated).toBe(true);
    expect(typeof r.combined_description).toBe("string");
  });

  it("svStreamTicket() returns a scoped stream URL", async () => {
    const t = await a.svStreamTicket("A1");
    expect(t.analysis_id).toBe("A1");
    expect(t.stream_url).toContain("/api/sv/analytics/video/A1/stream");
    expect(t.expires_in).toBeGreaterThan(0);
  });

  it("face surfaces round-trip without exposing vendor filesystem paths", async () => {
    const people = await a.svFaces();
    expect(Array.isArray(people)).toBe(true);
    for (const p of people) {
      expect(p).not.toHaveProperty("snapshot_path");
    }
    const events = await a.svFaceEvents();
    for (const e of events) {
      expect(["AUTHORIZED", "UNAUTHORIZED", "UNVERIFIED"]).toContain(e.person_status);
      expect(e.snapshot_available).toBe(false);
    }
    const status = await a.svFaceStatus();
    expect(typeof status.model_ready).toBe("boolean");
    // The enrolled roster is personal data — only a count is ever exposed.
    expect(status).not.toHaveProperty("authorized_names");

    const enrolled = await a.svEnrollFace({ person_id: "EMP-9", name: "Test", photos: [] });
    expect(enrolled.person_id).toBe("EMP-9");
    const updated = await a.svUpdateFace(12, { is_active: false });
    expect(updated.is_active).toBe(false);
  });

  it("mock SecureVision fixtures are labelled DEMO so they cannot pass as real", async () => {
    const list = await a.svAnalyses();
    expect(list.analyses[0].analysis_id).toContain("DEMO");
    const report = await a.svIncidentAll("A1");
    expect(report.combined_description).toContain("[DEMO]");
    const people = await a.svFaces();
    expect(people[0].person_id).toContain("DEMO");
  });
});
