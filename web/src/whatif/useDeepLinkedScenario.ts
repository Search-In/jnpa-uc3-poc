/**
 * `?scenario=<id>` — starting a scenario from a cross-twin hand-off.
 *
 * WHAT WAS MISSING. Reading the parameter into ScenarioContext was not enough: that id is
 * a LABEL (it drives the header banner and the reset button), and nothing about it starts
 * anything. A scenario actually running is three separate things, which only
 * WhatIfConsole's `trigger()` did together:
 *
 *   1. `setScenario(id)`                 the banner says what is running
 *   2. `runScenario(runner, params)`     the gateway actually performs it
 *   3. `tourStore.startScenario(...)`    the coach-mark narrates it, driving navigation
 *
 * So a deep link that only did (1) landed on the page and appeared to do nothing — which
 * is exactly what `/live?scenario=MONSOON-FRIDAY` did.
 *
 * The id is captured at MODULE LOAD (see ./pendingScenario), before React renders and
 * before the sign-in gate decides anything, so a login screen between the link and the
 * console cannot eat it.
 *
 * Fires once per visit. A failed run still starts the tour: the narration is honest about
 * being a scripted walkthrough, and a dead gateway should not leave the operator staring
 * at a page that silently ignored their link.
 */
import { useEffect, useRef } from "react";
import { getAdapter } from "@/data";
import { useScenario } from "@/hooks/ScenarioContext";
import { getScript } from "./scenarioScripts";
import { SCENARIOS } from "./scenarioRunners";
import { tourStore } from "./tourStore";
import { takePendingScenario } from "./pendingScenario";

export function useDeepLinkedScenario(): void {
  const { setScenario } = useScenario();
  // StrictMode double-invokes effects in dev; the ref keeps one link to one run.
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    const id = takePendingScenario();
    if (!id) return;

    const script = getScript(id);
    const entry = SCENARIOS.find((s) => s.id === id || s.runner === id);
    // Unknown id: ignore it rather than put the app in a state with no script behind it.
    if (!script || !entry) return;
    started.current = true;

    // Already mid-scenario — a link must not yank an operator out of a live run.
    if (tourStore.getState().scenarioId) return;

    setScenario(entry.id);
    void (async () => {
      let handleId: string | null = null;
      try {
        const res = await getAdapter().runScenario(entry.runner, entry.params);
        handleId = res.handle_id;
      } catch {
        // Gateway unreachable or the runner refused: narrate anyway, with no handle, so
        // the timeline panel simply stays empty instead of the link doing nothing at all.
      }
      tourStore.startScenario(entry.id, handleId);
    })();
  }, [setScenario]);
}
