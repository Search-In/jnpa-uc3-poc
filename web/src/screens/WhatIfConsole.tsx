import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useScenario, SCENARIO_LABELS, type ScenarioId } from "@/hooks/ScenarioContext";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { FlaskConical } from "lucide-react";

// Scaffold only — Prompt 10 wires this to the scenario driver (/api/scenarios)
// to drive TFC-1/2/3 and replay what-if simulations. For now it lets an operator
// flip the header's active-scenario banner and lists any scenarios the backend
// already knows about, so the surface and routing exist for Prompt 10 to fill.
const SCENARIOS: { id: ScenarioId; blurb: string }[] = [
  { id: "TFC-1", blurb: "Close a gate and watch trucks re-route to the next-best terminal." },
  { id: "TFC-2", blurb: "Inject a congestion surge on the corridor and observe forecaster onset." },
  { id: "TFC-3", blurb: "Drop in-cab GPS for a cohort; SECONDARY/TERTIARY fallback + advisory re-routes." },
];

export default function WhatIfConsole() {
  const { scenario, setScenario } = useScenario();
  const known = useQuery({ queryKey: ["scenarios"], queryFn: api.scenarios });

  return (
    <div className="h-full overflow-y-auto p-4">
      <div className="mb-4 flex items-center gap-2">
        <FlaskConical className="h-5 w-5 text-primary" />
        <div>
          <h1 className="text-lg font-semibold">What-If Console</h1>
          <p className="text-sm text-muted-foreground">
            Scaffold · Prompt 10 wires these to the scenario driver. Selecting one updates the header banner.
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        {SCENARIOS.map((s) => (
          <Card key={s.id} className={scenario === s.id ? "border-primary" : ""}>
            <CardHeader className="flex-row items-center justify-between">
              <CardTitle>{SCENARIO_LABELS[s.id]}</CardTitle>
              {scenario === s.id && <Badge colour="#56B4E9">active</Badge>}
            </CardHeader>
            <CardContent className="space-y-3">
              <p className="text-xs text-muted-foreground">{s.blurb}</p>
              <Button
                size="sm"
                variant={scenario === s.id ? "default" : "outline"}
                onClick={() => setScenario(scenario === s.id ? "none" : s.id)}
              >
                {scenario === s.id ? "Stop" : "Arm scenario"}
              </Button>
            </CardContent>
          </Card>
        ))}
      </div>

      <Card className="mt-5">
        <CardHeader>
          <CardTitle>Scenarios known to the backend</CardTitle>
        </CardHeader>
        <CardContent>
          {known.isLoading ? (
            <p className="text-sm text-muted-foreground">loading…</p>
          ) : (known.data?.scenarios?.length ?? 0) === 0 ? (
            <p className="text-sm text-muted-foreground">
              None recorded yet. The scenario driver (Prompt 10) will populate <span className="font-mono">/api/scenarios</span>.
            </p>
          ) : (
            <ul className="space-y-1 text-sm">
              {known.data!.scenarios.map((sc) => (
                <li key={sc.id} className="flex justify-between border-b border-border/50 py-1">
                  <span>{sc.name}</span>
                  <span className="font-mono text-xs text-muted-foreground">{sc.id}</span>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
