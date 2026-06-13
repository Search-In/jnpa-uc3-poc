import { createContext, useContext, useState, type ReactNode } from "react";

// The header shows the active demo scenario (none / TFC-1 / TFC-2 / TFC-3) and a
// "Reset to baseline" button. Prompt 10 wires this to the scenario driver; here
// it is client-side state shared across screens (the What-If console will drive
// it). Persisted to sessionStorage so a refresh keeps the banner.

export type ScenarioId = "none" | "TFC-1" | "TFC-2" | "TFC-3";

export const SCENARIO_LABELS: Record<ScenarioId, string> = {
  none: "Baseline",
  "TFC-1": "TFC-1 · Gate closure",
  "TFC-2": "TFC-2 · Congestion surge",
  "TFC-3": "TFC-3 · GPS / re-route",
};

interface ScenarioCtx {
  scenario: ScenarioId;
  setScenario: (s: ScenarioId) => void;
  reset: () => void;
}

const Ctx = createContext<ScenarioCtx | null>(null);
const KEY = "jnpa.scenario";

export function ScenarioProvider({ children }: { children: ReactNode }) {
  const [scenario, setScenarioState] = useState<ScenarioId>(
    () => (sessionStorage.getItem(KEY) as ScenarioId) || "none"
  );
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
