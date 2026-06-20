// Web KPI arithmetic tests (Wave 4 / KPI-3). Mirrors tests/test_kpi.py so the
// dashboard numbers can never drift from shared/jnpa_shared/kpi.py.
import { describe, it, expect } from "vitest";
import { deltaPct, isOnTarget, buildKpiResult, type KpiSpec } from "./compute";

describe("deltaPct (mirrors kpi.py._delta_pct)", () => {
  it("lower-is-better improvement reads as a negative change vs baseline", () => {
    // gate_queue_wait baseline 14.5, value 6.0 -> (6-14.5)/14.5*100 = -58.62
    expect(deltaPct(6.0, 14.5)).toBeCloseTo(-58.62, 1);
  });

  it("higher-is-better improvement reads as a positive change", () => {
    // gate_throughput baseline 44, value 66 -> (66-44)/44*100 = 50.0
    expect(deltaPct(66.0, 44.0)).toBeCloseTo(50.0, 1);
  });

  it("baseline 0 yields 0 (no divide-by-zero)", () => {
    expect(deltaPct(10, 0)).toBe(0);
  });
});

describe("isOnTarget", () => {
  it("lower_is_better: value <= target is on target", () => {
    expect(isOnTarget(6.0, 8.0, "lower_is_better")).toBe(true);
    expect(isOnTarget(4.5, 3.0, "lower_is_better")).toBe(false); // gate_txn_time off
  });

  it("higher_is_better: value >= target is on target", () => {
    expect(isOnTarget(66.0, 60.0, "higher_is_better")).toBe(true);
    expect(isOnTarget(50.0, 60.0, "higher_is_better")).toBe(false);
  });
});

describe("buildKpiResult", () => {
  const spec: KpiSpec = {
    key: "gate_queue_wait",
    label: "Gate Queue Wait Time",
    unit: "min",
    direction: "lower_is_better",
    target: 8.0,
    baseline: 14.5,
    value: 6.0,
  };

  it("assembles the KpiResult shape with computed delta + onTarget + trend", () => {
    const r = buildKpiResult(spec, [14.5, 10.0, 6.0]);
    expect(r.key).toBe("gate_queue_wait");
    expect(r.unit).toBe("min");
    expect(r.direction).toBe("lower_is_better");
    expect(r.onTarget).toBe(true);
    expect(r.deltaPct).toBeCloseTo(-58.62, 1);
    expect(r.trend[r.trend.length - 1]).toBe(6.0);
  });

  it("off-target lower-is-better KPI reports onTarget false", () => {
    const r = buildKpiResult(
      { ...spec, key: "gate_txn_time", target: 3.0, baseline: 5.2, value: 4.5 },
      [],
    );
    expect(r.onTarget).toBe(false);
  });
});
