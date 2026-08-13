import { createContext, useContext, useState, type ReactNode } from "react";

// The header shows the active demo scenario (none / TFC-1 / TFC-2 / TFC-3) and a
// "Reset to baseline" button. Prompt 10 wires this to the scenario driver; here
// it is client-side state shared across screens (the What-If console will drive
// it). Persisted to sessionStorage so a refresh keeps the banner.

export type ScenarioId = "none" | "TFC-1" | "TFC-2" | "TFC-3" | "MONSOON-FRIDAY";

export const SCENARIO_LABELS: Record<ScenarioId, string> = {
  none: "Baseline",
  "TFC-1": "TFC-1 · Gate closure",
  "TFC-2": "TFC-2 · Congestion surge",
  "TFC-3": "TFC-3 · GPS / re-route",
  "MONSOON-FRIDAY": "Monsoon Friday · master",
};

interface ScenarioCtx {
  scenario: ScenarioId;
  setScenario: (s: ScenarioId) => void;
  reset: () => void;
}

const Ctx = createContext<ScenarioCtx | null>(null);
const KEY = "jnpa.scenario";

/** Ids a deep link may name. Anything else is ignored rather than trusted. */
const DEEP_LINKABLE: ScenarioId[] = ["TFC-1", "TFC-2", "TFC-3", "MONSOON-FRIDAY"];

/**
 * `?scenario=<id>` — the cross-twin entry point.
 *
 * UC-1 and UC-2 already accepted this parameter; UC-3 did not, so the cross-domain
 * Monsoon chain (UC-1 pilotage hold -> UC-2 late discharge -> UC-3 corridor surge) had no
 * way to land here. The hand-off button at the end of UC-2's scenario opens this app at
 * the scenario that continues the story.
 *
 * VALIDATED against the known ids: a URL is operator-supplied input, and setting an
 * unknown scenario would put the app in a state with no script and no runner behind it.
 * An unrecognised value is dropped and the app opens on its stored/baseline scenario.
 *
 * Read ONCE, at first render, and only when nothing is already running — reopening a link
 * must not yank an operator out of a scenario they are mid-way through.
 */
function scenarioFromUrl(): ScenarioId | null {
  try {
    const raw = new URLSearchParams(window.location.search).get("scenario");
    if (!raw) return null;
    const match = DEEP_LINKABLE.find((id) => id.toLowerCase() === raw.trim().toLowerCase());
    return match ?? null;
  } catch {
    return null;
  }
}

export function ScenarioProvider({ children }: { children: ReactNode }) {
  const [scenario, setScenarioState] = useState<ScenarioId>(() => {
    const stored = (sessionStorage.getItem(KEY) as ScenarioId) || "none";
    // The link wins only over a baseline session, never over a run in progress.
    if (stored !== "none") return stored;
    const linked = scenarioFromUrl();
    if (linked) sessionStorage.setItem(KEY, linked);
    return linked ?? stored;
  });
  const setScenario = (s: ScenarioId) => {
    sessionStorage.setItem(KEY, s);
    setScenarioState(s);
  };
  const reset = () => setScenario("none");
  return <Ctx.Provider value={{ scenario, setScenario, reset }}>{children}</Ctx.Provider>;
}

export function useScenario(): ScenarioCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useScenario must be used within ScenarioProvider");
  return v;
}
