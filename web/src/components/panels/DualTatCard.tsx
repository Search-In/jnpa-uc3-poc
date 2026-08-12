// UC3-035 — the two turnaround definitions, rendered together. Always.
//
// UI-122: "neither can be displayed alone anywhere in the product". This
// component is the enforcement of that rule, which is why it takes no prop to
// select a definition and the API returns both arms in one payload: there is no
// code path here that can render one without the other.
//
// The rule is not pedantry. The two definitions measure different things:
//
//   terminal TAT — gate-in to gate-out. What the terminal controls.
//   driver TAT   — plaza entry to highway exit. What the driver actually
//                  experiences, including the plaza hold the terminal figure
//                  never sees.
//
// WHAT THIS CARD RENDERS, AND WHAT IT DOES NOT. Live Operations is a
// control-room surface: it shows the KPI name, its current value, its target
// and its baseline, and nothing else. The /api/kpi/dual-tat payload also
// carries engineering metadata — how each arm is derived (`method`), where the
// baseline came from (`baseline_source`), the internal render-rule note and ref,
// and the individual ground-truth marker records with their source-document and
// container identifiers. Those fields REMAIN in the API for the clients that
// need them (reports, audit, diagnostics); they are deliberately NOT read here.
// An operator on shift acts on the number and its target — provenance and
// derivation are not part of that decision, and printing them on the wallboard
// puts internal identifiers in front of an audience that cannot act on them.
//
// Nothing about the measurement changes: the values, targets and baselines
// rendered below are exactly what the endpoint computes.
import { useQuery } from "@tanstack/react-query";
import { Timer } from "lucide-react";

import { api } from "@/lib/api";
import { Card } from "@/components/ui/card";
import type { DualTatArm } from "@/lib/types";

function Arm({ arm, value }: { arm: DualTatArm; value: number | null }) {
  return (
    <div className="min-w-0 flex-1 rounded-lg border border-border bg-muted/20 p-2.5">
      <h4 className="text-[11px] font-semibold text-foreground">{arm.label}</h4>
      <p className="mt-0.5 text-[10px] text-muted-foreground">{arm.definition}</p>
      <p className="mt-1.5 text-2xl font-semibold tabular-nums">
        {value === null ? "—" : value}
        <span className="ml-1 text-[11px] font-normal text-muted-foreground">{arm.unit}</span>
      </p>
      <dl className="mt-1 flex flex-wrap gap-x-3 text-[10px] text-muted-foreground">
        <div>
          <dt className="inline">Target </dt>
          <dd className="inline font-medium text-foreground">
            {arm.target ?? "—"} {arm.unit}
          </dd>
        </div>
        <div>
          <dt className="inline">Baseline </dt>
          <dd className="inline font-medium text-foreground">
            {arm.baseline ?? "—"} {arm.unit}
          </dd>
        </div>
      </dl>
    </div>
  );
}

export default function DualTatCard() {
  const q = useQuery({
    queryKey: ["kpi-dual-tat"],
    queryFn: () => api.kpiDualTat(),
    refetchInterval: 30_000,
  });

  const d = q.data;
  // No live driver-leg aggregate is wired up yet, so both arms render their
  // target and baseline with no current value rather than a fabricated one.
  // `null` renders as an em-dash below — never as a zero, which would read as a
  // measured turnaround of nothing.
  const terminalValue: number | null = null;
  const driverValue: number | null = null;

  return (
    <Card className="min-w-0 p-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold">
          <Timer className="h-4 w-4 shrink-0" aria-hidden />
          Turn Around Time Inside Port
        </h3>
      </div>

      {q.isLoading && (
        <p className="mt-2 text-[12px] text-muted-foreground" role="status">
          Loading turnaround definitions…
        </p>
      )}
      {q.isError && (
        <p className="mt-2 text-[12px] text-severity-critical" role="alert">
          Turnaround KPIs unavailable: {(q.error as Error).message}
        </p>
      )}

      {d && (
        <>
          {/* Both arms, side by side. There is no branch that renders one. */}
          <div className="mt-2 flex flex-col gap-2 sm:flex-row">
            <Arm arm={d.pair.terminal} value={terminalValue} />
            <Arm arm={d.pair.driver} value={driverValue} />
          </div>
        </>
      )}
    </Card>
  );
}
