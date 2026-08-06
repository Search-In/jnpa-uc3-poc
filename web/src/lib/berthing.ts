// Berthing Reports — blank-value semantics for the presentation layer (module 7).
//
// PURE presentation logic. It reads the /api/berthing response shape and decides how a
// BLANK cell should read to a control-room operator. It fetches nothing, writes nothing,
// and changes no API / DTO / schema / business rule — the row it receives is the row the
// backend produced.
//
// WHY THIS EXISTS
// ---------------
// A blank berthing cell is not one thing. Measured over the 25 real daily reports in
// client-data/7-Berthing Reports (429 parsed rows -> 185 distinct vessel calls), blanks
// fall into four populations that a single "—" placeholder collapses together:
//
//   1. NOT REPORTED  — the terminal's layout never carries the field. imo_number is
//                      absent from all five layouts; NSFT publishes no berth column;
//                      APMT/BMCT publish no machine-readable ops-completed time.
//   2. PENDING       — the field belongs to a lifecycle milestone the vessel has not
//                      reached yet. ATA before arrival, berth before allocation.
//   3. NOT REPUBLISHED — the terminal carried the value in an earlier section and stops
//                      restating it. ETA lives only in "Vessels Expected"; once a call
//                      moves to the on-berth/sailed section its ETA column disappears.
//   4. ANOMALY       — the blank (or the value) violates a business rule and warrants
//                      operator attention.
//
// The rules below are calibrated so that (4) stays rare and therefore meaningful: they
// fire on 3 of the 185 real calls (1.6%). Two naive rules were deliberately REJECTED
// because the corpus shows them to be wrong:
//
//   * "ETA has passed but ATA is unavailable" would flag 102/185 calls (55%). Waiting at
//     anchorage for tide/berth is routine at JNPA, and most long-overdue rows are an
//     artefact of OUR import cadence (5 snapshots spanning 15 days), not terminal error.
//     Handled instead by `arrivalWatch` as a graduated freshness signal.
//   * "Berth assigned but operational timestamps missing" would be wrong on its face:
//     BMCT reports berth-allocated vessels pre-arrival (e.g. "BMCT04 JOLLY BIANCO S0605
//     04-Jun PUP@06:30" — pilot pick-up booked, not yet alongside). BERTH_ASSIGNED
//     legitimately precedes ATA, so ATA only becomes due at BERTHING_STARTED.

import type { Tone } from "@/components/ui/dtccc";

/** The vessel-call lifecycle, lowest first. Mirrors core.berthing_record's CHECK
 *  constraint and services/berthing/lifecycle.py — read-only here. */
export const LIFECYCLE = [
  "EXPECTED",
  "ARRIVED",
  "BERTH_ASSIGNED",
  "BERTHING_STARTED",
  "CARGO_OPERATION",
  "COMPLETED",
  "DEPARTED",
] as const;

export type BerthingStatus = (typeof LIFECYCLE)[number];

const RANK: Record<string, number> = Object.fromEntries(LIFECYCLE.map((s, i) => [s, i]));

/** Ladder position, or -1 for an absent/unknown status (so it never wins a comparison). */
export function statusRank(status?: string | null): number {
  if (!status) return -1;
  return RANK[String(status).trim().toUpperCase()] ?? -1;
}

export type BerthingField =
  | "terminal"
  | "vessel_name"
  | "voyage_number"
  | "imo_number"
  | "shipping_line"
  | "berth_number"
  | "eta"
  | "ata"
  | "berthing_time"
  | "cargo_operation_start"
  | "cargo_operation_end"
  | "departure_time";

/** The subset of the /api/berthing row this module reads. Extra keys are ignored, so
 *  the response DTO can grow without touching this file. */
export interface BerthingRow {
  terminal?: string | null;
  vessel_name?: string | null;
  voyage_number?: string | null;
  imo_number?: string | null;
  shipping_line?: string | null;
  berth_number?: string | null;
  eta?: string | null;
  ata?: string | null;
  berthing_time?: string | null;
  departure_time?: string | null;
  cargo_operation_start?: string | null;
  cargo_operation_end?: string | null;
  status?: string | null;
}

/** Why a cell is blank — drives the placeholder text and its tone. */
export type FieldState = "value" | "pending" | "not-reported" | "anomaly";

export interface FieldVerdict {
  state: FieldState;
  /** Text to render when `state !== "value"`. */
  label: string;
  tone: Tone;
  /** Operator-facing explanation, surfaced as the cell's tooltip. */
  hint: string;
}

/** Timestamp formatting shared by every berthing surface (IST, 24-hour, day-month).
 *  Returns "" for a blank so callers can fall back to an explained placeholder. */
export function fmtTs(ts?: string | null): string {
  if (isBlank(ts)) return "";
  try {
    return new Date(String(ts)).toLocaleString("en-IN", {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  } catch {
    return String(ts);
  }
}

// ---------------------------------------------------------------- field taxonomy

/** Present on every row of the real corpus (429/429). A blank here means the import
 *  produced a row it should have rejected — a genuine anomaly. */
const MANDATORY: readonly BerthingField[] = ["terminal", "vessel_name", "voyage_number"];

/** Fields a terminal's published layout does not carry, verified across all 25 files.
 *  A blank is factual, never an anomaly, and never "pending" — it will never arrive. */
const NOT_CARRIED: Record<string, readonly BerthingField[]> = {
  APMT: ["imo_number", "shipping_line", "cargo_operation_end", "eta"],
  BMCT: ["imo_number", "shipping_line", "cargo_operation_end", "eta"],
  NSFT: ["imo_number", "shipping_line", "berth_number"],
  NSICT: ["imo_number", "shipping_line"],
  NSIGT: ["imo_number", "shipping_line"],
};

/** Per-field reason text for the `NOT_CARRIED` case, so the tooltip explains the gap
 *  rather than just asserting it. */
const NOT_CARRIED_HINT: Partial<Record<BerthingField, string>> = {
  imo_number: "No JNPA terminal publishes IMO numbers in the daily berthing report.",
  shipping_line: "Not captured from this terminal's report layout.",
  cargo_operation_end:
    "This terminal's report does not publish a machine-readable ops-completed time.",
  berth_number: "NSFT's daily report has no berth column — vessel calls are serial-numbered.",
  eta: "This terminal's forward-looking 'Vessels Expected' section is not imported.",
};

/** The milestone at which a timestamp becomes DUE. Blank before it → pending; blank at
 *  or after it → anomaly.
 *
 *  ATA is deliberately due at BERTHING_STARTED, not ARRIVED: BERTH_ASSIGNED can precede
 *  arrival (berth pre-allocation / pilot pick-up rows), so a berth-allocated vessel with
 *  no ATA is normal, not broken. Verified: 0 of 185 calls violate this. */
const DUE_AT: Partial<Record<BerthingField, BerthingStatus>> = {
  ata: "BERTHING_STARTED",
  berthing_time: "BERTHING_STARTED",
  cargo_operation_start: "CARGO_OPERATION",
  cargo_operation_end: "COMPLETED",
  departure_time: "DEPARTED",
};

/** Human label per milestone field, used in pending/anomaly messages. */
const FIELD_LABEL: Record<BerthingField, string> = {
  terminal: "Terminal",
  vessel_name: "Vessel name",
  voyage_number: "Voyage / VIA",
  imo_number: "IMO number",
  shipping_line: "Shipping line",
  berth_number: "Berth",
  eta: "ETA",
  ata: "ATA",
  berthing_time: "Berthing time",
  cargo_operation_start: "Ops commenced",
  cargo_operation_end: "Ops completed",
  departure_time: "Departure (ATD)",
};

function isBlank(v: unknown): boolean {
  return v === null || v === undefined || (typeof v === "string" && v.trim() === "");
}

function carries(terminal: string | null | undefined, field: BerthingField): boolean {
  const missing = NOT_CARRIED[String(terminal ?? "").toUpperCase()];
  return !missing || !missing.includes(field);
}

// ---------------------------------------------------------------- anomalies

export type AnomalyCode = "mandatory_missing" | "milestone_missing" | "sequence_invalid";

export interface Anomaly {
  field: BerthingField;
  code: AnomalyCode;
  message: string;
}

/** Ordered pairs that must never invert. `[later, earlier]` — firing means the later
 *  milestone is timestamped BEFORE the earlier one, which is physically impossible and
 *  indicates a source-report or extraction error. */
const SEQUENCE: ReadonlyArray<[BerthingField, BerthingField, string]> = [
  ["berthing_time", "ata", "Berthing recorded before arrival"],
  ["cargo_operation_start", "ata", "Cargo operations commenced before arrival"],
  ["cargo_operation_end", "cargo_operation_start", "Operations completed before they commenced"],
  ["departure_time", "ata", "Departure recorded before arrival"],
  ["departure_time", "cargo_operation_end", "Departure recorded before operations completed"],
];

function ms(v?: string | null): number | null {
  if (isBlank(v)) return null;
  const t = Date.parse(String(v));
  return Number.isNaN(t) ? null : t;
}

/**
 * Every business-rule violation on one vessel call. Empty for a healthy row.
 *
 * This is the single source of truth for the word "Anomaly" in the Berthing UI —
 * `classifyField` consults it, so a cell and its row badge can never disagree.
 */
export function callAnomalies(row: BerthingRow): Anomaly[] {
  const out: Anomaly[] = [];
  const terminal = row.terminal;

  for (const f of MANDATORY) {
    if (isBlank(row[f])) {
      out.push({
        field: f,
        code: "mandatory_missing",
        message: `${FIELD_LABEL[f]} is mandatory but was not extracted on import.`,
      });
    }
  }

  const rank = statusRank(row.status);
  for (const [field, dueAt] of Object.entries(DUE_AT) as [BerthingField, BerthingStatus][]) {
    if (!carries(terminal, field)) continue; // the terminal never publishes it
    if (rank >= RANK[dueAt] && isBlank(row[field])) {
      out.push({
        field,
        code: "milestone_missing",
        message: `${FIELD_LABEL[field]} is missing although the call has reached ${String(
          row.status,
        ).replace(/_/g, " ")}.`,
      });
    }
  }

  for (const [later, earlier, message] of SEQUENCE) {
    const a = ms(row[later]);
    const b = ms(row[earlier]);
    if (a !== null && b !== null && a < b) {
      out.push({ field: later, code: "sequence_invalid", message: `${message}.` });
    }
  }

  return out;
}

// ---------------------------------------------------------------- field classification

/**
 * How one cell should read. `state === "value"` means render the value normally; every
 * other state carries the placeholder text and the tooltip that explains it.
 */
export function classifyField(row: BerthingRow, field: BerthingField): FieldVerdict {
  const anomalies = callAnomalies(row);
  const hit = anomalies.find((a) => a.field === field);

  if (hit) {
    return { state: "anomaly", label: "Anomaly", tone: "critical", hint: hit.message };
  }

  if (!isBlank(row[field])) {
    return { state: "value", label: "", tone: "neutral", hint: "" };
  }

  // Blank, and no rule is violated — say WHY it is blank.
  if (!carries(row.terminal, field)) {
    return {
      state: "not-reported",
      label: "Not reported",
      tone: "neutral",
      hint:
        NOT_CARRIED_HINT[field] ??
        `${row.terminal ?? "This terminal"} does not report ${FIELD_LABEL[field]}.`,
    };
  }

  const rank = statusRank(row.status);

  // ETA is published only in the "Vessels Expected" section; a call past that stage
  // keeps the ETA it was imported with, or has none because it was first seen on berth.
  if (field === "eta" && rank > RANK.EXPECTED) {
    return {
      state: "not-reported",
      label: "Not reported",
      tone: "neutral",
      hint: "ETA is published only while the vessel is expected; this call was first seen on berth.",
    };
  }

  if (field === "berth_number") {
    return {
      state: "pending",
      label: "Not allocated",
      tone: "neutral",
      hint: "No berth has been allocated to this call yet.",
    };
  }

  const dueAt = DUE_AT[field];
  if (dueAt) {
    return {
      state: "pending",
      label: "Pending",
      tone: "neutral",
      hint: `${FIELD_LABEL[field]} is recorded at ${dueAt.replace(/_/g, " ").toLowerCase()}; this call is ${String(
        row.status ?? "not yet started",
      )
        .replace(/_/g, " ")
        .toLowerCase()}.`,
    };
  }

  return {
    state: "not-reported",
    label: "Not reported",
    tone: "neutral",
    hint: `${FIELD_LABEL[field]} was not present in the source report.`,
  };
}

// ---------------------------------------------------------------- arrival freshness

export type ArrivalLevel = "awaiting" | "overdue" | "unconfirmed";

export interface ArrivalWatch {
  level: ArrivalLevel;
  label: string;
  tone: Tone;
  hint: string;
  /** Whole hours elapsed since the published ETA. */
  hoursLate: number;
}

const HOUR = 3_600_000;

/**
 * A graduated confidence signal for a still-expected vessel whose ETA has passed —
 * deliberately NOT an anomaly.
 *
 * At JNPA a vessel routinely waits at anchorage for tide, berth or pilot, so a few hours
 * past ETA is business-as-usual. A very stale row usually means no later report has been
 * imported for that terminal, not that the vessel is missing: on the real corpus 46 of
 * 102 expected calls are past ETA and 30 of those by more than a week, purely because the
 * five available snapshots span fifteen days.
 *
 * Returns `null` when there is nothing to say (not expected, no ETA, already arrived, or
 * the ETA is still in the future).
 */
export function arrivalWatch(row: BerthingRow, now: number = Date.now()): ArrivalWatch | null {
  if (statusRank(row.status) !== RANK.EXPECTED) return null;
  if (!isBlank(row.ata)) return null;
  const eta = ms(row.eta);
  if (eta === null || eta > now) return null;

  const hoursLate = Math.floor((now - eta) / HOUR);
  if (hoursLate < 24) {
    return {
      level: "awaiting",
      label: "Awaiting arrival",
      tone: "neutral",
      hoursLate,
      hint: `ETA passed ${hoursLate}h ago. Waiting at anchorage for tide, berth or pilot is routine.`,
    };
  }
  if (hoursLate < 72) {
    return {
      level: "overdue",
      label: "Overdue",
      tone: "warn",
      hoursLate,
      hint: `ETA passed ${Math.floor(hoursLate / 24)}d ago with no reported arrival. Confirm the schedule with the terminal.`,
    };
  }
  return {
    level: "unconfirmed",
    label: "Unconfirmed",
    tone: "warn",
    hoursLate,
    hint: `ETA passed ${Math.floor(hoursLate / 24)}d ago and no later report has confirmed an arrival — most often a gap in imported daily reports rather than a missing vessel.`,
  };
}
