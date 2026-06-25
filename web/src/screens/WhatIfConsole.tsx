import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { getAdapter } from "@/data";
import { useScenario, SCENARIO_LABELS, type ScenarioId } from "@/hooks/ScenarioContext";
import { useSocket } from "@/hooks/SocketContext";
import type { ScenarioStep } from "@/lib/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/misc";
import { fmtTimeIST } from "@/lib/utils";
import { FlaskConical, Play, RotateCcw, ArrowRight, ExternalLink, Sparkles } from "lucide-react";
import { tourStore } from "@/whatif/tourStore";
import { getScript } from "@/whatif/scenarioScripts";

// What-If Console (Sub-Criterion 5). Trigger TFC-1/2/3, watch the step-by-step
// storyline paint live from /api/ws (type=scenario_step), and reset to baseline.
const SCENARIOS: {
  id: ScenarioId;
  runner: string; // runner name (tfc1/tfc2/tfc3)
  blurb: string;
  params: Record<string, any>;
}[] = [
  {
    id: "TFC-1",
    runner: "tfc1",
    blurb:
      "Close G-NSICT; forecaster predicts spillover; trucks auto-re-route; TAS slots rescheduled.",
    params: { gate_id: "G-NSICT", duration_minutes: 120 },
  },
  {
    id: "TFC-2",
    runner: "tfc2",
    blurb:
      "Inject a wrong-way track at Karal Phata; anomaly fires; e-Challan issued with evidence.",
    params: { camera_id: "C-KARAL-EXIT" },
  },
  {
    id: "TFC-3",
    runner: "tfc3",
    blurb:
      "UC-II DPD release spike (2.5×) → corridor demand surge; forecaster build-up; gate-slot reissue.",
    params: { dpd_release_spike: 2.5 },
  },
];

export default function WhatIfConsole() {
  const { scenario, setScenario, reset: resetBanner } = useScenario();
  const { scenarioSteps } = useSocket();
  const [guided, setGuided] = useState(true);
  // Restore the active run from the tour store so the timeline survives a round
  // trip to another screen while a guided scenario is mid-flight (the guided
  // runtime switches the view away and back). Falls back to null for a fresh
  // console.
  const [activeHandle, setActiveHandle] = useState<string | null>(
    () => tourStore.getState().handleId,
  );
  const [activeRunner, setActiveRunner] = useState<string | null>(() => {
    const sid = tourStore.getState().scenarioId;
    return sid ? (getScript(sid)?.runner ?? null) : null;
  });

  const run = useMutation({
    mutationFn: (s: (typeof SCENARIOS)[number]) => getAdapter().runScenario(s.runner, s.params),
  });
  const resetRun = useMutation({
    mutationFn: () => getAdapter().resetScenario(activeRunner!, activeHandle ?? undefined),
  });

  // Backfill the timeline from the DB for the active handle (covers steps that
  // fired before this screen mounted), merged with live WS steps.
  const timelineQ = useQuery({
    queryKey: ["timeline", activeHandle],
    queryFn: () => getAdapter().scenarioTimeline(activeHandle!),
    enabled: !!activeHandle,
    refetchInterval: 4000,
  });

  const steps: ScenarioStep[] = useMemo(() => {
    const live = activeHandle ? (scenarioSteps[activeHandle] ?? []) : [];
    const fetched = (timelineQ.data?.steps ?? []) as ScenarioStep[];
    const byNo = new Map<number, ScenarioStep>();
    for (const s of fetched)
      byNo.set(s.step_no, { ...s, handle_id: activeHandle!, scenario: activeRunner! });
    for (const s of live) byNo.set(s.step_no, s);
    return [...byNo.values()].sort((a, b) => a.step_no - b.step_no);
  }, [scenarioSteps, activeHandle, activeRunner, timelineQ.data]);

  // The adapter's timeline returns { handle_id, steps }; the trace id rides on
  // the steps (each ScenarioStep carries trace_id).
  const traceId = steps.find((s) => s.trace_id)?.trace_id ?? undefined;

  async function trigger(s: (typeof SCENARIOS)[number]) {
    setScenario(s.id);
    setActiveRunner(s.runner);
    const res = await run.mutateAsync(s);
    setActiveHandle(res.handle_id);
    // Start the guided coach-mark in parallel with the real run; the app-level
    // GuidedTour follows this handle's live steps over /api/ws — narrating,
    // advancing the step, and switching the visible view as each step lands.
    if (guided) tourStore.startScenario(s.id, res.handle_id);
  }

  async function onReset() {
    tourStore.stopScenario();
    if (activeRunner) await resetRun.mutateAsync();
    resetBanner();
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border p-4">
        <div className="flex items-center gap-2">
          <FlaskConical className="h-5 w-5 text-primary" />
          <div>
            <h1 className="text-lg font-semibold">What-If Console</h1>
            <p className="text-sm text-muted-foreground">
              Trigger a scenario, watch the reactive chain, reset to baseline.
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant={guided ? "default" : "outline"}
            size="sm"
            onClick={() => setGuided((g) => !g)}
            title="Show the step-by-step guided narration while a scenario runs"
          >
            <Sparkles className="h-3.5 w-3.5" />
            Guided {guided ? "on" : "off"}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={onReset}
            disabled={resetRun.isPending || !activeRunner}
          >
            {resetRun.isPending ? <Spinner /> : <RotateCcw className="h-3.5 w-3.5" />}
            Reset to baseline
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3 p-4 md:grid-cols-3">
        {SCENARIOS.map((s) => {
          const active = scenario === s.id && !!activeHandle;
          return (
            <Card key={s.id} className={active ? "border-primary" : ""}>
              <CardHeader className="flex-row items-center justify-between">
                <CardTitle>{SCENARIO_LABELS[s.id]}</CardTitle>
                {active && <Badge colour="#56B4E9">running</Badge>}
              </CardHeader>
              <CardContent className="space-y-3">
                <p className="text-xs text-muted-foreground">{s.blurb}</p>
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(s.params).map(([key, value]) => (
                    <span
                      key={key}
                      className="inline-flex items-center gap-1 rounded-md border border-border bg-muted/50 px-2 py-1 text-[11px]"
                    >
                      <span className="text-muted-foreground">{humanizeParamKey(key)}</span>
                      <span className="font-mono font-medium text-foreground">{String(value)}</span>
                    </span>
                  ))}
                </div>
                <Button size="sm" onClick={() => trigger(s)} disabled={run.isPending}>
                  {run.isPending && run.variables?.id === s.id ? (
                    <Spinner />
                  ) : (
                    <Play className="h-3.5 w-3.5" />
                  )}
                  Run {s.id}
                </Button>
              </CardContent>
            </Card>
          );
        })}
      </div>

      {/* Storyline */}
      <div
        className="min-h-0 flex-1 overflow-y-auto border-t border-border p-4"
        data-guided-id="whatif-timeline"
      >
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-semibold">
            Reactive timeline {activeRunner ? `· ${activeRunner.toUpperCase()}` : ""}
          </h2>
          {traceId && (
            <a
              href="http://localhost:16686"
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-xs text-severity-info hover:underline"
              title={traceId}
            >
              <ExternalLink className="h-3.5 w-3.5" /> Open trace in Jaeger
            </a>
          )}
        </div>

        {!activeHandle ? (
          <p className="text-sm text-muted-foreground">
            Run a scenario to see its step-by-step storyline.
          </p>
        ) : steps.length === 0 ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner /> waiting for steps…
          </div>
        ) : (
          <ol className="relative space-y-3 pl-6">
            {steps.map((s) => (
              <li key={s.step_no} className="relative">
                <span
                  className="absolute -left-[18px] top-1.5 h-3 w-3 rounded-full border-2 border-background"
                  style={{ backgroundColor: stepColour(s.status) }}
                  aria-hidden
                />
                <div className="flex items-center gap-2">
                  <span className="text-xs font-semibold tabular-nums text-muted-foreground">
                    #{s.step_no}
                  </span>
                  <span className="text-sm font-medium">{s.title}</span>
                  <Badge colour={stepColour(s.status)}>{s.status}</Badge>
                  <span className="ml-auto text-[10px] text-muted-foreground">
                    {fmtTimeIST(s.ts)}
                  </span>
                </div>
                {s.trigger && (
                  <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">
                    ↳ {s.trigger}
                  </div>
                )}
                <CrossTwinArrow step={s} />
              </li>
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}

function stepColour(status: string): string {
  if (status === "failed") return "#D55E00";
  if (status === "degraded") return "#E69F00";
  if (status === "info") return "#56B4E9";
  return "#009E73";
}

/** "gate_id" → "Gate id" — readable label for a scenario param chip. */
function humanizeParamKey(key: string): string {
  const spaced = key.replace(/[_-]+/g, " ").trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

// TFC-3 step 5 carries an explicit cross-twin arrow annotation.
function CrossTwinArrow({ step }: { step: ScenarioStep }) {
  const arrow = step.detail?.arrow as { from: string; to: string } | undefined;
  if (!arrow) return null;
  return (
    <div
      data-guided-id="crosstwin-link"
      className="mt-1 inline-flex items-center gap-2 rounded-md border border-primary/40 bg-primary/10 px-2 py-1 text-xs"
    >
      <span className="font-medium">{arrow.from}</span>
      <ArrowRight className="h-3.5 w-3.5 text-primary" />
      <span className="font-medium">{arrow.to}</span>
    </div>
  );
}
