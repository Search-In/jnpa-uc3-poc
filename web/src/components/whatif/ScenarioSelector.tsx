// Scenario selector — the top strip of the Cargo What-If Dashboard.
//
// Cards are built ENTIRELY from GET /api/cargo/simulate/scenarios: the JNPA
// reference (I-B / II-A / …), the question each scenario answers, and the tables
// it reads all come from the backend catalog. Nothing here is hardcoded, so a
// scenario registered in services/cargo/simulation/REGISTRY appears the moment
// the gateway serves it.

import type { SimScenarioEntry } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

export function ScenarioSelector({
  scenarios,
  value,
  onChange,
  disabled,
}: {
  scenarios: SimScenarioEntry[];
  value: string | null;
  onChange: (scenario: string) => void;
  disabled?: boolean;
}) {
  if (!scenarios.length) return null;
  return (
    <div className="grid gap-2.5 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-5">
      {scenarios.map((s) => {
        const active = s.scenario === value;
        return (
          <Card
            key={s.scenario}
            role="button"
            tabIndex={0}
            aria-pressed={active}
            aria-label={`${s.jnpa_reference} — ${s.scenario}`}
            onClick={() => !disabled && onChange(s.scenario)}
            onKeyDown={(e) => {
              if (!disabled && (e.key === "Enter" || e.key === " ")) {
                e.preventDefault();
                onChange(s.scenario);
              }
            }}
            className={cn(
              "flex cursor-pointer flex-col gap-1.5 p-3 transition-colors",
              active
                ? "border-primary bg-primary/5 ring-1 ring-primary/30"
                : "hover:bg-muted/50",
              disabled && "pointer-events-none opacity-60",
            )}
          >
            <div className="flex items-start justify-between gap-2">
              <span
                className={cn(
                  "rounded px-1.5 py-0.5 text-[10.5px] font-bold uppercase tracking-wide",
                  active ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground",
                )}
              >
                {jnpaCode(s.jnpa_reference)}
              </span>
              <span className="text-[10.5px] font-medium text-muted-foreground">
                {s.reads.length} {s.reads.length === 1 ? "source" : "sources"}
              </span>
            </div>
            <div className="text-[13px] font-semibold leading-tight text-foreground">
              {jnpaTitle(s.jnpa_reference)}
            </div>
            <p className="line-clamp-3 text-[11.5px] leading-snug text-muted-foreground">
              {s.question}
            </p>
          </Card>
        );
      })}
    </div>
  );
}

/** "I-B — Extended Berth Window" → "I-B". The catalog string is authored by the
 *  backend as `<code> — <title>`; both halves are shown, split for emphasis. */
function jnpaCode(reference: string): string {
  return reference.split("—")[0]?.trim() || reference;
}

function jnpaTitle(reference: string): string {
  const parts = reference.split("—");
  return parts.length > 1 ? parts.slice(1).join("—").trim() : reference;
}
