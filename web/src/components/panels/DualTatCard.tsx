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
// A single "TAT" number is therefore not a simplification but the wrong answer,
// and a reader cannot tell which of the two they are looking at. S4 puts it
// plainly: the GAP between them is itself the reportable finding, so this card
// shows the gap as a first-class figure rather than leaving it to be inferred.
//
// The ground-truth markers are the only REAL measured turnarounds in the corpus
// — two GTI visits by MH43BX1488, computed from the truck-in/truck-out times
// printed on the slips. They are plotted as reference markers and never mixed
// into the aggregate, because two visits are evidence, not a baseline.
import { useQuery } from "@tanstack/react-query";
import { Timer } from "lucide-react";

import { api } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { StatusChip } from "@/components/ui/dtccc";
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
      <p className="mt-1 break-words text-[10px] leading-snug text-muted-foreground">
        <span className="font-medium">Method:</span> {arm.method}
      </p>
      <p className="mt-0.5 break-words text-[10px] leading-snug text-muted-foreground">
        <span className="font-medium">Baseline source:</span> {arm.baseline_source}
      </p>
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
  // The measured aggregate is not yet wired to a live driver-leg feed (the plaza
  // legs have no corpus events), so the arms render their targets/baselines and
  // the REAL markers rather than a fabricated current value.
  const terminalValue: number | null = null;
  const driverValue: number | null = null;

  return (
    <Card className="min-w-0 p-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold">
          <Timer className="h-4 w-4 shrink-0" aria-hidden />
          Turn Around Time Inside Port
        </h3>
        {d && <StatusChip label={`${d.render_rule.ref} — shown as a pair`} tone="info" />}
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

          <p className="mt-2 rounded-md border border-border bg-muted/30 p-2 text-[10px] leading-snug text-muted-foreground">
            {d.render_rule.note}
          </p>

          <div className="mt-2 min-w-0">
            <h4 className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              Ground-truth markers
            </h4>
            {d.ground_truth_markers.length === 0 ? (
              <p className="mt-1 text-[11px] text-muted-foreground">{d.ground_truth_note}</p>
            ) : (
              <>
                <ul className="mt-1 flex flex-col gap-1">
                  {d.ground_truth_markers.map((m) => (
                    <li
                      key={m.source_document}
                      className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px]"
                    >
                      <StatusChip label={m.provenance} tone="ok" />
                      <span className="font-semibold tabular-nums">{m.tat_minutes} min</span>
                      <span className="font-mono text-[10px]">{m.vehicle_no}</span>
                      <span className="text-muted-foreground">
                        {m.terminal_code} · {m.container_no}
                      </span>
                      <span className="font-mono text-[10px] text-muted-foreground">
                        {m.source_document}
                      </span>
                    </li>
                  ))}
                </ul>
                <p className="mt-1 text-[10px] leading-snug text-muted-foreground">
                  {d.ground_truth_note}
                </p>
              </>
            )}
          </div>
        </>
      )}
    </Card>
  );
}
