/**
 * tourStore — a tiny framework-agnostic pub/sub store that drives the guided
 * coach-mark tour on the What-If Console. It mirrors the reference project's
 * simStore tour state machine (start / goto / next / prev / stop / auto-advance)
 * but carries *only* tour state — there is no client-side sim-override or tick
 * engine here, because the target console is server-driven: the real tfc1/2/3
 * steps stream over /api/ws and the timeline is the live board.
 *
 * Auto-advance has two sources, so guidance always keeps moving:
 *   1. syncLive(n) — called by the console as real scenario_step frames arrive,
 *      so the coach-mark tracks the *actual* backend step (the primary driver),
 *   2. a TOUR_STEP_MS fallback timer — so narration still advances in mock /
 *      no-socket mode where no live steps arrive.
 * Both only move the tour forward and never past the script's last step.
 *
 * Deterministic: no Date.now / Math.random (a monotonic `stamp` drives the
 * progress bar). Reversible: stop() returns to a clean baseline.
 */
import { getScript } from "./scenarioScripts";

/** How long each step stays on screen before the fallback timer advances it. */
export const TOUR_STEP_MS = 6000;

export interface TourState {
  /** Active scenario id (e.g. "TFC-1"), or null when no tour is playing. */
  scenarioId: string | null;
  /**
   * The gateway run handle this tour follows. The app-level GuidedTour reads this
   * handle's live scenario_step frames out of SocketContext, so the tour keeps
   * running and switching the visible view even after WhatIfConsole unmounts on a
   * view change. Also lets WhatIfConsole restore its timeline when returned to
   * mid-tour.
   */
  handleId: string | null;
  /** Index of the current step in the scenario's storyline. */
  stepIndex: number;
  /** Total steps in the active script (0 when idle). */
  totalSteps: number;
  /** Auto-advance through steps (pause to read a step). */
  autoAdvance: boolean;
  /** Bumped on every step change so the UI can restart its progress bar. */
  stepStartedAt: number;
}

function baseState(): TourState {
  return {
    scenarioId: null,
    handleId: null,
    stepIndex: 0,
    totalSteps: 0,
    autoAdvance: true,
    stepStartedAt: 0,
  };
}

type Listener = () => void;

class TourStore {
  private state: TourState = baseState();
  private listeners = new Set<Listener>();
  private timer: ReturnType<typeof setTimeout> | null = null;
  /** Monotonic stamp source for step progress (avoids Date.now in the store). */
  private stamp = 0;

  getState = (): TourState => this.state;

  subscribe = (fn: Listener): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  private set = (next: TourState): void => {
    this.state = next;
    this.listeners.forEach((l) => l());
  };

  /**
   * Start a tour for a scenario: reset to step 0 and (if autoAdvance) arm the
   * fallback timer. The coach-mark overlay reads this to narrate; the console
   * also calls runScenario() against the gateway in parallel.
   */
  startScenario = (
    scenarioId: string,
    handleId: string | null = null,
    autoAdvance = true,
  ): void => {
    const script = getScript(scenarioId);
    if (!script || script.steps.length === 0) return;
    this.set({
      scenarioId,
      handleId,
      stepIndex: 0,
      totalSteps: script.steps.length,
      autoAdvance,
      stepStartedAt: ++this.stamp,
    });
    this.armTimer();
  };

  /** Jump to a specific step (prev/next buttons + progress dots). */
  gotoStep = (index: number): void => {
    const { scenarioId, totalSteps } = this.state;
    if (!scenarioId) return;
    const i = Math.max(0, Math.min(totalSteps - 1, index));
    if (i === this.state.stepIndex) return;
    this.set({ ...this.state, stepIndex: i, stepStartedAt: ++this.stamp });
    this.armTimer();
  };

  nextStep = (): void => this.gotoStep(this.state.stepIndex + 1);
  prevStep = (): void => this.gotoStep(this.state.stepIndex - 1);

  /** Toggle auto-advance without changing the current step. */
  setAutoAdvance = (autoAdvance: boolean): void => {
    if (!this.state.scenarioId) return;
    this.set({ ...this.state, autoAdvance, stepStartedAt: ++this.stamp });
    this.armTimer();
  };

  /**
   * Follow the live backend: `liveCount` is the number of real scenario_step
   * frames received so far. When auto-advancing, move the coach-mark to narrate
   * the latest real step (forward-only). Paused tours stay put.
   */
  syncLive = (liveCount: number): void => {
    const { scenarioId, autoAdvance, stepIndex, totalSteps } = this.state;
    if (!scenarioId || !autoAdvance || liveCount <= 0) return;
    const target = Math.min(totalSteps - 1, liveCount - 1);
    if (target > stepIndex) {
      this.set({ ...this.state, stepIndex: target, stepStartedAt: ++this.stamp });
      this.armTimer();
    }
  };

  /** End the tour and clear all tour state back to baseline. */
  stopScenario = (): void => {
    this.clearTimer();
    this.set(baseState());
  };

  private armTimer(): void {
    this.clearTimer();
    const { scenarioId, autoAdvance, stepIndex, totalSteps } = this.state;
    if (!scenarioId || !autoAdvance) return;
    if (stepIndex >= totalSteps - 1) return; // last step: stop
    this.timer = setTimeout(() => this.nextStep(), TOUR_STEP_MS);
  }

  private clearTimer(): void {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }
}

/** Singleton store shared by every component in the tab. */
export const tourStore = new TourStore();
