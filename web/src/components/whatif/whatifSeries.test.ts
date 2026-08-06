// The chart mapping is the only real logic in the What-If UI, so it is the part
// worth testing: everything else renders what the API already computed.
//
// These fixtures are shaped exactly like a real /api/cargo/simulate/* response.
// They assert the RESHAPING is faithful — that the chart shows what the backend
// said, in the right series — not that any figure is correct (the backend's own
// suite owns that).

import { describe, expect, it } from "vitest";
import type { SimulationResult } from "@/lib/api";
import { buildSeries } from "./whatifSeries";

function envelope(partial: Partial<SimulationResult>): SimulationResult {
  return {
    scenario: "berth-cascade",
    method: "…",
    result: {},
    figures: {},
    assumptions: [],
    queries: [],
    recommendations: [],
    data_available: true,
    notes: [],
    ...partial,
  };
}

describe("buildSeries", () => {
  it("returns null for a scenario with no series", () => {
    expect(buildSeries(envelope({ scenario: "unknown-scenario" }))).toBeNull();
  });

  it("returns null when the window produced no rows", () => {
    expect(buildSeries(envelope({ scenario: "berth-cascade", result: { displaced_calls: [] } })))
      .toBeNull();
  });

  it("maps modal-shift to a baseline vs shifted hourly series with the capacity line", () => {
    const s = buildSeries(
      envelope({
        scenario: "modal-shift",
        figures: { sustained_rate_per_hour: 100 },
        result: {
          shifted_profile: [
            { hour: "2026-08-01T08:00:00Z", baseline: 150, added: 6, shifted: 156 },
            { hour: "2026-08-01T09:00:00Z", baseline: 100, added: 4, shifted: 104 },
          ],
        },
      }),
    );
    expect(s).not.toBeNull();
    expect(s!.bars.map((b) => b.key)).toEqual(["Baseline", "After shift"]);
    expect(s!.data[0]).toMatchObject({ Baseline: 150, "After shift": 156 });
    expect(s!.reference).toEqual({ value: 100, label: "Sustained 100/h" });
  });

  it("maps gate-slotting by joining proposed slots onto the observed hours", () => {
    const s = buildSeries(
      envelope({
        scenario: "gate-slotting",
        figures: { sustained_rate_per_hour: 100 },
        result: {
          arrival_pattern: {
            hourly: [
              { bucket: "2026-08-03T08:00:00Z", arrivals: 150 },
              { bucket: "2026-08-03T09:00:00Z", arrivals: 20 },
            ],
          },
          proposed_slots: [
            { hour: "2026-08-03T08:00:00Z", cap: 100, booked: 100 },
            { hour: "2026-08-03T09:00:00Z", cap: 100, booked: 70 },
          ],
        },
      }),
    );
    expect(s!.data).toEqual([
      { x: "03 08:00", Observed: 150, Slotted: 100 },
      { x: "03 09:00", Observed: 20, Slotted: 70 },
    ]);
  });

  it("falls back to 0 slotted when an observed hour has no proposed slot", () => {
    const s = buildSeries(
      envelope({
        scenario: "gate-slotting",
        result: {
          arrival_pattern: { hourly: [{ bucket: "2026-08-03T08:00:00Z", arrivals: 12 }] },
          proposed_slots: [],
        },
      }),
    );
    expect(s!.data[0]).toMatchObject({ Observed: 12, Slotted: 0 });
  });

  it("maps berth-cascade to one delay bar per displaced vessel", () => {
    const s = buildSeries(
      envelope({
        scenario: "berth-cascade",
        result: {
          displaced_calls: [
            { vessel: "VESSEL TWO", berth: "B1", delay_hours: 6 },
            { vessel: "VESSEL THREE", berth: "B1", delay_hours: 6 },
          ],
        },
      }),
    );
    expect(s!.layout).toBe("vertical");
    expect(s!.data).toEqual([
      { x: "VESSEL TWO (B1)", "Delay (h)": 6 },
      { x: "VESSEL THREE (B1)", "Delay (h)": 6 },
    ]);
  });

  it("maps driver-shortage to a fleet total plus per-transporter before/after", () => {
    const s = buildSeries(
      envelope({
        scenario: "driver-shortage",
        figures: { baseline_trips: 6, reduced_trips: 3 },
        result: {
          exposed_transporters: {
            by_absolute_loss: [{ transporter: "ALPHA LOGISTICS", trips: 5, reduced_trips: 3 }],
          },
        },
      }),
    );
    expect(s!.data[0]).toMatchObject({ x: "All transporters", Before: 6, After: 3 });
    expect(s!.data[1]).toMatchObject({ x: "ALPHA LOGISTICS", Before: 5, After: 3 });
  });

  it("omits the capacity line when the backend derived no sustained rate", () => {
    const s = buildSeries(
      envelope({
        scenario: "modal-shift",
        figures: { sustained_rate_per_hour: null },
        result: { shifted_profile: [{ hour: "2026-08-01T08:00:00Z", baseline: 1, shifted: 2 }] },
      }),
    );
    expect(s!.reference).toBeUndefined();
  });
});
