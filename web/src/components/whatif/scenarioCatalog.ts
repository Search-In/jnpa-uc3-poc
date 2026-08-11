/**
 * The nine what-if scenarios, with the parameters the JNPA Notice states.
 *
 * One list, shared by all three dashboards, so a scenario cannot appear in one
 * app under a different name, a different reference or a different default from
 * another. The defaults are the Notice's own dates and percentages, quoted in
 * the comment beside each, so the briefed question runs without anyone typing.
 *
 * `owner` vs `answeredBy` is the fact worth surfacing: I-A, I-B, II-A and II-B
 * are UC-1 and UC-2 questions that are COMPUTED in UC-3, because the berthing,
 * traffic and gate tables all live there. An evaluator hunting for "the UC-2
 * answer to II-B" inside the UC-2 app needs to be told that, not left to infer
 * it from an absence.
 *
 * SHARED FILE — canonical copy in jnpa-uc3-poc. UC-1 (poc_1) and UC-2 (PoC_2)
 * hold copies. Change it here first.
 */

export type UcId = "UC-1" | "UC-2" | "UC-3";

export interface ScenarioEntry {
  /** Path segment of POST /api/cargo/simulate/{id}. */
  id: string;
  /** JNPA's reference (I-A … III-B), or N-1..N-3 for the ones we proposed. */
  ref: string;
  label: string;
  /** The question, in the words the Notice or the briefing uses. */
  question: string;
  /** Which use case the question belongs to. */
  owner: UcId;
  /** Where it is actually computed — always UC-3 today; that is the point. */
  answeredBy: UcId;
  source: "notice" | "bidder";
  /** Request body. The Notice's own dates and percentages. */
  params: Record<string, unknown>;
  /** Stated up front when an evaluator should know it before reading figures. */
  caveat?: string;
}

export const SCENARIO_CATALOG: ScenarioEntry[] = [
  {
    id: "vessel-bunching",
    ref: "I-A",
    label: "Vessel bunching",
    question:
      "A large number of vessels are alongside, unevenly distributed between terminals. What berthing order should be run, against which stated objective, and what would an alternative order cost against that same objective?",
    owner: "UC-1", answeredBy: "UC-3", source: "notice",
    // "On 6 August 2026 a large number of vessels are alongside…"
    params: { as_of: "2026-08-06T00:00:00Z", objective: "waiting_time", horizon_hours: 24 },
    caveat:
      "6 August is beyond the measured data. The state is carried forward from the last measured day and the answer says so.",
  },
  {
    id: "berth-cascade",
    ref: "I-B",
    label: "Extended berth window",
    question:
      "A vessel's operation overruns by six hours. Which subsequent calls at that terminal are displaced, by how long, and what is the cumulative delay across the berth queue over the following 48 hours?",
    owner: "UC-1", answeredBy: "UC-3", source: "notice",
    // "On 2nd August 2026, a vessel's operation is overrun by six hours."
    params: { as_of: "2026-08-02T00:00:00Z", delay_hours: 6, horizon_hours: 48 },
  },
  {
    id: "modal-shift",
    ref: "II-A",
    label: "Rail to road modal shift",
    question:
      "If 20% of rail-evacuated volume moves to road, does the gate absorb it? Hourly gate profile before and after, and the first constraint to saturate.",
    owner: "UC-2", answeredBy: "UC-3", source: "notice",
    // "Twenty per cent … for period 1st August 2026 to 3rd August 2026."
    params: { from_date: "2026-08-01", to_date: "2026-08-03", shift_pct: 0.2 },
  },
  {
    id: "crane-productivity",
    ref: "II-B",
    label: "Equipment availability",
    question:
      "What is the effective productivity per vessel call in gross moves per hour worked, and what does a 25% reduction on one call cost in turnaround and berth-queue delay?",
    owner: "UC-2", answeredBy: "UC-3", source: "notice",
    // "Model a twenty-five per cent reduction … Take up a vessel on 6th August."
    params: { as_of: "2026-08-06T00:00:00Z", reduction_pct: 0.25, window_hours: 48 },
    caveat:
      "The rate is per BERTH — all cranes on the vessel — because the feed carries no crane count. On 6 August the move counts are placeholders; 1, 2 and 4 August are measured.",
  },
  {
    id: "gate-slotting",
    ref: "III-A",
    label: "Gate approach congestion",
    question:
      "Characterise the arrival pattern, identify the periods where arrivals exceed the rate the gate sustains, and propose a slotting arrangement that flattens the peak.",
    owner: "UC-3", answeredBy: "UC-3", source: "notice",
    params: { from_ts: "2026-08-03T00:00:00Z", to_ts: "2026-08-04T00:00:00Z" },
  },
  {
    id: "driver-shortage",
    ref: "III-B",
    label: "Driver shortage",
    question:
      "If each vehicle completes a third fewer trips per day, what happens to evacuation throughput, which transporters and cargo flows are most exposed, and what is the state on the report date?",
    owner: "UC-3", answeredBy: "UC-3", source: "notice",
    // "1st to 3rd August … show state on 4th August 2026."
    params: {
      from_date: "2026-08-01", to_date: "2026-08-03",
      state_date: "2026-08-04", reduction_pct: 0.3333,
    },
    caveat:
      "Throughput is measured from gate telemetry. Transporter attribution is not available — no plate-to-transporter register exists in the data — and the answer reports that rather than ranking a single bucket.",
  },
  {
    id: "channel-closure",
    ref: "N-1",
    label: "Channel closure",
    question:
      "The approach channel is lost for N hours, so arrivals and sailings stop together. At what hour is the port berth-locked, and in what order should held vessels sail on reopening?",
    owner: "UC-1", answeredBy: "UC-3", source: "bidder",
    params: { as_of: "2026-08-06T06:00:00Z", closure_hours: 12, transit_hours: 1.5 },
    caveat:
      "Not requested by JNPA. The only scenario in which one shared asset throttles both directions at once.",
  },
  {
    id: "yard-feedback",
    ref: "N-2",
    label: "Yard saturation feedback",
    question:
      "Evacuation drops while discharge continues. Above a utilisation threshold, re-handles degrade berth productivity, which feeds back into the yard. Where does it settle and when does it tip?",
    owner: "UC-2", answeredBy: "UC-3", source: "bidder",
    params: { from_date: "2026-08-01", to_date: "2026-08-05", evacuation_drop_pct: 0.5 },
    caveat:
      "Not requested by JNPA. The only closed loop in the set. The occupancy-to-productivity curve is a declared assumption.",
  },
  {
    id: "degraded-gate",
    ref: "N-3",
    label: "Degraded-mode gate outage",
    question:
      "Gate automation is unavailable for N hours and the gate reverts to manual. How far does the queue back up, and how long does it take to clear once systems return?",
    owner: "UC-3", answeredBy: "UC-3", source: "bidder",
    params: {
      from_ts: "2026-08-03T00:00:00Z", to_ts: "2026-08-04T00:00:00Z",
      outage_hours: 4, degraded_fraction: 0.4,
    },
    caveat:
      "Not requested by JNPA. Every requested scenario is a physical disruption; this is the only digital one, and the only one that measures recovery.",
  },
];

export const NOTICE_SCENARIOS = SCENARIO_CATALOG.filter((s) => s.source === "notice");
export const PROPOSED_SCENARIOS = SCENARIO_CATALOG.filter((s) => s.source === "bidder");

/** The scenarios a given use case OWNS — shown first in that app. */
export function ownedBy(uc: UcId): ScenarioEntry[] {
  return SCENARIO_CATALOG.filter((s) => s.owner === uc);
}

/** A list the caller can index into without a null check. */
export type NonEmpty<T> = [T, ...T[]];

/**
 * All nine, ordered for a given dashboard: the ones this use case owns first,
 * then the rest. Every app can run every scenario — a cross-domain twin that
 * hides a question because another department asked it is not a twin — but the
 * ones you are accountable for come first.
 *
 * Returns a non-empty tuple so callers can take `[0]` as the default selection
 * without a null check. UC-2's tsconfig enables `noUncheckedIndexedAccess` and
 * this file is copied verbatim into all three apps, so it has to satisfy the
 * strictest of them.
 */
export function orderedFor(uc: UcId): NonEmpty<ScenarioEntry> {
  const ordered = [...ownedBy(uc), ...SCENARIO_CATALOG.filter((s) => s.owner !== uc)];
  const [first, ...rest] = ordered;
  if (!first) throw new Error("scenario catalog is empty");
  return [first, ...rest];
}

export function byId(id: string): ScenarioEntry | undefined {
  return SCENARIO_CATALOG.find((s) => s.id === id);
}
