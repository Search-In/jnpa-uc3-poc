import type { ScenarioStep } from "@/lib/types";
import { Card } from "@/components/ui/card";
import { StatusChip } from "@/components/ui/dtccc";
import { Check, CircleDashed, TriangleAlert, X, Warehouse } from "lucide-react";

// TFC-4 · UC-3 Execution panel.
//
// This renders the progression of the UC-3 backend run and NOTHING ELSE. Every
// value shown is read out of the scenario timeline steps that
// `scenarios/tfc4.py` recorded, which are themselves the responses the existing
// UC-3 endpoints returned (/api/yard/capacity/board, /adjust, /evaluate,
// /release). There is no client-side simulation, no animation timer and no
// fallback constant: a phase the backend has not reached yet renders as
// "pending", and a figure the backend did not return renders as "—".
//
// The panel keys off `step.detail.phase`, which tfc4.py stamps on every step,
// so it stays correct if step titles or ordering are ever reworded.

type Phase =
  | "baseline"
  | "arrivals"
  | "peak"
  | "detect"
  | "alert"
  | "hold"
  | "parking"
  | "notify"
  | "release"
  | "complete"
  | "reset";

/** The checklist, in the order the backend performs it. */
const PHASES: { phase: Phase; label: string }[] = [
  { phase: "baseline", label: "Yard baseline" },
  { phase: "arrivals", label: "Truck arrivals injected into AT_GATE_QUEUE" },
  { phase: "peak", label: "Yard utilization raised to peak" },
  { phase: "detect", label: "Truck arrival pressure detected" },
  { phase: "alert", label: "TRAFFIC_CONGESTION alert" },
  { phase: "hold", label: "Affected trucks evaluated & held" },
  { phase: "parking", label: "Authorized parking (CPP) recommended" },
  { phase: "notify", label: "Driver advisory dispatched" },
  { phase: "release", label: "Yard capacity recovered — trucks released" },
  { phase: "complete", label: "Scenario completed" },
];

type Detail = Record<string, any>;

function detailFor(steps: ScenarioStep[], phase: Phase): Detail | null {
  // Last wins: a re-run of the same phase (or the reset step) supersedes.
  for (let i = steps.length - 1; i >= 0; i--) {
    const d = (steps[i].detail ?? {}) as Detail;
    if (d.phase === phase) return d;
  }
  return null;
}

function statusFor(steps: ScenarioStep[], phase: Phase): string | null {
  for (let i = steps.length - 1; i >= 0; i--) {
    const d = (steps[i].detail ?? {}) as Detail;
    if (d.phase === phase) return steps[i].status;
  }
  return null;
}

function PhaseIcon({ status }: { status: string | null }) {
  if (status === null)
    return <CircleDashed className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />;
  if (status === "failed") return <X className="h-4 w-4 shrink-0 text-severity-crit" aria-hidden />;
  if (status === "degraded")
    return <TriangleAlert className="h-4 w-4 shrink-0 text-severity-warning" aria-hidden />;
  return <Check className="h-4 w-4 shrink-0 text-severity-ok" aria-hidden />;
}

/** A measured figure, or an explicit em-dash. NEVER a substituted default. */
function Fact({ label, value, unit }: { label: string; value: unknown; unit?: string }) {
  const shown =
    value === null || value === undefined || value === "" ? "—" : `${value}${unit ?? ""}`;
  return (
    <div className="rounded-md border border-border bg-muted/40 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="font-mono text-[13px] font-semibold text-foreground">{shown}</div>
    </div>
  );
}

export function Uc3ExecutionPanel({ steps, running }: { steps: ScenarioStep[]; running: boolean }) {
  const baseline = detailFor(steps, "baseline");
  const peak = detailFor(steps, "peak");
  const detect = detailFor(steps, "detect");
  const alert = detailFor(steps, "alert");
  const hold = detailFor(steps, "hold");
  const parking = detailFor(steps, "parking");
  const notify = detailFor(steps, "notify");
  const release = detailFor(steps, "release");
  const complete = detailFor(steps, "complete");
  const reset = detailFor(steps, "reset");

  // The most recent yard reading any phase reported — the live figures.
  const yard: Detail =
    (reset?.yard as Detail) ||
    (complete?.yard as Detail) ||
    (release?.yard as Detail) ||
    (detect?.yard as Detail) ||
    (peak?.yard as Detail) ||
    (baseline?.yard as Detail) ||
    {};

  const summary = (complete?.summary ?? {}) as Detail;
  const done = !!complete || !!reset;

  return (
    <Card className="overflow-hidden" data-testid="uc3-execution-panel">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <Warehouse className="h-4 w-4 text-primary" />
        <h3 className="text-sm font-semibold">UC-3 Execution</h3>
        <span className="ml-auto">
          {reset ? (
            <StatusChip label="RESET" tone="neutral" />
          ) : done ? (
            <StatusChip label="COMPLETED" tone="ok" />
          ) : running ? (
            <StatusChip label="RUNNING" tone="info" />
          ) : (
            <StatusChip label="IDLE" tone="neutral" />
          )}
        </span>
      </div>

      {/* Progression checklist — driven by detail.phase, not by a timer. */}
      <ol className="divide-y divide-border/60">
        {PHASES.map(({ phase, label }) => {
          const st = statusFor(steps, phase);
          const d = detailFor(steps, phase);
          return (
            <li key={phase} className="flex items-start gap-2 px-3 py-1.5 text-[13px]">
              <PhaseIcon status={st} />
              <span className={st === null ? "text-muted-foreground" : "text-foreground"}>
                {label}
                {phase === "baseline" && d?.yard ? (
                  <span className="ml-1 font-mono text-muted-foreground">
                    {d.yard.utilization_pct}% {d.yard.capacity_status}
                  </span>
                ) : null}
                {phase === "peak" && d?.yard ? (
                  <span className="ml-1 font-mono text-muted-foreground">
                    {d.yard.utilization_pct}% {d.yard.capacity_status}
                  </span>
                ) : null}
                {phase === "hold" && d ? (
                  <span className="ml-1 font-mono text-muted-foreground">{d.held_count} held</span>
                ) : null}
                {phase === "parking" && d ? (
                  <span className="ml-1 font-mono text-muted-foreground">
                    {d.facility_id ?? "none available"}
                  </span>
                ) : null}
                {phase === "release" && d ? (
                  <span className="ml-1 font-mono text-muted-foreground">
                    {d.released_count} released
                  </span>
                ) : null}
              </span>
              {st === null && running && (
                <span className="ml-auto text-[10px] uppercase tracking-wide text-muted-foreground">
                  pending
                </span>
              )}
            </li>
          );
        })}
      </ol>

      {/* Actual backend values — every one of these is read from a step detail. */}
      <div className="grid grid-cols-2 gap-2 border-t border-border p-3 sm:grid-cols-3 lg:grid-cols-5">
        <Fact label="Utilization" value={yard.utilization_pct} unit="%" />
        <Fact label="Yard status" value={yard.capacity_status} />
        <Fact label="Capacity" value={yard.capacity_slots} />
        <Fact label="Occupied" value={yard.occupied_slots} />
        <Fact label="Available" value={yard.available_slots} />
        <Fact label="Headroom" value={yard.headroom_slots} />
        <Fact label="Arrivals" value={detect?.arrivals?.total ?? summary.arrivals_evaluated} />
        <Fact label="Pressure" value={detect?.congestion_pressure} />
        <Fact label="Held" value={hold?.held_count ?? summary.held_count} />
        <Fact label="Released" value={release?.released_count ?? summary.released_count} />
        <Fact label="Parking" value={parking?.facility_id ?? (parking ? "none available" : null)} />
        <Fact label="Parking free" value={parking?.available} />
        <Fact
          label="Alert"
          value={
            alert?.alert_id ? String(alert.alert_id).slice(0, 8) : alert?.deduped ? "deduped" : null
          }
        />
        <Fact
          label="Notified"
          value={notify ? `${notify.notified ?? 0}/${notify.held_count ?? 0}` : null}
        />
        <Fact label="Capacity source" value={yard.capacity_source} />
      </div>

      {/* Provenance + the exact driver message the backend composed. */}
      <div className="space-y-1 border-t border-border px-3 py-2 text-[11px] text-muted-foreground">
        {hold?.reason ? (
          <p>
            <span className="font-medium text-foreground">Hold reason:</span> {hold.reason}
          </p>
        ) : null}
        {notify?.message_template ? (
          <p>
            <span className="font-medium text-foreground">Driver advisory:</span>{" "}
            {notify.message_template}
          </p>
        ) : null}
        {hold?.by_source ? (
          <p>
            Held by source — simulator: {hold.by_source["truck-sim"] ?? 0} · enrolled PWA:{" "}
            {hold.by_source["pwa-registered"] ?? 0}
          </p>
        ) : null}
        {detect && !detect.constrained && detect.detail_note ? (
          <p className="text-severity-warning">{detect.detail_note}</p>
        ) : null}
        {yard.capacity_declared ? (
          <p>
            Yard capacity is a declared figure ({String(yard.capacity_source)}); it is superseded by
            core.yard_block when a real block layout is loaded.
          </p>
        ) : null}
        <p>
          Values above are read from the UC-3 API responses recorded on this run&apos;s timeline —
          nothing on this panel is computed in the browser.
        </p>
      </div>
    </Card>
  );
}
