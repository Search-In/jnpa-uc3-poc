// Customs disposition as UC-III reads it.
//
// There is no FLAGGED customs status in the system: core.cargo.customs_status is
// CHECKed to PENDING / CLEARED / HELD / UNDER_INSPECTION. "Flagged by customs"
// is the operator wording for the two dispositions that mean customs has stopped
// the box, which the backend already defines once (services/cargo/service.py
// CUSTOMS_BLOCKS_RELEASE) and enforces on POST /api/jobs as `customs_flagged`.
//
// This module derives the banner from that REJECTION, so the UI never restates
// the rule or guesses a reason — it renders exactly what customs recorded.

import type { ApiErrorInfo } from "./api";

/** Wording the ticket requires whenever customs has stopped the container. */
export const CUSTOMS_FLAGGED_MESSAGE = "Flagged by customs";

export interface CustomsBlock {
  /** e.g. "Vehicle assignment blocked — Flagged by customs" */
  message: string;
  /** The remark customs recorded, or null when there is none on record. */
  note: string | null;
  /** HELD | UNDER_INSPECTION — whichever disposition the backend reported. */
  status: string | null;
  /** The container the refusal was raised for, so a different one clears it. */
  container: string | null;
}

/**
 * A `customs_flagged` rejection turned into the blocking banner, or null when
 * the refusal was about something else (vehicle busy, PDP, paperwork…), which
 * keeps rendering through the normal check list.
 */
export function customsBlock(err: ApiErrorInfo): CustomsBlock | null {
  if (err.code !== "customs_flagged") return null;
  const note = err.extra?.customs_note;
  const status = err.extra?.customs_status;
  const container = err.extra?.container_number;
  return {
    message: `Vehicle assignment blocked — ${CUSTOMS_FLAGGED_MESSAGE}`,
    note: typeof note === "string" && note.trim() ? note.trim() : null,
    status: typeof status === "string" ? status : null,
    container: typeof container === "string" ? container : null,
  };
}
