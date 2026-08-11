/**
 * The one-sentence answer.
 *
 * A what-if result currently opens with a table of eighteen figures. An operator
 * who is not a port planner reads that as "here is some data", not as "here is
 * what happens". Every scenario therefore leads with a single plain sentence that
 * states the outcome, and the figures follow underneath for anyone who wants to
 * check it.
 *
 * Rules this file follows:
 *
 * - **No jargon.** "the gate absorbs the extra load", not "gate_absorbs_load: true".
 * - **A number and a consequence**, never a number alone. "563 trucks queued and
 *   six hours to clear" beats "peak_queue: 563.6".
 * - **Say when you cannot answer.** A refusal is a legitimate verdict and gets the
 *   same treatment as a result, so the reader is never left inferring from a blank
 *   panel.
 * - **Pure.** No React, no fetch, no formatting library. It takes the result
 *   envelope and returns strings, so it is trivially testable and portable to the
 *   UC-1 and UC-2 dashboards unchanged.
 *
 * SHARED FILE — the canonical copy lives in jnpa-uc3-poc. UC-1 (poc_1) and UC-2
 * (PoC_2) hold copies so all three dashboards word the same answer identically.
 * Change it here first.
 */

export type SimAssumptionSource = "MEASURED" | "DERIVED" | "ASSUMED" | "PARAMETER";

export interface VerdictInput {
  scenario: string;
  // Booleans are part of this contract: channel-closure reports
  // `berth_lock_reached` and modal-shift `gate_absorbs_load` as figures.
  figures: Record<string, number | string | boolean | null>;
  result: Record<string, any>;
  data_available: boolean;
  notes: string[];
}

export interface Verdict {
  /** The sentence. Always present, always readable on its own. */
  headline: string;
  /** Optional second line — the caveat or the next-most-useful fact. */
  detail?: string;
  /** Drives colour/weight only. Never the sole carrier of meaning. */
  tone: "ok" | "warning" | "critical" | "unavailable";
}

/* ------------------------------------------------------------------ helpers */

const num = (v: unknown): number | null =>
  typeof v === "number" && Number.isFinite(v) ? v : null;

/** Round for reading, not for maths: 5.666 -> "5.7", 120.0 -> "120". */
const n = (v: unknown, dp = 1): string => {
  const x = num(v);
  if (x === null) return "—";
  const r = Math.abs(x) >= 100 ? Math.round(x) : Number(x.toFixed(dp));
  return String(r);
};

/**
 * A figure the sentence cannot be written without.
 *
 * Throws when it is missing, which `verdictFor` catches and turns into the
 * neutral "see the figures below". That is deliberate: a headline reading
 * "peaking at 284 trucks against — sustained" is worse than no headline, and a
 * renamed backend key should degrade to silence rather than to broken copy. It
 * happened once — `sustained_rate` became `sustained_rate_per_hour` — and this
 * is what stops it reaching a screen.
 */
const req = (v: unknown, field: string): number => {
  const x = num(v);
  if (x === null) throw new Error(`verdict: missing required figure '${field}'`);
  return x;
};

/** "1 call" / "2 calls" — an operator reading "1 calls" stops trusting the screen. */
const plural = (count: unknown, one: string, many = `${one}s`): string => {
  const x = num(count) ?? 0;
  return `${n(x, 0)} ${x === 1 ? one : many}`;
};

const hours = (v: unknown): string => {
  const x = num(v);
  return x === null ? "—" : `${n(x)} ${x === 1 ? "hour" : "hours"}`;
};

/** Attributive form: "a 6-hour overrun", never "a 6 hours overrun". */
const hourAdj = (v: unknown): string => `${n(v)}-hour`;

/** Verb agreement for a counted subject: "1 hour runs", "2 hours run". */
const verb = (count: unknown, singular: string, plural_: string): string =>
  (num(count) ?? 0) === 1 ? singular : plural_;

/* ---------------------------------------------------------------- verdicts */

function vesselBunching(f: VerdictInput["figures"], r: VerdictInput["result"]): Verdict {
  const ordering = String(f.recommended_ordering ?? "");
  const gain = num(f.improvement_vs_baseline) ?? 0;
  // The catalogue description carries a parenthetical weighting note
  // ("(+0.5 per berth reassignment)") that belongs in the assumptions, not in a
  // sentence someone reads once.
  const objective = String(r?.objective?.description ?? "the stated objective")
    .replace(/\s*\([^)]*\)/g, "")
    .trim();
  const contending = f.vessels_contending;
  const busiest = f.busiest_terminal;
  const share = num(f.busiest_terminal_calls);

  const total = num(contending) ?? 0;
  // Only worth saying when one terminal is actually carrying a disproportionate
  // share — otherwise it is noise dressed up as insight.
  const detail = busiest && share && total > 0 && share / total >= 0.4
    ? `${busiest} is carrying ${n(share, 0)} of the ${n(total, 0)} contending calls — resequencing inside one terminal will not fix an uneven spread across terminals.`
    : undefined;

  if (!ordering || ordering === "FCFS" || gain <= 0) {
    return {
      headline: `Keep the current arrival order. With ${plural(contending, "vessel")} contending, no alternative sequence does better on ${objective}.`,
      detail,
      tone: "ok",
    };
  }
  return {
    headline: `Berth in ${ordering} order. Against arrival order it saves ${n(gain)} on ${objective}, across ${plural(contending, "vessel")}.`,
    detail,
    tone: "ok",
  };
}

function berthCascade(f: VerdictInput["figures"]): Verdict {
  const displaced = num(f.calls_displaced) ?? 0;
  const cumulative = f.cumulative_delay_hours;
  const delay = req(f.target_delay_hours, "target_delay_hours");
  if (displaced === 0) {
    return {
      headline: `The ${hourAdj(delay)} overrun is absorbed. No later call at this terminal is pushed back.`,
      tone: "ok",
    };
  }
  return {
    headline: `The ${hourAdj(delay)} overrun pushes back ${plural(displaced, "later call")}, costing ${hours(cumulative)} across the berth queue over the next 48 hours.`,
    detail: "Each displaced call inherits the delay in full — the queue does not recover it.",
    tone: displaced > 5 ? "critical" : "warning",
  };
}

function modalShift(f: VerdictInput["figures"], r: VerdictInput["result"]): Verdict {
  const absorbs = r?.gate_absorbs_load;
  const extra = req(f.additional_truck_trips, "additional_truck_trips");
  const c = r?.first_constraint;
  const constraint = c?.constraint ? String(c.constraint).replace(/_/g, " ").toLowerCase() : null;
  const hour = c?.hour ? new Date(c.hour).toISOString().slice(11, 16) : null;

  if (absorbs === true) {
    return {
      headline: `The gate absorbs it. Moving that share of rail to road adds ${plural(extra, "truck trip")} and no hour runs past what the gate sustains.`,
      tone: "ok",
    };
  }
  return {
    headline: `The gate does not absorb it. ${plural(extra, "extra truck trip")} push it past capacity${constraint ? `, and ${constraint} is the first thing to run short${hour ? ` at ${hour}` : ""}` : ""}.`,
    detail: `${plural(f.additional_saturated_hours, "additional hour")} ${verb(f.additional_saturated_hours, "runs", "run")} over the sustained rate.`,
    tone: "critical",
  };
}

function craneProductivity(f: VerdictInput["figures"], r: VerdictInput["result"]): Verdict {
  const vessel = r?.target_call?.vessel_name ?? "the call";
  const own = req(f.turnaround_increase_hours, "turnaround_increase_hours");
  const displaced = num(f.calls_displaced) ?? 0;
  const behind = f.cumulative_berth_delay_hours;
  return {
    headline: `Losing a quarter of the working rate on ${vessel} adds ${hours(own)} to its own turnaround${displaced > 0 ? `, and ${hours(behind)} across ${plural(displaced, "call")} behind it` : ", and nothing behind it moves"}.`,
    detail: `Berth productivity here is ${n(f.baseline_moves_per_hour)} moves per hour worked — that is the whole berth, not one crane.`,
    tone: displaced > 3 ? "critical" : "warning",
  };
}

function gateSlotting(f: VerdictInput["figures"], r: VerdictInput["result"]): Verdict {
  const shape = String(r?.arrival_pattern?.shape ?? "").toLowerCase();
  const peak = req(f.observed_peak, "observed_peak");
  const rate = req(f.sustained_rate_per_hour, "sustained_rate_per_hour");
  const saturated = num(f.saturated_hours) ?? 0;
  const cut = num(f.peak_reduction_pct) ?? 0;

  if (saturated === 0) {
    return {
      headline: `Arrivals stay within capacity all day. The busiest hour reaches ${n(peak, 0)} trucks against ${n(rate, 0)} the gate sustains.`,
      detail: shape === "peaked" ? "The day is peaked, so the headroom is uneven even though no hour overflows." : undefined,
      tone: "ok",
    };
  }
  return {
    headline: `Arrivals outrun the gate in ${plural(saturated, "hour")}, peaking at ${n(peak, 0)} trucks against ${n(rate, 0)} sustained. Booking arrivals into slots cuts the peak by ${n(cut, 0)}%.`,
    // Suppressed at zero: "0 arrivals cannot be placed" is a true sentence that
    // reads as a problem, and there isn't one.
    detail: (num(f.arrivals_not_placeable_in_window) ?? 0) > 0
      ? `${plural(f.arrivals_not_placeable_in_window, "arrival")} cannot be placed inside the day at all — those need a longer window or more capacity, not resequencing.`
      : "Every arrival still fits inside the day; the peak just moves.",
    tone: saturated > 4 ? "critical" : "warning",
  };
}

function driverShortage(f: VerdictInput["figures"], r: VerdictInput["result"]): Verdict {
  const loss = req(f.throughput_loss_pct, "throughput_loss_pct");
  const lost = f.trips_lost;
  const top = r?.transporter_exposure?.by_absolute_loss?.[0];
  const flow = r?.cargo_flow_exposure?.[0];
  return {
    headline: `Cutting each vehicle's daily trips by a third removes ${plural(lost, "trip")} and ${n(loss)}% of evacuation throughput.`,
    detail: [
      top?.company ? `${top.company} is the most exposed transporter` : null,
      flow?.flow ? `${String(flow.flow).replace(/_/g, " ").toLowerCase()} is the most exposed cargo flow` : null,
    ].filter(Boolean).join("; ") || undefined,
    tone: (num(loss) ?? 0) > 25 ? "critical" : "warning",
  };
}

function channelClosure(f: VerdictInput["figures"]): Verdict {
  const locked = f.berth_lock_reached === true || f.berth_lock_reached === "true";
  const toLock = f.hours_to_berth_lock;
  const held = f.vessels_held;
  if (!locked) {
    return {
      headline: `The port stays workable through the closure. ${plural(f.berths_free_at_reopen, "berth")} ${verb(f.berths_free_at_reopen, "is", "are")} still free when the channel reopens.`,
      detail: `${plural(held, "vessel")} ${verb(held, "finishes", "finish")} working and ${verb(held, "waits", "wait")} to sail.`,
      tone: "warning",
    };
  }
  return {
    headline: `The port is berth-locked ${hours(toLock)} into the closure — no berth can be freed by anything the port can do from the inside.`,
    detail: `${plural(held, "vessel")} ${verb(held, "is", "are")} held alongside. Every hour beyond that point costs the whole estate, not one berth.`,
    tone: "critical",
  };
}

function yardFeedback(f: VerdictInput["figures"]): Verdict {
  const regime = String(f.regime ?? "");
  const tipping = f.tipping_day;
  const util = req(f.final_utilisation_pct, "final_utilisation_pct");
  if (regime === "SATURATING") {
    return {
      headline: `The yard fills until it is full and stays there${tipping ? `, crossing the comfortable ceiling on day ${n(tipping, 0)}` : ""}. It does not settle on its own.`,
      detail: `${n(f.discharge_blocked_teu, 0)} TEU of discharge is blocked over the horizon — at that point the constraint moves to the quay and vessels wait.`,
      tone: "critical",
    };
  }
  return {
    headline: `The yard settles at ${n(util)}% full${tipping ? `, crossing the comfortable ceiling on day ${n(tipping, 0)}` : ""} — but it settles by slowing the berth.`,
    detail: "Self-limiting is not the same as safe: the cost shows up as vessel turnaround, not as a yard alarm.",
    tone: "warning",
  };
}

function degradedGate(f: VerdictInput["figures"]): Verdict {
  const peak = req(f.peak_queue_with_outage, "peak_queue_with_outage");
  const recovery = f.recovery_hours_after_restore;
  const outage = req(f.outage_hours, "outage_hours");
  return {
    headline: `A ${hourAdj(outage)} outage backs the queue up to ${n(peak, 0)} trucks${recovery === null || recovery === undefined ? ", and it has not cleared by the end of the window" : `, and takes a further ${hours(recovery)} to clear once systems return`}.`,
    detail: `Manual working runs at ${n(f.degraded_rate, 0)} trucks an hour against ${n(f.normal_sustained_rate, 0)} normally.`,
    tone: "critical",
  };
}

const BY_SCENARIO: Record<string, (f: VerdictInput["figures"], r: VerdictInput["result"]) => Verdict> = {
  "vessel-bunching": vesselBunching,
  "berth-cascade": (f) => berthCascade(f),
  "modal-shift": modalShift,
  "crane-productivity": craneProductivity,
  "gate-slotting": gateSlotting,
  "driver-shortage": driverShortage,
  "channel-closure": (f) => channelClosure(f),
  "yard-feedback": (f) => yardFeedback(f),
  "degraded-gate": (f) => degradedGate(f),
};

/**
 * The verdict for one result.
 *
 * An unanswerable scenario returns the backend's own reason rather than a generic
 * "no data": the backend distinguishes "the table was empty" from "the query
 * failed", and flattening those two would tell the reader something untrue.
 */
export function verdictFor(input: VerdictInput): Verdict {
  if (!input.data_available) {
    const failed = input.notes.find((x) => x.startsWith("QUERY FAILED"));
    return {
      headline: failed
        ? "This could not be answered — a query behind it failed."
        : "This cannot be answered from the data available.",
      detail: input.notes[0] ?? undefined,
      tone: "unavailable",
    };
  }
  const build = BY_SCENARIO[input.scenario];
  if (!build) {
    return {
      headline: "Answered. See the figures below.",
      tone: "ok",
    };
  }
  try {
    return build(input.figures ?? {}, input.result ?? {});
  } catch {
    // A verdict is a convenience; never let it take the whole panel down.
    return { headline: "Answered. See the figures below.", tone: "ok" };
  }
}

/**
 * Whether a figure came from measurement or from a stated assumption.
 *
 * MEASURED is the unmarked default — chipping every measured number turns the
 * screen into a wall of badges and stops anyone reading any of them. Only the two
 * that change how much a reader should trust a figure are surfaced.
 */
export function shouldChip(source: SimAssumptionSource): boolean {
  return source === "ASSUMED" || source === "PARAMETER";
}

/** Coverage banner text, when the answer rests on a projected day. */
export function coverageNotice(result: Record<string, any>): string | null {
  const c = result?.coverage;
  if (!c || c.basis !== "PROJECTED") return null;
  const asked = String(c.requested ?? "").slice(0, 10);
  const through = String(c.measured_through ?? "").slice(0, 10);
  return `${asked} is beyond the data, which ends ${through}. The state below is carried forward from the last measured day under a stated assumption — it is a projection, not a measurement.`;
}
