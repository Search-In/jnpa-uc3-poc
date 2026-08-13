// TFC-2 · Congestion surge — the full run → run lifecycle.
//
// Regression cover for the reported defect: after running TFC-2 once, the
// Active Alerts drawer opened by itself and the SECOND run hung.
//
// TWO independent causes, both asserted here:
//
//   1. THE DRAWER. HeaderActions force-opened the alerts sheet whenever the
//      guided tour's current step targeted an "alert-*" coach-mark, with
//      outside-click and Escape disabled. TFC-2's LAST script step is such a
//      step, and a tour STOPS on its last step without clearing itself — so the
//      modal sheet stayed open indefinitely and swallowed every click behind it,
//      including the Run button. The drawer is now user-owned state; the second
//      test below pins that the script still ends on an alert step, so the
//      pairing that caused the lock-up cannot silently come back.
//
//   2. THE RUN STATE. The console mutated `activeRunner`/`activeHandle` in
//      sequence around an awaited mutation, so run #1's handle stayed attached
//      while run #2 started, and a failed run threw out of the click handler
//      leaving the console pointing at a runner with no handle and no error.
//
// The state machine is pure, so the whole sequence — select, run, complete, run
// again, complete — is driven here without a DOM.

import { describe, expect, it } from "vitest";

import { getScript } from "./scenarioScripts";
import {
  IDLE_RUN,
  isBusy,
  previewRun,
  resetRun,
  runFailed,
  runStarted,
  startRun,
} from "./runState";

const TFC2 = { id: "TFC-2", runner: "tfc2" };

describe("TFC-2 run lifecycle", () => {
  it("completes run #1, then completes run #2 from a clean state", () => {
    // ---- run #1 -----------------------------------------------------------
    let s = startRun(IDLE_RUN, TFC2);
    expect(s.status).toBe("starting");
    expect(isBusy(s)).toBe(true);

    s = runStarted(s, "tfc2-handle-1");
    expect(s.status).toBe("running");
    expect(s.handleId).toBe("tfc2-handle-1");
    // Run #1 has settled: the console is not stuck "starting".
    expect(isBusy(s)).toBe(false);
    expect(s.error).toBeNull();

    // ---- run #2 -----------------------------------------------------------
    s = startRun(s, TFC2);
    // THE REGRESSION: run #1's handle is gone the instant run #2 begins, so the
    // board cannot show run #1's timeline, steps or alerts under run #2.
    expect(s.handleId).toBeNull();
    expect(s.status).toBe("starting");

    s = runStarted(s, "tfc2-handle-2");
    expect(s.status).toBe("running");
    expect(s.handleId).toBe("tfc2-handle-2");
    expect(s.handleId).not.toBe("tfc2-handle-1");
    // Run #2 finished. Nothing is left in flight, so the Run button re-enables.
    expect(isBusy(s)).toBe(false);
  });

  it("settles a failed run instead of leaving the console loading forever", () => {
    let s = runStarted(startRun(IDLE_RUN, TFC2), "tfc2-handle-1");
    s = startRun(s, TFC2);
    s = runFailed(s, "502 Bad Gateway — scenarios_runner_unreachable");

    expect(s.status).toBe("failed");
    expect(isBusy(s)).toBe(false); // loading resolves on failure too
    expect(s.error).toContain("502");
    // A failed run has no timeline — and must not inherit run #1's.
    expect(s.handleId).toBeNull();
    // The scenario stays named so the operator sees WHAT failed and can retry.
    expect(s.scenarioId).toBe("TFC-2");
  });

  it("recovers: a run after a failed run still completes", () => {
    let s = runFailed(startRun(IDLE_RUN, TFC2), "boom");
    s = startRun(s, TFC2);
    expect(s.error).toBeNull(); // the previous failure is cleared, not sticky
    s = runStarted(s, "tfc2-handle-3");
    expect(s.status).toBe("running");
    expect(isBusy(s)).toBe(false);
  });

  it("never carries a handle across scenarios", () => {
    let s = runStarted(startRun(IDLE_RUN, TFC2), "tfc2-handle-1");
    s = startRun(s, { id: "TFC-3", runner: "tfc3" });
    expect(s.scenarioId).toBe("TFC-3");
    expect(s.runner).toBe("tfc3");
    expect(s.handleId).toBeNull();
  });

  it("resets to a clean idle state", () => {
    const s = resetRun();
    expect(s).toEqual(IDLE_RUN);
    expect(s.handleId).toBeNull();
    expect(s.status).toBe("idle");
  });

  it("previewing a recorded run adopts only that handle", () => {
    const s = previewRun("tfc2", "demo-tfc2-001");
    expect(s.handleId).toBe("demo-tfc2-001");
    expect(s.scenarioId).toBeNull(); // a preview is not a live scenario run
    expect(s.error).toBeNull();
  });
});

describe("TFC-2 guided script", () => {
  it("still ends on an alert step — the pairing that used to jam the console", () => {
    // If this ever stops being true the lock-up story changes; the assertion is
    // here so the drawer fix is not quietly assumed to be unnecessary.
    const script = getScript("TFC-2");
    expect(script).toBeTruthy();
    const last = script!.steps[script!.steps.length - 1];
    expect(last.target.kind).toBe("dom");
    expect(last.target.selector).toBe("alert-WRONG_WAY");
  });

  it("has a runner that matches the console's scenario id", () => {
    expect(getScript("TFC-2")!.runner).toBe(TFC2.runner);
  });
});
