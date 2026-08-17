/**
 * Presentation rules for the Evidence & Audit Explorer (S-06).
 *
 * These live apart from the screen because they are the part that can lie. The
 * thread API answers three DIFFERENT things and the UI must never blur them:
 *
 *   FOUND          the corpus evidences this step
 *   NOT_IN_CORPUS  we looked, and JNPA supplied nothing — an ANSWER
 *   ERROR          we could not look — a FAULT
 *
 * Collapsing ERROR into NOT_IN_CORPUS is the specific failure this module
 * exists to prevent: it would report a broken query as "JNPA has no data",
 * which reads as a finding about the client's corpus rather than a bug in ours.
 *
 * The vehicle rules matter for the same reason. A plate is always real — it is
 * read off a gate document or a CODECO message. What is often assumed is the
 * TRANSPORTER behind it, because `11-Transport Data` carries no vehicle
 * registration column at all (defect B1), so no plate can be resolved through
 * JNPA's own masters. The chip must therefore qualify the attribution, never
 * the truck.
 */
import type { ThreadVehicle } from "@/lib/types";

export type ThreadTone = "ok" | "warn" | "neutral";

export const VERDICT_TONE: Record<string, ThreadTone> = {
  FOUND: "ok",
  NOT_IN_CORPUS: "neutral",
  ERROR: "warn",
};

export const VERDICT_LABEL: Record<string, string> = {
  FOUND: "Evidenced",
  NOT_IN_CORPUS: "Not in corpus",
  ERROR: "Could not read",
};

/** Label for a hop verdict. An unrecognised verdict is shown verbatim rather
 *  than guessed at — an unknown state must look unknown, not look fine. */
export function verdictLabel(verdict: string): string {
  return VERDICT_LABEL[verdict] ?? verdict;
}

/** Tone for a hop verdict. Unknown verdicts are never toned "ok". */
export function verdictTone(verdict: string): ThreadTone {
  return VERDICT_TONE[verdict] ?? "neutral";
}

/**
 * How a truck's transporter attribution should be described.
 *
 * Returns the chip text and tone; `ok` is reserved for an attribution that a
 * named JNPA document actually supports.
 */
export function describeVehicleAttribution(v: ThreadVehicle): {
  label: string;
  tone: ThreadTone;
} {
  if (v.provenance === "DOCUMENT_EVIDENCED") {
    return {
      label: `transporter from ${v.source_ref ?? "a JNPA document"}`,
      tone: "ok",
    };
  }
  if (!v.transporter) return { label: "unmapped", tone: "warn" };
  return {
    label: `transporter assumed${v.assumption_ref ? ` (${v.assumption_ref})` : ""}`,
    tone: "warn",
  };
}
