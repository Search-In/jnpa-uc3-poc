// UC3-030 — the e-Challan SIMULATED badge.
//
// EC-5: "e-Challan workflow (badge: SIMULATED in PoC)". Assumption A5: the
// authority to issue a challan rests with JNPA/RTO, and integration happens only
// once JNPA confirms that authority. So no challan this system produces has been
// issued to anyone, and the requirement is that NO screen and NO export shows a
// challan without saying so.
//
// The component takes the disclosure from the API payload rather than hard-coding
// the wording: the gateway attaches it to every challan-bearing response
// (gateway/enforcement.py::challan_disclosure), and the exported PDF prints the
// same strings. One source, three surfaces — so the badge cannot say one thing on
// screen and another on paper.
//
// It renders nothing when there is no challan, and falls back to the canonical
// wording if an older response omits the block: a challan with a missing
// disclosure must still be badged, never silently shown bare.
import { ShieldAlert } from "lucide-react";

import { cn } from "@/lib/utils";

/** The disclosure block the gateway attaches beside a challan number. */
export interface ChallanDisclosureLike {
  issuance_mode?: string;
  badge?: string;
  is_legal_instrument?: boolean;
  authority_note?: string;
  assumption_ref?: string;
  disclosure?: string;
}

/** Used when a response predates the disclosure block. Same text as the server. */
const FALLBACK = {
  badge: "SIMULATED",
  authority_note: "issuance authority: JNPA/RTO — assumption A5",
  disclosure:
    "Challan issuance is a workflow demonstration; the state machine and audit " +
    "are production-real. No challan shown here has been issued to any authority.",
};

export default function ChallanSimulatedBadge({
  challanNo,
  disclosure,
  compact = false,
  className,
}: {
  /** Rendering is skipped entirely when there is no challan to badge. */
  challanNo?: string | null;
  disclosure?: ChallanDisclosureLike | null;
  /** Ribbon only, for tight rows; the full block adds the footnote. */
  compact?: boolean;
  className?: string;
}) {
  if (!challanNo) return null;

  const badge = disclosure?.badge ?? FALLBACK.badge;
  const authority = disclosure?.authority_note ?? FALLBACK.authority_note;
  const text = disclosure?.disclosure ?? FALLBACK.disclosure;

  return (
    <div
      className={cn(
        "min-w-0 rounded-md border-2 border-severity-warning/60 bg-severity-warning/10 p-2",
        className,
      )}
      // Announced as a group so a screen reader reads the badge with the
      // challan it qualifies, not as a stray decoration elsewhere on the page.
      role="note"
      aria-label={`Challan ${challanNo} is ${badge}`}
    >
      <p className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-severity-warning">
        <ShieldAlert className="h-3.5 w-3.5 shrink-0" aria-hidden />
        {badge} — not a legally issued challan
      </p>
      <p className="mt-0.5 break-words text-[10px] leading-snug text-muted-foreground">
        {authority}
      </p>
      {!compact && (
        <p className="mt-1 break-words border-t border-severity-warning/30 pt-1 text-[10px] leading-snug text-muted-foreground">
          {text}
        </p>
      )}
    </div>
  );
}
