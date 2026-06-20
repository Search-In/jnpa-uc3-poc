// Web-side KPI arithmetic (Wave 4 / KPI-3). Extracted from data/mock.ts so the
// numbers the dashboard shows have their OWN unit tests and can never silently
// drift from the Python ground truth in shared/jnpa_shared/kpi.py.
//
// Mirrors kpi.py exactly:
//   * _delta_pct: signed % change vs baseline ((value-baseline)/baseline*100),
//     so a lower-is-better improvement reads NEGATIVE. baseline==0 -> 0.
//   * on_target: value<=target (lower_is_better) | value>=target (higher_is_better).

import type { KpiResult } from "@/lib/types";

export type Direction = "lower_is_better" | "higher_is_better";

export interface KpiSpec {
  key: string;
  label: string;
  unit: string;
  direction: Direction;
  target: number;
  baseline: number;
  value: number;
}

export function round(n: number, dp = 2): number {
  const f = 10 ** dp;
  return Math.round(n * f) / f;
}

/** Signed % change vs baseline (mirrors kpi.py._delta_pct). */
export function deltaPct(value: number, baseline: number): number {
  if (baseline === 0) return 0;
  return round(((value - baseline) / baseline) * 100, 1);
}

/** Whether a value meets/beats its target for the given direction. */
export function isOnTarget(value: number, target: number, direction: Direction): boolean {
  return direction === "lower_is_better" ? value <= target : value >= target;
}

/** Build a full KpiResult from a spec + a precomputed trend (oldest -> newest). */
export function buildKpiResult(spec: KpiSpec, trend: number[]): KpiResult {
  return {
    key: spec.key,
    label: spec.label,
    unit: spec.unit,
    value: spec.value,
    target: spec.target,
    baseline: spec.baseline,
    deltaPct: deltaPct(spec.value, spec.baseline),
    direction: spec.direction,
    onTarget: isOnTarget(spec.value, spec.target, spec.direction),
    trend,
  };
}
