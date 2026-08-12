// What-If Console — scenario planning interface (FINAL PHASE redesign).
// Trigger TFC-1/2/3, watch the reactive storyline paint live from /api/ws
// (type=scenario_step), preview recorded & demo runs, and reset to baseline.
// The execution flow (runScenario / resetScenario / guided tour / WS timeline /
// handle preview) is preserved verbatim — only the presentation is reworked onto
// the DTCCC kit, with Live / Recorded / Demo separation, a comparison strip, a
// timeline visualization and status badges. No backend changes.

import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery } from "@tanstack/react-query";
import { getAdapter } from "@/data";
import { api } from "@/lib/api";
import { useScenario, SCENARIO_LABELS, type ScenarioId } from "@/hooks/ScenarioContext";
import { useSocket } from "@/hooks/SocketContext";
import type { ScenarioStep } from "@/lib/types";
import { Card } from "@/components/ui/card";
import { Spinner } from "@/components/ui/misc";
import {
  PageContainer,
  PageHeader,
  StatGrid,
  StatCard,
  StatusChip,
  type Tone,
} from "@/components/ui/dtccc";
import { fmtTimeIST } from "@/lib/utils";
import {
  FlaskConical,
  Play,
  RotateCcw,
  ArrowRight,
  ExternalLink,
  Sparkles,
  Radio,
  Film,
  Clapperboard,
  GitCompare,
} from "lucide-react";
import { tourStore } from "@/whatif/tourStore";
import {
  IDLE_RUN,
  isBusy,
  previewRun,
  resetRun,
  runFailed,
  runStarted,
  startRun,
  type WhatIfRunState,
} from "@/whatif/runState";
import { getScript } from "@/whatif/scenarioScripts";
import { ReactiveGuidePanel } from "@/components/panels/ReactiveGuidePanel";

const SCENARIOS: { id: ScenarioId; runner: string; blurb: string; params: Record<string, any> }[] =
  [
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
    {
      id: "MONSOON-FRIDAY",
      runner: "monsoon_friday",
      blurb:
        "Heavy Rain — cascades to driver & fuel shortage + reactive recommendations. Monsoon rain + Friday peak → congestion → demand surge → gate queue → reroute → carbon impact.",
      params: { gate_id: "G-NSICT", rain_intensity: "heavy", demand_trucks: 120 },
    },
  ];

// Display-only theme labels that make the requested scenario themes
// discoverable in the UI. These NEVER change the backend `runner` name or the
// run/reset wiring — they only annotate the shared SCENARIO_LABELS title. Any
// id without an override falls back to SCENARIO_LABELS unchanged.
const SCENARIO_THEME: Partial<Record<ScenarioId, string>> = {
  "TFC-1": "Port Congestion",
  "MONSOON-FRIDAY": "Heavy Rain",
};
function displayLabel(id: ScenarioId): string {
  const theme = SCENARIO_THEME[id];
  return theme ? `${SCENARIO_LABELS[id]} · ${theme}` : SCENARIO_LABELS[id];
}

function stepTone(status: string): Tone {
  if (status === "failed") return "critical";
  if (status === "degraded") return "warn";
  if (status === "info") return "info";
  return "ok";
}

export default function WhatIfConsole() {
  const { t } = useTranslation();
  const { scenario, setScenario, reset: resetBanner } = useScenario();
  const { scenarioSteps } = useSocket();
  const [guided, setGuided] = useState(true);
  // ONE value owns the run lifecycle (see whatif/runState.ts) — a handle can
  // never outlive the scenario it belongs to, and every failure path settles.
  const [runState, setRunState] = useState<WhatIfRunState>(() => {
    // Returning to the console mid-tour restores the run the tour is following.
    const { scenarioId: sid, handleId } = tourStore.getState();
    if (!sid) return IDLE_RUN;
    return {
      scenarioId: sid,
      runner: getScript(sid)?.runner ?? null,
      handleId,
      status: handleId ? "running" : "idle",
      error: null,
    };
  });
  const activeHandle = runState.handleId;
  const activeRunner = runState.runner;

  const run = useMutation({
    mutationFn: (s: (typeof SCENARIOS)[number]) => getAdapter().runScenario(s.runner, s.params),
  });
  const resetScenarioRun = useMutation({
    mutationFn: () => getAdapter().resetScenario(activeRunner!, activeHandle ?? undefined),
  });

  const timelineQ = useQuery({
    queryKey: ["timeline", activeHandle],
    queryFn: () => getAdapter().scenarioTimeline(activeHandle!),
    enabled: !!activeHandle,
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

  const traceId = steps.find((s) => s.trace_id)?.trace_id ?? undefined;

  async function trigger(s: (typeof SCENARIOS)[number]) {
    // Guard the double-submit: one run at a time, and the button is disabled
    // while a submit is in flight (below) so this is belt-and-braces.
    if (isBusy(runState)) return;
    // Tear the PREVIOUS run down before starting a new one: stop its guided tour
    // (otherwise the old script keeps driving the view and its coach-mark state
    // outlives the run) and drop its handle, so the timeline/steps below cannot
    // show run #1's output under run #2.
    tourStore.stopScenario();
    setRunState((prev) => startRun(prev, s));
    setScenario(s.id);
    try {
      const res = await run.mutateAsync(s);
      setRunState((prev) => runStarted(prev, res.handle_id));
      if (guided) tourStore.startScenario(s.id, res.handle_id);
    } catch (err) {
      // A failed submit must settle like any other terminal state — never leave
      // the console "starting" forever, and never leave the previous run's
      // handle attached to it.
      setRunState((prev) => runFailed(prev, (err as Error)?.message ?? "Scenario run failed"));
      resetBanner();
    }
  }
  async function onReset() {
    tourStore.stopScenario();
    try {
      if (activeRunner) await resetScenarioRun.mutateAsync();
    } finally {
      setRunState(resetRun());
      resetBanner();
    }
  }

  const handlesQ = useQuery({
    queryKey: ["scenario-handles"],
    queryFn: () => api.scenarioHandles(50),
  });
  function previewHandle(h: { handle_id: string; name: string }) {
    tourStore.stopScenario();
    setRunState(previewRun(h.name, h.handle_id));
  }

  const allHandles = handlesQ.data?.handles ?? [];
  const demoHandles = allHandles.filter((h) => h.is_demo);
  const recordedHandles = allHandles.filter((h) => !h.is_demo && h.step_count > 0);
  const blurbFor = (runner: string | null) =>
    SCENARIOS.find((s) => s.runner === runner)?.blurb ?? "";
  const previewingDemo = demoHandles.some((h) => h.handle_id === activeHandle);

  return (
    <PageContainer>
      <PageHeader
        icon={FlaskConical}
        title={t("nav.whatIf")}
        subtitle={t("whatIf.subtitle")}
        actions={
          <div className="flex items-center gap-2">
            <button
              onClick={() => setGuided((g) => !g)}
              title={t("whatIf.guidedTooltip")}
              className={`inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors ${guided ? "bg-primary text-primary-foreground" : "border border-border hover:bg-muted"}`}
            >
              <Sparkles className="h-3.5 w-3.5" />{" "}
              {guided ? t("whatIf.guidedOn") : t("whatIf.guidedOff")}
            </button>
            <button
              onClick={onReset}
              disabled={resetScenarioRun.isPending || !activeRunner}
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium transition-colors hover:bg-muted disabled:opacity-40"
            >
              {resetScenarioRun.isPending ? <Spinner /> : <RotateCcw className="h-3.5 w-3.5" />}{" "}
              {t("whatIf.resetToBaseline")}
            </button>
          </div>
        }
      />

      {/* Status summary */}
      <div className="px-4 pt-3">
        <StatGrid className="lg:grid-cols-4">
          <StatCard icon={Radio} label="Live Scenarios" value={SCENARIOS.length} tone="info" />
          <StatCard
            icon={Film}
            label="Recorded Runs"
            value={recordedHandles.length}
            tone="info"
            loading={handlesQ.isLoading}
          />
          <StatCard
            icon={Clapperboard}
            label="Demo Scenarios"
            value={demoHandles.length}
            tone="warn"
            loading={handlesQ.isLoading}
          />
          <StatCard
            icon={Play}
            label="Active Run"
            value={activeRunner ? activeRunner.toUpperCase() : "—"}
            tone={activeHandle ? "ok" : "neutral"}
            sub={activeHandle ? `${steps.length} steps` : "idle"}
          />
        </StatGrid>
      </div>

      {/* Live Scenarios */}
      <Section
        title="Live Scenarios"
        icon={Radio}
        hint="Trigger a reactive what-if chain (TFC-1/2/3)"
      >
        <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
          {SCENARIOS.map((s) => {
            const active = runState.scenarioId === s.id && !!activeHandle;
            return (
              <Card key={s.id} className={`p-3 ${active ? "border-primary" : ""}`}>
                <div className="mb-2 flex items-center justify-between">
                  <h3 className="text-sm font-semibold">{displayLabel(s.id)}</h3>
                  {active && <StatusChip label={t("whatIf.running")} tone="ok" />}
                </div>
                <p className="mb-2 text-xs text-muted-foreground">{t(`whatIf.blurb.${s.id}`)}</p>
                <div className="mb-3 flex flex-wrap gap-1.5">
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
                <button
                  onClick={() => void trigger(s)}
                  disabled={isBusy(runState)}
                  className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                >
                  {isBusy(runState) && runState.scenarioId === s.id ? (
                    <Spinner className="text-primary-foreground" />
                  ) : (
                    <Play className="h-3.5 w-3.5" />
                  )}
                  {t("whatIf.run", { id: s.id })}
                </button>
                {/* A failed submit is reported on the card that owns it — the
                    run no longer fails silently into a console that just stops. */}
                {runState.status === "failed" && runState.scenarioId === s.id && (
                  <p className="mt-2 text-[11px] text-severity-critical" role="alert">
                    {runState.error}
                  </p>
                )}
              </Card>
            );
          })}
        </div>

        {/* Theme coverage caption — keeps the catalogue honest: Driver Shortage
            and Fuel Shortage are not standalone runs, they are modelled as
            cascading sub-effects inside Heavy Rain; Major Accident has no
            backend scenario yet. */}
        <p className="mt-2 text-[11px] text-muted-foreground">
          Driver Shortage and Fuel Shortage are sub-effects modelled inside Heavy Rain (Monsoon
          Friday). Major Accident is planned — no live scenario yet.
        </p>

        {/* Scenario comparison */}
        <Card className="mt-3 overflow-hidden">
          <div className="flex items-center gap-2 border-b border-border px-3 py-2">
            <GitCompare className="h-4 w-4 text-primary" />
            <h3 className="text-sm font-semibold">Scenario Comparison</h3>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[13px]">
              <thead className="bg-muted/60 text-[11px] uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th className="px-3 py-2">Scenario</th>
                  <th className="px-3 py-2">Trigger</th>
                  <th className="px-3 py-2">Key parameter</th>
                  <th className="px-3 py-2">State</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {SCENARIOS.map((s) => {
                  const active = runState.scenarioId === s.id && !!activeHandle;
                  const [k, v] = Object.entries(s.params)[0];
                  return (
                    <tr key={s.id}>
                      <td className="px-3 py-2 font-medium">{displayLabel(s.id)}</td>
                      <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                        {s.runner}
                      </td>
                      <td className="px-3 py-2 font-mono text-xs">
                        {humanizeParamKey(k)} = {String(v)}
                      </td>
                      <td className="px-3 py-2">
                        <StatusChip
                          label={active ? "RUNNING" : "Idle"}
                          tone={active ? "ok" : "neutral"}
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      </Section>

      {/* Demo Scenarios */}
      {demoHandles.length > 0 && (
        <Section
          title="Demo Scenarios"
          icon={Clapperboard}
          hint="Preview a recorded demo run (read-only — does not start a live simulation)"
        >
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {demoHandles.map((h) => (
              <button
                key={h.handle_id}
                onClick={() => previewHandle(h)}
                title={h.handle_id}
                className={`flex flex-col items-start gap-1 rounded-md border px-3 py-2 text-left text-xs transition-colors ${activeHandle === h.handle_id ? "border-primary bg-primary/10" : "border-border hover:bg-muted"}`}
              >
                <span className="flex items-center gap-2">
                  <span className="font-semibold uppercase">{h.name}</span>
                  <StatusChip label="DEMO" tone="warn" />
                  <span className="text-muted-foreground">
                    {h.step_count} steps · {h.status}
                  </span>
                </span>
                <span className="line-clamp-2 text-[11px] text-muted-foreground">
                  {blurbFor(h.name)}
                </span>
              </button>
            ))}
          </div>
        </Section>
      )}

      {/* Recorded Runs */}
      {recordedHandles.length > 0 && (
        <Section title="Recorded Runs" icon={Film} hint="Past live runs — read-only preview">
          <div className="flex flex-wrap gap-2">
            {recordedHandles.map((h) => (
              <button
                key={h.handle_id}
                onClick={() => previewHandle(h)}
                title={h.handle_id}
                className={`inline-flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-xs transition-colors ${activeHandle === h.handle_id ? "border-primary bg-primary/10" : "border-border hover:bg-muted"}`}
              >
                <span className="font-medium uppercase">{h.name}</span>
                <span className="text-muted-foreground">
                  {h.step_count} steps · {h.status}
                </span>
              </button>
            ))}
          </div>
        </Section>
      )}

      {/* Reactive timeline */}
      {/* shrink-0 (not flex-1/min-h-0): inside the page's scrolling flex column
          a flex-grow item collapses below its content height once the page
          overflows, letting this section's list paint over the panel below. */}
      <div className="shrink-0 border-t border-border p-4" data-guided-id="whatif-timeline">
        <div className="mb-3 flex items-center justify-between">
          <div>
            <h2 className="flex items-center gap-2 text-sm font-semibold">
              {t("whatIf.reactiveTimeline")} {activeRunner ? `· ${activeRunner.toUpperCase()}` : ""}
              {previewingDemo && <StatusChip label="DEMO PREVIEW" tone="warn" />}
            </h2>
            {activeRunner && blurbFor(activeRunner) && (
              <p className="mt-0.5 max-w-3xl text-xs text-muted-foreground">
                {blurbFor(activeRunner)}
              </p>
            )}
          </div>
          {traceId && (
            <a
              href="http://localhost:16686"
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-xs text-severity-info hover:underline"
              title={traceId}
            >
              <ExternalLink className="h-3.5 w-3.5" /> {t("whatIf.openTrace")}
            </a>
          )}
        </div>

        {!activeHandle ? (
          <Card className="p-6 text-center text-sm text-muted-foreground">
            {t("whatIf.emptyTimeline")}
          </Card>
        ) : steps.length === 0 ? (
          <Card className="flex items-center justify-center gap-2 p-6 text-sm text-muted-foreground">
            <Spinner /> {t("whatIf.waitingForSteps")}
          </Card>
        ) : (
          <Card className="p-4">
            <ol className="relative space-y-3 pl-6">
              <span className="absolute left-[5px] top-1.5 bottom-1.5 w-px bg-border" aria-hidden />
              {steps.map((s) => (
                <li key={s.step_no} className="relative">
                  <span
                    className="absolute -left-[18px] top-1.5 h-3 w-3 rounded-full border-2 border-card"
                    style={{ backgroundColor: toneColour(stepTone(s.status)) }}
                    aria-hidden
                  />
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs font-semibold tabular-nums text-muted-foreground">
                      #{s.step_no}
                    </span>
                    <span className="text-sm font-medium">{s.title}</span>
                    <StatusChip label={s.status} tone={stepTone(s.status)} />
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
          </Card>
        )}
      </div>

      {/* Reactive Guide — explainable causal chain (UC-3 P1). Locks to the
          running scenario, else lets the user explore a teaching chain. */}
      <div className="px-4 pb-6">
        <ReactiveGuidePanel scenarioId={activeHandle ? scenario : null} />
      </div>

      {/* Cross-twin XT-2 proof surface: DeferredArrivalWindow events consumed
          from UC-II (Kafka jnpa.crosstwin.deferred-arrival) -> TAS metering. */}
      <div className="px-4 pb-6">
        <DeferredWindowsCard />
      </div>
    </PageContainer>
  );
}

function DeferredWindowsCard() {
  const q = useQuery({
    queryKey: ["tas-deferred-windows"],
    queryFn: api.tasDeferredWindows,
    refetchInterval: 10_000,
  });
  const windows = q.data?.windows ?? [];
  return (
    <Card className="p-4">
      <div className="mb-2 flex items-center justify-between">
        <div className="text-sm font-semibold">
          UC-II Deferred-Arrival Windows (cross-twin TAS metering)
        </div>
        <StatusChip
          tone={windows.length ? ("warn" as Tone) : ("ok" as Tone)}
          label={windows.length ? `${windows.length} active` : "none consumed"}
        />
      </div>
      {windows.length === 0 ? (
        <div className="text-xs text-muted-foreground">
          No DeferredArrivalWindow consumed yet — publish one on Kafka topic{" "}
          <code>jnpa.crosstwin.deferred-arrival</code> (e.g. UC-II scenario S2) and it will appear
          here with its TAS re-slots and booking cap.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-left text-muted-foreground">
              <tr>
                <th className="py-1 pr-3">Correlation</th>
                <th className="py-1 pr-3">Gate</th>
                <th className="py-1 pr-3">Window (UTC)</th>
                <th className="py-1 pr-3">Slots re-scheduled</th>
                <th className="py-1 pr-3">Bookings (cap)</th>
                <th className="py-1 pr-3">Source</th>
              </tr>
            </thead>
            <tbody>
              {windows.map((w: any) => (
                <tr key={w.correlation_id} className="border-t border-border/50">
                  <td className="py-1 pr-3 font-mono">{w.correlation_id}</td>
                  <td className="py-1 pr-3">{w.gate_id ?? "all"}</td>
                  <td className="py-1 pr-3">
                    {String(w.window_start).slice(11, 16)}–{String(w.window_end).slice(11, 16)} (
                    {w.window_min} min)
                  </td>
                  <td className="py-1 pr-3">{w.applied_slots?.length ?? 0}</td>
                  <td className="py-1 pr-3">
                    {w.booked} / {w.slot_cap}
                    {w.booked >= w.slot_cap ? " — refusing" : ""}
                  </td>
                  <td className="py-1 pr-3">{w.source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function Section({
  title,
  icon: Icon,
  hint,
  children,
}: {
  title: string;
  icon: LucideIconType;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="border-t border-border px-4 py-3">
      <div className="mb-2 flex items-center gap-2">
        <Icon className="h-4 w-4 text-primary" />
        <h2 className="text-sm font-semibold">{title}</h2>
        {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
      </div>
      {children}
    </div>
  );
}
type LucideIconType = typeof FlaskConical;

function humanizeParamKey(key: string): string {
  const spaced = key.replace(/[_-]+/g, " ").trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
function toneColour(t: Tone): string {
  return {
    info: "#56B4E9",
    ok: "#009E73",
    warn: "#E69F00",
    critical: "#D55E00",
    neutral: "#64748b",
  }[t];
}

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
