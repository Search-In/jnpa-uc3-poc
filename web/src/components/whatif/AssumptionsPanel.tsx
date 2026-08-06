// Assumptions panel — JNPA Notice §1.c.
//
//   "Every assumption made, stated explicitly and separately from the result.
//    Where the data does not carry a value your method requires, say so and state
//    what you assumed in its place. An assumption declared openly will be treated
//    more favourably than a figure presented without one."
//
// So this panel is not decoration — it is the part of the answer the Notice
// explicitly rewards. Two design consequences:
//
//   * ASSUMED is colour-separated from MEASURED. An evaluator must be able to see
//     at a glance which numbers came from JNPA data and which are our stand-ins.
//   * The `reason` is shown in full, never truncated. The reason IS the argument.

import type { SimAssumption, SimAssumptionSource } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { StatusChip, type Tone } from "@/components/ui/dtccc";

const SOURCE_TONE: Record<SimAssumptionSource, Tone> = {
  MEASURED: "ok", // read straight from a JNPA-sourced column
  DERIVED: "info", // computed from JNPA rows by a stated rule
  PARAMETER: "neutral", // supplied by the operator in the request
  ASSUMED: "warn", // not in the data at all — the declared stand-in
};

const SOURCE_HELP: Record<SimAssumptionSource, string> = {
  MEASURED: "Read directly from a JNPA-sourced column.",
  DERIVED: "Computed from JNPA rows by the stated rule.",
  PARAMETER: "Supplied in the request by the operator.",
  ASSUMED: "Not present in the data — this is the declared stand-in value.",
};

function renderValue(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export function AssumptionsPanel({ assumptions }: { assumptions: SimAssumption[] }) {
  if (!assumptions.length) {
    return (
      <Card className="p-3">
        <h3 className="text-[13px] font-semibold text-foreground">Assumptions</h3>
        <p className="mt-1 text-[11.5px] text-muted-foreground">
          This answer declared no assumptions — every figure came straight from the data.
        </p>
      </Card>
    );
  }

  const assumed = assumptions.filter((a) => a.source === "ASSUMED").length;

  return (
    <Card className="p-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-[13px] font-semibold text-foreground">
          Assumptions <span className="text-muted-foreground">({assumptions.length})</span>
        </h3>
        {assumed > 0 && (
          <span className="text-[10.5px] font-medium text-amber-600">
            {assumed} value{assumed === 1 ? "" : "s"} not present in the data — declared below
          </span>
        )}
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[520px] border-collapse text-left">
          <thead>
            <tr className="border-b border-border">
              <th className="py-1.5 pr-3 text-[11px] font-semibold text-muted-foreground">Field</th>
              <th className="py-1.5 pr-3 text-[11px] font-semibold text-muted-foreground">Value</th>
              <th className="py-1.5 pr-3 text-[11px] font-semibold text-muted-foreground">Source</th>
              <th className="py-1.5 text-[11px] font-semibold text-muted-foreground">Reason</th>
            </tr>
          </thead>
          <tbody>
            {assumptions.map((a, i) => (
              <tr key={`${a.field}-${i}`} className="border-b border-border/50 align-top">
                <td className="py-1.5 pr-3 text-[11.5px] font-medium text-foreground">{a.field}</td>
                <td className="py-1.5 pr-3 text-[11.5px] font-semibold tabular-nums text-foreground">
                  {renderValue(a.value)}
                </td>
                <td className="py-1.5 pr-3">
                  <span title={SOURCE_HELP[a.source] ?? a.source}>
                    <StatusChip label={a.source} tone={SOURCE_TONE[a.source] ?? "neutral"} />
                  </span>
                </td>
                <td className="py-1.5 text-[11px] leading-snug text-muted-foreground">{a.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="mt-2 border-t border-border pt-2 text-[10.5px] leading-snug text-muted-foreground">
        MEASURED = from a JNPA column · DERIVED = computed by a stated rule · PARAMETER = entered
        above · ASSUMED = not in the data, declared here.
      </p>
    </Card>
  );
}
