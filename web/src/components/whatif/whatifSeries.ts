// Series derivation for the What-If before/after chart.
//
// Split out of BeforeAfterChart.tsx so the mapping is a pure function with no
// React dependency: it can be unit-tested directly, and the chart file exports
// only a component (which is what React Fast Refresh requires).
//
// This module RESHAPES what the API returned. It never computes a figure — every
// number here came from /api/cargo/simulate/*.

import type { SimulationResult } from "@/lib/api";
import { STATUS } from "@/lib/tokens";

export const BEFORE = STATUS.info;
export const AFTER = STATUS.warning;
export const LIMIT = STATUS.critical;

export interface Series {

  title: string;
  data: Record<string, string | number>[];
  xKey: string;
  bars: { key: string; name: string; colour: string }[];
  /** Horizontal capacity/ceiling line, when the scenario has one. */
  reference?: { value: number; label: string };
  /** Rendered vertically when the category axis holds names rather than time. */
  layout?: "horizontal" | "vertical";
  footnote?: string;
}

/** The sustained-rate ceiling, or undefined when the backend could not derive one.
 *
 *  Guarded explicitly against null/0: `Number(null)` is 0 and `isFinite(0)` is
 *  true, so a naive check draws a red "Sustained 0/h" line across the chart and
 *  implies the gate has no capacity — when the truth is that no rate could be
 *  established. gate-slotting legitimately returns null here. */
function ceiling(raw: unknown): { value: number; label: string } | undefined {
  if (raw === null || raw === undefined || raw === "") return undefined;
  const n = Number(raw);
  if (!Number.isFinite(n) || n <= 0) return undefined;
  return { value: n, label: `Sustained ${n}/h` };
}

/** Short time label for an hourly bucket ("2026-08-03T08:00:00Z" → "03 08:00"). */
function hourLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const day = String(d.getUTCDate()).padStart(2, "0");
  const hh = String(d.getUTCHours()).padStart(2, "0");
  return `${day} ${hh}:00`;
}

export function buildSeries(result: SimulationResult): Series | null {
  const r = result.result ?? {};
  const f = result.figures ?? {};

  switch (result.scenario) {
    case "modal-shift": {
      const rows = (r.shifted_profile ?? []) as any[];
      if (!rows.length) return null;
      return {
        title: "Hourly gate profile — before vs after the shift",
        data: rows.map((h) => ({
          x: hourLabel(h.hour),
          Baseline: Number(h.baseline ?? 0),
          "After shift": Number(h.shifted ?? 0),
        })),
        xKey: "x",
        bars: [
          { key: "Baseline", name: "Baseline", colour: BEFORE },
          { key: "After shift", name: "After shift", colour: AFTER },
        ],
        reference: ceiling(f.sustained_rate_per_hour),
        footnote:
          "Shifted trips are apportioned across hours in proportion to the observed arrival shape — see Assumptions.",
      };
    }

    case "gate-slotting": {
      const observed = (r.arrival_pattern?.hourly ?? []) as any[];
      const slotted = (r.proposed_slots ?? []) as any[];
      if (!observed.length) return null;
      const byHour = new Map(slotted.map((s: any) => [String(s.hour), Number(s.booked ?? 0)]));
      return {
        title: "Hourly arrivals — observed vs proposed slotting",
        data: observed.map((h) => ({
          x: hourLabel(h.bucket),
          Observed: Number(h.arrivals ?? 0),
          Slotted: byHour.get(String(h.bucket)) ?? 0,
        })),
        xKey: "x",
        bars: [
          { key: "Observed", name: "Observed arrivals", colour: BEFORE },
          { key: "Slotted", name: "After slotting", colour: AFTER },
        ],
        reference: ceiling(f.sustained_rate_per_hour),
        footnote:
          "Slotting caps each hour at the sustained rate and defers the excess forward into hours with headroom.",
      };
    }

    case "berth-cascade": {
      const rows = (r.displaced_calls ?? []) as any[];
      if (!rows.length) return null;
      return {
        title: "Delay inherited by each displaced call",
        data: rows.map((d) => ({
          x: `${d.vessel ?? "—"}${d.berth ? ` (${d.berth})` : ""}`,
          "Delay (h)": Number(d.delay_hours ?? 0),
        })),
        xKey: "x",
        bars: [{ key: "Delay (h)", name: "Delay (hours)", colour: LIMIT }],
        layout: "vertical",
        footnote: "Calls at other berths are unaffected — the cascade is per berth.",
      };
    }

    case "crane-productivity": {
      const rows = (r.berth_queue_impact ?? []) as any[];
      if (!rows.length) return null;
      return {
        title: "Berth-queue delay behind the slowed call",
        data: rows.map((d) => ({
          x: `${d.vessel ?? "—"}${d.berth ? ` (${d.berth})` : ""}`,
          "Delay (h)": Number(d.delay_hours ?? 0),
        })),
        xKey: "x",
        bars: [{ key: "Delay (h)", name: "Delay (hours)", colour: LIMIT }],
        layout: "vertical",
        footnote:
          "Derived from the turnaround increase: new_hours = hours_worked / (1 − reduction).",
      };
    }

    case "driver-shortage": {
      const base = Number(f.baseline_trips ?? 0);
      const after = Number(f.reduced_trips ?? 0);
      if (!base) return null;
      const exposure = (r.exposed_transporters?.by_absolute_loss ?? []) as any[];
      return {
        title: "Trips before vs after the shortage",
        data: [
          { x: "All transporters", Before: base, After: after },
          ...exposure.slice(0, 6).map((e) => ({
            x: String(e.transporter ?? "—").slice(0, 22),
            Before: Number(e.trips ?? 0),
            After: Number(e.reduced_trips ?? 0),
          })),
        ],
        xKey: "x",
        bars: [
          { key: "Before", name: "Baseline trips", colour: BEFORE },
          { key: "After", name: "After shortage", colour: AFTER },
        ],
        layout: "vertical",
        footnote: "Per vehicle-day, trips fall to floor(trips × (1 − reduction)).",
      };
    }

    default:
      return null;
  }
}

