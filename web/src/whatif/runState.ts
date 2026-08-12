/**
 * runState — the What-If Console's scenario-run lifecycle, as a small pure
 * state machine.
 *
 * WHY THIS EXISTS. The console used to keep the run in three loose pieces of
 * component state (`activeRunner`, `activeHandle`, the mutation's own pending
 * flag) mutated in sequence inside an async click handler. Two defects fell out
 * of that, both visible on a second TFC-2 run:
 *
 *   * RUN #1 STATE LEAKED INTO RUN #2. `activeHandle` was only overwritten AFTER
 *     the new run's POST resolved, so between the click and the response the
 *     board still showed run #1's handle, its timeline query and its steps — and
 *     if the new run FAILED it kept showing them indefinitely, as though the old
 *     result belonged to the new run.
 *   * A FAILED RUN NEVER SETTLED. The click handler awaited `mutateAsync` with no
 *     catch, so a rejected run threw out of an async event handler: the console
 *     was left pointing at a runner with no handle, with nothing rendered to say
 *     the run had failed.
 *
 * Modelling the run as ONE value with explicit transitions makes both states
 * unrepresentable: `start()` clears the previous handle and error in the same
 * transition that names the new scenario, and every terminal path (`started`,
 * `failed`) is a total function that settles `status`. There is no transition
 * that leaves a stale handle attached to a new scenario.
 *
 * Pure and framework-free on purpose — the console holds it in a `useState`, and
 * the regression test drives a full TFC-2 run→run sequence without a DOM.
 */

/** Where a run is in its lifecycle. `starting` = the POST is in flight. */
export type RunStatus = "idle" | "starting" | "running" | "failed";

export interface WhatIfRunState {
  /** Display id of the selected scenario (e.g. "TFC-2"), null when idle. */
  scenarioId: string | null;
  /** Backend runner name (e.g. "tfc2") — what /api/scenarios/{name}/run takes. */
  runner: string | null;
  /** Handle minted by the runner for THIS run. Never carried across runs. */
  handleId: string | null;
  status: RunStatus;
  /** Operator-facing failure text for the most recent run, else null. */
  error: string | null;
}

export const IDLE_RUN: WhatIfRunState = {
  scenarioId: null,
  runner: null,
  handleId: null,
  status: "idle",
  error: null,
};

/** A run that is in flight and must not be interrupted by another submit. */
export function isBusy(state: WhatIfRunState): boolean {
  return state.status === "starting";
}

/**
 * Begin a run. Clears the previous handle and error IN THE SAME TRANSITION, so
 * nothing from the previous run can be read while this one is starting.
 */
export function startRun(
  _prev: WhatIfRunState,
  scenario: { id: string; runner: string },
): WhatIfRunState {
  return {
    scenarioId: scenario.id,
    runner: scenario.runner,
    handleId: null,
    status: "starting",
    error: null,
  };
}

/** The runner accepted the run and minted `handleId`. */
export function runStarted(prev: WhatIfRunState, handleId: string): WhatIfRunState {
  return { ...prev, handleId, status: "running", error: null };
}

/**
 * The run could not be started (transport error, 502 from the gateway proxy, a
 * timeout). The scenario stays named so the operator can see what failed and
 * retry it, but there is no handle — a failed run has no timeline to show.
 */
export function runFailed(prev: WhatIfRunState, message: string): WhatIfRunState {
  return { ...prev, handleId: null, status: "failed", error: message };
}

/** Preview a recorded/demo handle: adopts it read-only, never a live run. */
export function previewRun(name: string, handleId: string): WhatIfRunState {
  return { scenarioId: null, runner: name, handleId, status: "running", error: null };
}

/** Reset to baseline — back to a clean idle state. */
export function resetRun(): WhatIfRunState {
  return { ...IDLE_RUN };
}
