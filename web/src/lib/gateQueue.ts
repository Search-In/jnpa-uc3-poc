// Gate-queue source posture — classifying what an empty Driver-Advisory queue
// actually MEANS.
//
// GET /api/trucks?state=AT_GATE_QUEUE already answers this question. Its
// fallback ladder is LIVE (truck-sim control plane) → CACHED (last good payload,
// aged) → [RDS telemetry tail / check-ins, both SKIPPED for a state-filtered
// query because only the sim knows a TruckState]. When the sim cannot be
// reached and the memo has expired, the gateway answers HTTP 200 with
//
//     {count: 0, devices: [], degraded: true, state_filter_supported: false,
//      decision_path: null, hint: "TruckState is only known to the truck-sim…"}
//
// — i.e. "I cannot answer this question", not "the queue is empty".
//
// The client threw that envelope away (`adapter.trucks()` returned only
// `.devices`), so an unreachable queue source rendered as the flat operational
// statement "No trucks currently in a gate queue" — a claim about the port that
// the gateway never made. A control room acting on that reads a broken data path
// as a quiet gate.
//
// This module keeps the envelope and classifies it. No new data source, no new
// queue model: it is the existing contract, read.

import type { TruckDevice } from "./types";

/** The gateway's fleet-list envelope for a state-filtered query. */
export interface TruckListEnvelope {
  devices: TruckDevice[];
  count: number;
  /** True when the answer did not come from the live control plane. */
  degraded?: boolean;
  /** Which rung answered: PRIMARY / CACHED / … null when none could. */
  decision_path?: string | null;
  /** Upstream that answered ("truck-sim", "memo", …). */
  source?: string | null;
  /** False when the rung that answered cannot honour a `state=` filter. */
  state_filter_supported?: boolean;
  /** Age of the memo when the CACHED rung answered. */
  cache_age_s?: number | null;
  /** Operator-facing hint the gateway attaches to an unanswerable query. */
  hint?: string | null;
  /**
   * Devices a real driver is currently signed in on (core.push_subscription).
   *
   * A SEPARATE list, never folded into `devices`: these are real but their
   * TruckState was not measured, so they carry no state, ETA, remaining
   * distance or gate. Keeping them out of `devices` is what preserves the
   * meaning of `count`, the per-gate depth cards and the empty/unavailable
   * classification below — all of which describe the AT_GATE_QUEUE measurement
   * and nothing else.
   */
  registered_devices?: TruckDevice[];
  registered_count?: number;
}

export type QueueStatus =
  /** The request is in flight and nothing has been rendered yet. */
  | "loading"
  /** The request itself failed (transport, timeout, 5xx). */
  | "error"
  /** The queue source could not be consulted — the count is NOT a measurement. */
  | "unavailable"
  /** Answered from the memo, not the live sim: real rows, but aged. */
  | "degraded"
  /**
   * This refresh failed (or could not be answered), but an EARLIER one
   * succeeded: the last known queue is still on screen, marked as not current.
   * Blanking a good table because one poll missed is worse than saying so.
   */
  | "stale"
  /** Live answer, genuinely nobody queueing. */
  | "empty"
  /** Live answer with trucks in it. */
  | "ok";

export interface QueueState {
  status: QueueStatus;
  /** Rows to render. Always [] unless the source actually answered. */
  devices: TruckDevice[];
  /**
   * The queue depth to display, or null when no measurement exists. A KPI card
   * MUST render null as "—": printing 0 for an unreachable source states a fact
   * about the port that nothing established.
   */
  count: number | null;
  /** Whether the figures came from somewhere other than the live control plane. */
  degraded: boolean;
  /** Short operator-facing explanation for a non-live posture, else null. */
  detail: string | null;
}

/** Human wording for the CACHED rung's age. */
function cachedDetail(env: TruckListEnvelope): string {
  const age = env.cache_age_s;
  const when =
    typeof age === "number" && Number.isFinite(age)
      ? ` (last update ${age < 90 ? `${Math.round(age)} s` : `${Math.round(age / 60)} min`} ago)`
      : "";
  return `Showing the last known gate queue${when} — the live queue feed is not responding.`;
}

const UNAVAILABLE_DETAIL =
  "The live gate-queue feed is not responding, so the current queue cannot be read. " +
  "This is a data-source outage, not an empty queue.";

/**
 * Classify one fleet-list result. `envelope` is undefined while loading or after
 * a failure; `isError` distinguishes the two.
 */
export function deriveQueueState(input: {
  isLoading: boolean;
  isError: boolean;
  envelope?: TruckListEnvelope | null;
}): QueueState {
  const { isLoading, isError, envelope } = input;

  if (isError) {
    return { status: "error", devices: [], count: null, degraded: true, detail: null };
  }
  if (isLoading || !envelope) {
    return { status: "loading", devices: [], count: null, degraded: false, detail: null };
  }

  const devices = envelope.devices ?? [];

  // The rung that answered cannot apply a state filter at all: whatever it
  // returned is not the gate queue, so the queue is UNKNOWN — never rendered as
  // zero. (The gateway sends devices: [] here; the guard is belt-and-braces.)
  if (envelope.state_filter_supported === false) {
    return {
      status: "unavailable",
      devices: [],
      count: null,
      degraded: true,
      detail: envelope.hint || UNAVAILABLE_DETAIL,
    };
  }

  // Answered from the memo. The rows are real and were measured — just not now.
  if (envelope.degraded) {
    return {
      status: "degraded",
      devices,
      count: devices.length,
      degraded: true,
      detail: cachedDetail(envelope),
    };
  }

  return {
    status: devices.length === 0 ? "empty" : "ok",
    devices,
    count: devices.length,
    degraded: false,
    detail: null,
  };
}

/** Per-gate depth of a queue result. Empty when the queue is unknown. */
export function queueDepthByGate(devices: TruckDevice[]): Map<string, number> {
  const depth = new Map<string, number>();
  for (const truck of devices) {
    if (truck.gate_id) depth.set(truck.gate_id, (depth.get(truck.gate_id) ?? 0) + 1);
  }
  return depth;
}

/**
 * Gate depth for a stat card: a number when the queue was measured, null when
 * it was not (rendered as "—", never 0).
 */
export function gateDepth(state: QueueState, gateId: string): number | null {
  if (state.count === null) return null;
  return queueDepthByGate(state.devices).get(gateId) ?? 0;
}

// ---------------------------------------------------------------- last known good
//
// THE INTERMITTENCY. The Driver-Advisory queue polls, and any single poll can
// miss: the truck-sim is one asyncio process simulating a large fleet, so
// /devices/list occasionally answers slower than the gateway's 4 s list budget,
// and the browser request itself can time out or land mid-deploy. Rendering that
// miss as "unavailable" (or as an error) THREW AWAY a queue that was correct
// seconds earlier — which is exactly the reported symptom: the same page showing
// 3 trucks, then "Gate-queue feed unavailable", then 3 trucks again.
//
// React Query keeps `data` from the last success when a background refetch
// fails, but the classifier read `isError` first and discarded it. So the rule
// is now explicit: an ANSWERED result always wins and becomes the new baseline;
// an UNANSWERED result never erases the baseline, it only marks it stale.
//
// Two invariants make this safe rather than sticky:
//   * only an answered result updates the baseline, so a failure can never
//     promote itself into "known good";
//   * updates are MONOTONIC in the answer's timestamp, so a slow response that
//     lands after a newer one cannot overwrite it.

/** The most recent result that the queue source actually answered. */
export interface LastKnownGoodQueue {
  state: QueueState;
  /** When that answer was produced (React Query's `dataUpdatedAt`). */
  at: number;
}

/** True for results the source actually answered (as opposed to missed). */
export function isAnswered(state: QueueState): boolean {
  return state.status === "ok" || state.status === "empty" || state.status === "degraded";
}

/**
 * Fold one result into the baseline. Answered results replace it, but ONLY when
 * they are newer than what is held — an out-of-order response cannot win.
 */
export function recordAnswer(
  previous: LastKnownGoodQueue | null,
  state: QueueState,
  at: number,
): LastKnownGoodQueue | null {
  if (!isAnswered(state)) return previous;
  if (!Number.isFinite(at) || at <= 0) return previous;
  if (previous && at <= previous.at) return previous; // stale arrival — ignored
  return { state, at };
}

function ageText(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 90) return `${s} s ago`;
  return `${Math.round(s / 60)} min ago`;
}

/**
 * What to render: the current result when the source answered, otherwise the
 * last known queue marked stale. "Gate-queue feed unavailable" is reserved for
 * the case where there is nothing good to fall back to.
 */
export function withLastKnownGood(
  current: QueueState,
  lastGood: LastKnownGoodQueue | null,
  now: number,
): QueueState {
  if (isAnswered(current)) return current;
  // A first load with nothing behind it: loading stays loading, and a genuine
  // failure is reported as a failure.
  if (!lastGood) return current;
  if (current.status === "loading") {
    // Refresh in progress — keep the table, no spinner over good data.
    return lastGood.state;
  }
  const why =
    current.status === "error"
      ? "The last refresh failed"
      : "The live gate-queue feed did not answer the last refresh";
  return {
    ...lastGood.state,
    status: "stale",
    degraded: true,
    detail: `${why}. Showing the queue as of ${ageText(now - lastGood.at)}; it may be out of date.`,
  };
}
