// Recommendations — the "what to do about it" half of a what-if answer.
//
// Rendered straight from the backend's `recommendations[]`. Each entry carries an
// `action` verb, a `reason` sentence, and any number of extra detail keys that
// vary per scenario (recovers_hours, constraint, transporter, …) — those are
// shown as chips rather than being mapped to a fixed schema, so a scenario can
// add a detail without a frontend change.

import { Lightbulb } from "lucide-react";
import type { SimRecommendation } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { StatusChip, type Tone } from "@/components/ui/dtccc";

/** NO_ACTION / ABSORBED are good news; everything else asks for intervention. */
function toneFor(action: string): Tone {
  const a = action.toUpperCase();
  if (a === "NO_ACTION" || a === "ABSORBED") return "ok";
  if (a.startsWith("RELIEVE") || a.startsWith("PROTECT") || a.startsWith("REINFORCE"))
    return "warn";
  return "info";
}

function humanise(key: string): string {
  return key.replace(/_/g, " ");
}

export function RecommendationList({ recommendations }: { recommendations: SimRecommendation[] }) {
  if (!recommendations.length) return null;

  return (
    <Card className="p-3">
      <h3 className="mb-2 flex items-center gap-1.5 text-[13px] font-semibold text-foreground">
        <Lightbulb className="h-4 w-4 text-amber-500" />
        Recommendations <span className="text-muted-foreground">({recommendations.length})</span>
      </h3>
      <ul className="flex flex-col gap-2">
        {recommendations.map((rec, i) => {
          const details = Object.entries(rec).filter(([k]) => k !== "action" && k !== "reason");
          return (
            <li key={`${rec.action}-${i}`} className="rounded-md border border-border p-2.5">
              <div className="mb-1 flex flex-wrap items-center gap-2">
                <StatusChip label={rec.action.replace(/_/g, " ")} tone={toneFor(rec.action)} />
              </div>
              <p className="text-[11.5px] leading-snug text-foreground/85">{rec.reason}</p>
              {details.length > 0 && (
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  {details.map(([k, v]) => (
                    <span
                      key={k}
                      className="rounded bg-muted px-1.5 py-0.5 text-[10.5px] text-muted-foreground"
                    >
                      <span className="font-medium text-foreground/70">{humanise(k)}:</span>{" "}
                      {Array.isArray(v) ? v.join(", ") : String(v)}
                    </span>
                  ))}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </Card>
  );
}
