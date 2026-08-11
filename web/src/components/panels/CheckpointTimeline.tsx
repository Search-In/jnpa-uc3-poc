// UC3-025 — the ten-checkpoint visit timeline with per-step evidence labels.
//
// A component rather than a screen: the timeline belongs wherever a visit is
// already in context (T-04 Truck Visit Detail today), not on a surface of its
// own. It renders whatever /api/trip/{id} returns and owns no fetching, so it
// can be dropped beside any existing visit view.
//
// The labels are the point of the ticket. No container crosses all ten
// checkpoints in the supplied corpus — the enroute and plaza steps have no
// events at all (gaps G6/G9) — so a timeline that showed ten neat timestamps
// would be inventing at least four of them. Each step therefore says which of
// three things is true:
//
//   VERIFIED       a real corpus timestamp backs this step
//   KEY ONLY       the document evidences the step but prints no time
//   NOT IN CORPUS  the corpus has no source for this step
//
// Dwell is shown only between consecutive TIMED steps; a duration spanning a
// NOT_IN_CORPUS gap would be a fabricated number, so it is left blank.
import { CircleCheck, CircleDashed, CircleHelp } from "lucide-react";

import { cn, fmtDateTimeIST } from "@/lib/utils";
import { StatusChip, type Tone } from "@/components/ui/dtccc";
import type { EvidenceLabel, TimelineStep, TripDetail } from "@/lib/types";

const EVIDENCE_TONE: Record<EvidenceLabel, Tone> = {
  VERIFIED: "ok",
  KEY_ONLY: "warn",
  NOT_IN_CORPUS: "neutral",
};

const EVIDENCE_LABEL: Record<EvidenceLabel, string> = {
  VERIFIED: "VERIFIED",
  KEY_ONLY: "KEY ONLY",
  NOT_IN_CORPUS: "NOT IN CORPUS",
};

const EVIDENCE_ICON: Record<EvidenceLabel, typeof CircleCheck> = {
  VERIFIED: CircleCheck,
  KEY_ONLY: CircleHelp,
  NOT_IN_CORPUS: CircleDashed,
};

const ICON_COLOUR: Record<EvidenceLabel, string> = {
  VERIFIED: "text-emerald-600 dark:text-emerald-400",
  KEY_ONLY: "text-amber-600 dark:text-amber-400",
  NOT_IN_CORPUS: "text-muted-foreground/50",
};

export function CheckpointTimelineSteps({ steps }: { steps: TimelineStep[] }) {
  return (
    <ol className="flex flex-col">
      {steps.map((s, i) => {
        const Icon = EVIDENCE_ICON[s.evidence];
        const last = i === steps.length - 1;
        return (
          <li key={s.key} className="flex min-w-0 gap-2.5">
            <div className="flex shrink-0 flex-col items-center">
              <Icon className={cn("h-4 w-4", ICON_COLOUR[s.evidence])} aria-hidden />
              {!last && <span className="my-0.5 w-px flex-1 bg-border" />}
            </div>
            <div className={cn("min-w-0 flex-1", last ? "pb-0" : "pb-3")}>
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className="text-[12px] font-medium">{s.label}</span>
                <StatusChip label={EVIDENCE_LABEL[s.evidence]} tone={EVIDENCE_TONE[s.evidence]} />
                {s.dwell_minutes !== null && (
                  <span className="text-[10px] text-muted-foreground">+{s.dwell_minutes} min</span>
                )}
              </div>
              <p className="mt-0.5 text-[11px] tabular-nums text-foreground">
                {s.ts ? fmtDateTimeIST(s.ts) : "—"}
              </p>
              {s.detail && (
                <p className="mt-0.5 break-words text-[10px] leading-snug text-muted-foreground">
                  {s.detail}
                  {s.source && <span className="ml-1 font-mono">({s.source})</span>}
                </p>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/** The timeline plus its evidence summary, for a visit already in context. */
export default function CheckpointTimeline({
  trip,
  loading,
  error,
}: {
  trip?: TripDetail | null;
  loading?: boolean;
  error?: Error | null;
}) {
  if (loading) {
    return (
      <p className="p-3 text-[12px] text-muted-foreground" role="status">
        Loading checkpoint timeline…
      </p>
    );
  }
  if (error) {
    return (
      <p className="p-3 text-[12px] text-severity-critical" role="alert">
        Checkpoint timeline unavailable: {error.message}
      </p>
    );
  }
  if (!trip) {
    return (
      <p className="p-3 text-[12px] text-muted-foreground">
        Select a document to see its checkpoint timeline.
      </p>
    );
  }

  const s = trip.timeline_summary;
  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-center gap-1.5">
        <StatusChip label={`${s.verified} verified`} tone="ok" />
        <StatusChip label={`${s.key_only} key-only`} tone="warn" />
        <StatusChip label={`${s.not_in_corpus} not in corpus`} tone="neutral" />
      </div>
      {s.in_gate_minutes !== null && (
        <p className="mt-1.5 text-[11px] text-muted-foreground">
          In-gate time{" "}
          <span className="font-semibold tabular-nums text-foreground">
            {s.in_gate_minutes} min
          </span>{" "}
          (recognition portal → gate out)
        </p>
      )}
      <div className="mt-3">
        <CheckpointTimelineSteps steps={trip.timeline} />
      </div>
      <p className="mt-2 border-t border-border pt-2 text-[10px] leading-snug text-muted-foreground">
        {s.note}
      </p>
    </div>
  );
}
