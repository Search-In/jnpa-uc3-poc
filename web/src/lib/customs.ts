// Customs disposition as UC-III reads it.
//
// There is no FLAGGED customs status in the system: core.cargo.customs_status is
// CHECKed to PENDING / CLEARED / HELD / UNDER_INSPECTION. "Flagged by Customs"
// is the operator wording for the two dispositions that mean customs has stopped
// the box, which the backend already defines once (services/cargo/service.py
// CUSTOMS_BLOCKS_RELEASE) and enforces on POST /api/jobs as `customs_flagged`.
//
// This module derives the banner from that REJECTION, so the UI never restates
// the rule or guesses a reason — it renders exactly what the backend refused
// with. The reason and the explanatory sentence are now sent BY the gateway
// (services/container_job/service.py CUSTOMS_FLAGGED_MESSAGE / _DETAIL); the
// constants below are the fallback for a response that predates them, never a
// second copy of the rule.

import type { ApiErrorInfo } from "./api";

/** Wording the ticket requires whenever customs has stopped the container. */
export const CUSTOMS_FLAGGED_MESSAGE = "Flagged by Customs";

/** The full sentence, matching the gateway's `message`. */
export const CUSTOMS_FLAGGED_DETAIL =
  "Vehicle and driver assignment is blocked because this container is flagged by Customs.";

export interface CustomsBlock {
  /** The headline reason, as the backend named it: "Flagged by Customs". */
  reason: string;
  /** The sentence explaining what is blocked and why. */
  message: string;
  /** The remark customs recorded, or null when there is none on record. */
  note: string | null;
  /** HELD | UNDER_INSPECTION — whichever disposition the backend reported. */
  status: string | null;
  /** The container the refusal was raised for, so a different one clears it. */
  container: string | null;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

/**
 * A `customs_flagged` rejection turned into the blocking banner, or null when
 * the refusal was about something else (vehicle busy, PDP, paperwork…), which
 * keeps rendering through the normal check list.
 */
export function customsBlock(err: ApiErrorInfo): CustomsBlock | null {
  if (err.code !== "customs_flagged") return null;
  return {
    reason: str(err.extra?.reason) ?? CUSTOMS_FLAGGED_MESSAGE,
    message: str(err.extra?.message) ?? CUSTOMS_FLAGGED_DETAIL,
    note: str(err.extra?.customs_note),
    status: str(err.extra?.customs_status),
    container: str(err.extra?.container_number),
  };
}
