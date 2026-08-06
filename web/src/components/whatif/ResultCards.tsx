// Result summary — KPI tiles from the scenario's `figures`, plus the two states
// that matter more than the tiles.
//
// A what-if answer has three possible outcomes and the UI must not blur them:
//
//   1. answered            → figures render
//   2. no data in window   → "No data available for selected period" + the
//                            backend's own reason. NOT a blank panel: a blank
//                            panel reads as a broken screen, when in fact the
//                            engine ran correctly and declined to invent a number.
//   3. a query FAILED      → louder still, because a failure and an empty table
//                            both produce zero rows. Conflating them is how a
//                            confidently wrong "no data" reaches an evaluator.
//
// Which figures are headline is scenario-specific; the rest are shown in a
// secondary grid so nothing the backend computed is hidden from the operator.

import { AlertTriangle, Database, Info } from "lucide-react";
import type { SimulationResult } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { StatCard, StatGrid, type Tone } from "@/components/ui/dtccc";

/** Headline figure keys per scenario, in display order, with a label + tone. */
const HEADLINE: Record<string, { key: string; label: string; tone?: Tone; unit?: string }[]> = {
  "berth-cascade": [
    { key: "calls_displaced", label: "Calls displaced", tone: "warn" },
    { key: "cumulative_delay_hours", label: "Cumulative delay", tone: "critical", unit: "h" },
    { key: "max_single_delay_hours", label: "Worst single delay", tone: "warn", unit: "h" },
    { key: "target_delay_hours", label: "Target call overrun", tone: "info", unit: "h" },
    { key: "calls_in_window", label: "Calls in window", tone: "neutral" },
  ],
  "crane-productivity": [
    { key: "baseline_moves_per_hour", label: "Baseline moves/hour", tone: "ok" },
    { key: "reduced_moves_per_hour", label: "Reduced moves/hour", tone: "warn" },
    { key: "turnaround_increase_hours", label: "Turnaround increase", tone: "critical", unit: "h" },
    { key: "cumulative_berth_delay_hours", label: "Berth queue delay", tone: "critical", unit: "h" },
    { key: "calls_displaced", label: "Calls displaced", tone: "warn" },
  ],
  "modal-shift": [
    { key: "additional_truck_trips", label: "Additional truck trips", tone: "warn" },
    { key: "shifted_teus", label: "TEU shifted to road", tone: "info" },
    { key: "shifted_peak", label: "Peak after shift", tone: "critical", unit: "/h" },
    { key: "saturated_hours_after", label: "Saturated hours (after)", tone: "critical" },
    { key: "saturated_hours_before", label: "Saturated hours (before)", tone: "neutral" },
  ],
  "gate-slotting": [
    { key: "observed_peak", label: "Observed peak", tone: "critical", unit: "/h" },
    { key: "sustained_rate_per_hour", label: "Sustained rate", tone: "info", unit: "/h" },
    { key: "saturated_hours", label: "Saturated hours", tone: "warn" },
    { key: "slotted_peak", label: "Peak after slotting", tone: "ok", unit: "/h" },
    { key: "peak_reduction_pct", label: "Peak reduction", tone: "ok", unit: "%" },
  ],
  "driver-shortage": [
    { key: "baseline_trips", label: "Baseline trips", tone: "neutral" },
    { key: "reduced_trips", label: "Trips after shortage", tone: "warn" },
    { key: "throughput_loss_pct", label: "Throughput loss", tone: "critical", unit: "%" },
    { key: "containers_not_evacuated", label: "Containers not evacuated", tone: "critical" },
    { key: "transporters_affected", label: "Transporters affected", tone: "info" },
  ],
};

function fmt(v: unknown, unit?: string): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "number") {
    const n = Number.isInteger(v) ? v.toLocaleString() : v.toFixed(2);
    return unit ? `${n}${unit.startsWith("/") || unit === "%" ? "" : " "}${unit}` : n;
  }
  return String(v);
}

function humanise(key: string): string {
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** The state the brief calls out explicitly: never render an empty panel. */
export function NoDataPanel({ result }: { result: SimulationResult }) {
  const failures = result.queries.filter((q) => q.error);
  const critical = failures.length > 0;
  return (
    <Card
      className={
        critical
          ? "flex flex-col gap-2 border-severity-critical/40 bg-severity-critical/5 p-4"
          : "flex flex-col gap-2 border-amber-300/60 bg-amber-50/60 p-4 dark:bg-amber-950/20"
      }
    >
      <div className="flex items-center gap-2">
        {critical ? (
          <AlertTriangle className="h-5 w-5 text-severity-critical" />
        ) : (
          <Database className="h-5 w-5 text-amber-600" />
        )}
        <h3 className="text-[14px] font-semibold text-foreground">
          {critical
            ? "Query failed — this is not an empty result"
            : "No data available for selected period"}
        </h3>
      </div>
      <p className="text-[12px] leading-snug text-muted-foreground">
        {critical
          ? "One or more queries behind this answer did not run. The figures below are absent because the query errored, not because the window is empty — do not read this as “nothing happened”."
          : "The simulation ran correctly and declined to produce a figure, because the data it needs is not present for this window. No number has been invented."}
      </p>
      {result.notes.length > 0 && (
        <ul className="flex list-disc flex-col gap-1 pl-5">
          {result.notes.map((n, i) => (
            <li key={i} className="text-[11.5px] leading-snug text-foreground/80">
              {n}
            </li>
          ))}
        </ul>
      )}
      <p className="border-t border-border/60 pt-2 text-[10.5px] text-muted-foreground">
        The assumptions and query trace below still apply — they show exactly what was asked of
        the database.
      </p>
    </Card>
  );
}

export function ResultCards({ result }: { result: SimulationResult }) {
  if (!result.data_available) return <NoDataPanel result={result} />;

  const spec = HEADLINE[result.scenario] ?? [];
  const shown = new Set(spec.map((s) => s.key));
  const rest = Object.entries(result.figures).filter(([k]) => !shown.has(k));

  return (
    <div className="flex flex-col gap-3">
      <StatGrid className="lg:grid-cols-5">
        {spec.map((s) => (
          <StatCard
            key={s.key}
            label={s.label}
            value={fmt(result.figures[s.key], s.unit)}
            tone={s.tone ?? "info"}
          />
        ))}
      </StatGrid>

      {result.notes.length > 0 && (
        <Card className="flex items-start gap-2 border-sky-300/50 bg-sky-50/50 p-2.5 dark:bg-sky-950/20">
          <Info className="mt-0.5 h-4 w-4 shrink-0 text-sky-600" />
          <ul className="flex flex-col gap-1">
            {result.notes.map((n, i) => (
              <li key={i} className="text-[11.5px] leading-snug text-foreground/80">
                {n}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {rest.length > 0 && (
        <Card className="p-3">
          <h4 className="mb-2 text-[12px] font-semibold text-foreground">
            All computed figures
          </h4>
          <div className="grid gap-x-4 gap-y-1.5 sm:grid-cols-2 lg:grid-cols-3">
            {rest.map(([k, v]) => (
              <div key={k} className="flex items-baseline justify-between gap-2 border-b border-border/50 pb-1">
                <span className="truncate text-[11px] text-muted-foreground" title={humanise(k)}>
                  {humanise(k)}
                </span>
                <span className="shrink-0 text-[12px] font-semibold tabular-nums text-foreground">
                  {fmt(v)}
                </span>
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
