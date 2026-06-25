/**
 * GuidedTour — the coach-mark overlay that narrates a What-If scenario as the
 * real tfc1/2/3 chain runs, AND switches the visible view per step.
 *
 * This reproduces the reference project's runtime mechanism exactly. In
 * jnpa_poc_2 (apps/web): each scenario step carries `step.tab`; GuidedTour fires
 * `onTab(step.tab)` in a useEffect on every step change; the host (Dashboard)
 * owns the active-view state and passes `onTab={(tab) => setActiveTab(tab)}` —
 * so the visible tab/panel changes as the tour advances. It is host-owned
 * active-view state, NOT React Router.
 *
 * Here the mechanism is identical — per-step target (`step.target.page`), an
 * `onView(step.target.page)` callback fired on step change — but the host (App)
 * owns *routes* instead of tabs, so it passes `onView={navigate}`. Same
 * mechanism, bound to this project's view system. The overlay is mounted ONCE at
 * the app level (web/src/App.tsx) so it survives the view change (in the
 * reference the tabbed Dashboard never unmounts; here switching routes would
 * unmount the page, so the overlay lives above the router outlet). The tour step
 * is advanced by the real scenario_step WebSocket frames (tourStore.syncLive)
 * with a timer fallback.
 *
 * Each step also declares the EXACT business object it describes (step.target):
 * a "dom" target rings the tagged component (data-guided-id) on the active page
 * (scrolled into view if needed); a "map" target rings the gate/segment on the
 * Live map (the map owns that halo — see ArcgisMap/LiveOperations). The overlay
 * never highlights a generic element. Purely additive: it floats above the
 * existing UI and never alters it.
 */
import { type PointerEvent as RPointerEvent, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Circle,
  FlaskConical,
  ListChecks,
  MapPin,
  Pause,
  Play,
  RotateCcw,
  X,
  Zap,
} from "lucide-react";
import { useSocket } from "@/hooks/SocketContext";
import { useScenario } from "@/hooks/ScenarioContext";
import { getAdapter } from "@/data";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useTargetRect } from "./coachmarkTargets";
import { getScript, type MetricChange } from "./scenarioScripts";
import { tourStore, TOUR_STEP_MS } from "./tourStore";
import { useTourStore } from "./useTourStore";

const TONE_COLOUR: Record<MetricChange["tone"], string> = {
  worse: "#D55E00",
  better: "#009E73",
  neutral: "#56B4E9",
};

export function GuidedTour({
  /**
   * Host-owned view switcher — the direct analog of the reference's
   * `onTab={(tab) => setActiveTab(tab)}`. App passes `onView={navigate}`, so the
   * tour changes the visible screen by driving the host's view system (routes).
   */
  onView,
}: {
  onView: (view: string) => void;
}) {
  const { scenarioId, handleId, stepIndex, autoAdvance, stepStartedAt } = useTourStore();
  const { scenarioSteps } = useSocket();
  const { reset: resetBanner } = useScenario();
  const script = scenarioId ? getScript(scenarioId) : undefined;
  const step = script?.steps[stepIndex];

  // The live (WebSocket) steps for the run this tour follows. Read straight from
  // SocketContext so the tour works on every view, not just /what-if.
  const liveSteps = handleId ? (scenarioSteps[handleId] ?? []) : [];
  const liveCount = liveSteps.length;

  // Advance the tour step from the real backend: each scenario_step frame moves
  // the narration forward (forward-only, honours pause — see syncLive).
  useEffect(() => {
    if (handleId) tourStore.syncLive(liveCount);
  }, [liveCount, handleId]);

  // VIEW SWITCH — the exact reproduction of the reference's onTab effect
  // (apps/web GuidedTour.tsx): on every step change, switch the visible view to
  // this step's target page. `lastView` keyed by `${stepIndex}:${page}` mirrors
  // the reference's `lastTab` guard so we fire once per step.
  const targetPage = step?.target.page ?? null;
  const lastView = useRef<string | null>(null);
  useEffect(() => {
    if (!targetPage) return;
    const key = `${stepIndex}:${targetPage}`;
    if (lastView.current !== key) {
      lastView.current = key;
      onView(targetPage);
    }
  }, [targetPage, stepIndex, onView]);

  // Ring the EXACT business object this step describes. For a DOM target, resolve
  // its tagged element (data-guided-id) and ring it (scrolling it into view when
  // needed). For a map target, the ring lives on the map (halo + goTo) — no DOM
  // ring here, so the overlay never highlights a generic element on a map step.
  const domTarget = step?.target.kind === "dom" ? (step.target.selector ?? null) : null;
  const scrollIntoView = (step?.target.scrollBehaviour ?? "center") !== "none";
  const rect = useTargetRect(domTarget, scrollIntoView, stepIndex);

  const [collapsed, setCollapsed] = useState(false);
  const [navOpen, setNavOpen] = useState(false);

  // Draggable coach-mark: the operator can move it off whatever it covers, on
  // ANY page (it is the same app-level overlay everywhere). `pos` null = default
  // dock (bottom-right); dragging the header pins it to a fixed x/y.
  const cardRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{ sx: number; sy: number; bx: number; by: number } | null>(null);
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);

  function onDragDown(e: RPointerEvent<HTMLDivElement>) {
    // Don't start a drag from a control inside the header (e.g. minimise).
    if ((e.target as HTMLElement).closest("button")) return;
    const r = cardRef.current?.getBoundingClientRect();
    dragRef.current = {
      sx: e.clientX,
      sy: e.clientY,
      bx: pos?.x ?? r?.left ?? 0,
      by: pos?.y ?? r?.top ?? 0,
    };
    e.currentTarget.setPointerCapture(e.pointerId);
  }
  function onDragMove(e: RPointerEvent<HTMLDivElement>) {
    const d = dragRef.current;
    if (!d) return;
    const w = cardRef.current?.offsetWidth ?? 380;
    // Keep the card on screen (header always reachable).
    const x = Math.max(0, Math.min(window.innerWidth - w, d.bx + (e.clientX - d.sx)));
    const y = Math.max(0, Math.min(window.innerHeight - 48, d.by + (e.clientY - d.sy)));
    setPos({ x, y });
  }
  function onDragUp(e: RPointerEvent<HTMLDivElement>) {
    dragRef.current = null;
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* pointer already released */
    }
  }

  // Progress bar for auto-advance — purely visual, resets each step.
  const [progress, setProgress] = useState(0);
  useEffect(() => {
    setProgress(0);
    if (!autoAdvance || !script) return;
    if (stepIndex >= script.steps.length - 1) return; // last step: no bar
    const start = performance.now();
    let raf = 0;
    const loop = (now: number) => {
      const p = Math.min(1, (now - start) / TOUR_STEP_MS);
      setProgress(p);
      if (p < 1) raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [autoAdvance, stepIndex, stepStartedAt, script]);

  if (!script || !step) return null;

  const isLast = stepIndex >= script.steps.length - 1;
  const ringColour = TONE_COLOUR[step.metrics[0]?.tone ?? "neutral"];

  // End/Finish: run the real reset-to-baseline against the gateway, clear the
  // scenario banner + tour state, and switch the view back to the console (via
  // the host, like every other view change).
  function end() {
    const runner = script?.runner;
    const hid = handleId ?? undefined;
    tourStore.stopScenario();
    resetBanner();
    if (runner)
      getAdapter()
        .resetScenario(runner, hid)
        .catch(() => {});
    onView("/what-if");
  }

  return (
    <>
      {/* One-time keyframes for the pin-point pulse. */}
      <style>{`
        @keyframes jnpaTourPulse {
          0%   { box-shadow: 0 0 0 0 var(--jnpa-tour-glow); }
          70%  { box-shadow: 0 0 0 9px transparent; }
          100% { box-shadow: 0 0 0 0 transparent; }
        }
      `}</style>

      {/* Pulsing ring around the EXACT live timeline row for this step. */}
      {rect && (
        <div
          aria-hidden
          style={{
            position: "fixed",
            top: rect.top - 4,
            left: rect.left - 6,
            width: rect.width + 12,
            height: rect.height + 8,
            border: `2.5px solid ${ringColour}`,
            borderRadius: 8,
            zIndex: 950,
            pointerEvents: "none",
            ["--jnpa-tour-glow" as never]: `${ringColour}66`,
            animation: "jnpaTourPulse 1.6s ease-out infinite",
            transition: "top 200ms ease, left 200ms ease, width 200ms ease, height 200ms ease",
          }}
        />
      )}

      {/* Collapsed pill — a tiny chip so the screen stays fully visible. */}
      {collapsed && (
        <button
          onClick={() => setCollapsed(false)}
          aria-label="Expand guided scenario"
          className="fixed bottom-4 right-4 z-[1000] inline-flex items-center gap-2 rounded-full bg-primary px-3.5 py-2 text-xs font-semibold text-primary-foreground shadow-lg"
        >
          <FlaskConical className="h-3.5 w-3.5" />
          Guided · {script.title} · {stepIndex + 1}/{script.steps.length}
          <ChevronUp className="h-3.5 w-3.5" />
        </button>
      )}

      {/* Coach-mark card — docked bottom-right by default, draggable by its
          header, collapsible. When dragged, `pos` pins it to a fixed x/y. */}
      {!collapsed && (
        <div
          ref={cardRef}
          role="dialog"
          aria-label={`Guided scenario: ${script.title}`}
          className="fixed bottom-4 right-4 z-[1000] flex max-h-[calc(100vh-2rem)] w-[min(380px,calc(100vw-2rem))] flex-col overflow-hidden rounded-xl border border-border bg-card shadow-2xl"
          style={pos ? { left: pos.x, top: pos.y, right: "auto", bottom: "auto" } : undefined}
        >
          {/* Header — also the drag handle. */}
          <div
            onPointerDown={onDragDown}
            onPointerMove={onDragMove}
            onPointerUp={onDragUp}
            className="flex cursor-move touch-none select-none items-center gap-2 bg-primary px-3 py-2.5 text-primary-foreground"
          >
            <FlaskConical className="h-4 w-4" />
            <strong className="text-sm">Guided · {script.title}</strong>
            <span className="ml-auto text-xs opacity-90">
              {stepIndex + 1}/{script.steps.length}
            </span>
            <button
              onClick={() => setCollapsed(true)}
              aria-label="Minimise"
              title="Minimise"
              className="flex p-0.5 text-primary-foreground/90 hover:text-primary-foreground"
            >
              <ChevronDown className="h-4 w-4" />
            </button>
          </div>

          {/* Progress dots */}
          <div className="flex gap-1.5 px-3 pt-2">
            {script.steps.map((_, i) => (
              <button
                key={i}
                onClick={() => tourStore.gotoStep(i)}
                aria-label={`Go to step ${i + 1}`}
                className="relative h-1.5 flex-1 overflow-hidden rounded-full"
              >
                <span
                  className="absolute inset-0 rounded-full"
                  style={{
                    backgroundColor:
                      i < stepIndex ? "#009E73" : i === stepIndex ? "#9ca3af" : "#e5e7eb",
                  }}
                  aria-hidden
                />
                {i === stepIndex && (
                  <span
                    className="absolute inset-0 origin-left rounded-full bg-primary"
                    style={{
                      transform: `scaleX(${progress})`,
                      transition: "transform 80ms linear",
                    }}
                    aria-hidden
                  />
                )}
              </button>
            ))}
          </div>

          {/* Body */}
          <div className="overflow-y-auto px-3 pb-1 pt-2.5">
            <div className="text-sm font-semibold text-foreground">{step.title}</div>
            {/* Where to look: current page + the exact business object. */}
            <div className="mt-1 flex items-center gap-1.5 text-[11px] text-primary">
              <MapPin className="h-3 w-3 shrink-0" />
              <span className="min-w-0 truncate">
                <strong>{step.target.page}</strong> · {step.target.component}
              </span>
            </div>
            <p className="my-1.5 text-xs leading-relaxed text-muted-foreground">{step.explain}</p>

            {/* Metric chips: what's changing, before → after */}
            {step.metrics.length > 0 && (
              <div className="mb-2.5 flex flex-wrap gap-2">
                {step.metrics.map((m, i) => (
                  <div
                    key={i}
                    className="flex items-center gap-1.5 rounded-md border border-border bg-muted px-2 py-1 text-xs"
                  >
                    <span className="text-muted-foreground">{m.label}</span>
                    <span className="text-muted-foreground">{m.from}</span>
                    <ArrowRight className="h-3 w-3 text-muted-foreground" />
                    <strong style={{ color: TONE_COLOUR[m.tone] }}>
                      {m.to}
                      {m.unit ? ` ${m.unit}` : ""}
                    </strong>
                  </div>
                ))}
              </div>
            )}

            {/* Automated action the twin took */}
            {step.action && (
              <div className="mb-1.5 flex items-center gap-2 rounded-md border border-primary/30 bg-primary/10 px-2.5 py-1.5">
                <Badge colour="#56B4E9" className="shrink-0">
                  <Zap className="h-3 w-3" />
                  {step.action.kind.replace(/_/g, " ")}
                </Badge>
                <span className="text-xs text-foreground">{step.action.detail}</span>
              </div>
            )}
          </div>

          {/* Scenario Navigator — every step, with status; click any to jump.
              Jumping replays that step's page + highlight + coach-mark WITHOUT
              re-running the backend (tourStore.gotoStep only moves the index). */}
          <div className="border-t border-border">
            <button
              onClick={() => setNavOpen((o) => !o)}
              className="flex w-full items-center gap-1.5 px-3 py-1.5 text-[11px] font-medium text-muted-foreground hover:bg-muted"
            >
              <ListChecks className="h-3.5 w-3.5" />
              Scenario steps
              <ChevronDown
                className={cn("ml-auto h-3.5 w-3.5 transition-transform", navOpen && "rotate-180")}
              />
            </button>
            {navOpen && (
              <ol className="max-h-44 overflow-y-auto px-2 pb-2">
                {script.steps.map((st, i) => {
                  const done = i < stepIndex;
                  const current = i === stepIndex;
                  return (
                    <li key={i}>
                      <button
                        onClick={() => tourStore.gotoStep(i)}
                        className={cn(
                          "flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-muted",
                          current && "bg-primary/10",
                        )}
                      >
                        <span className="mt-0.5 shrink-0" aria-hidden>
                          {done ? (
                            <CheckCircle2 className="h-3.5 w-3.5 text-severity-ok" />
                          ) : current ? (
                            <Play className="h-3.5 w-3.5 text-primary" />
                          ) : (
                            <Circle className="h-3.5 w-3.5 text-muted-foreground" />
                          )}
                        </span>
                        <span className="min-w-0">
                          <span
                            className={cn(
                              "block truncate font-medium",
                              current ? "text-foreground" : "text-muted-foreground",
                            )}
                          >
                            {i + 1}. {st.title}
                          </span>
                          <span className="block truncate text-[10px] text-muted-foreground">
                            {st.target.page} · {st.target.component}
                          </span>
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ol>
            )}
          </div>

          {/* Footer controls */}
          <div className="flex flex-wrap items-center gap-1.5 border-t border-border px-3 py-2">
            <Button
              variant="outline"
              size="sm"
              disabled={stepIndex === 0}
              onClick={() => tourStore.prevStep()}
            >
              <ChevronLeft className="h-3.5 w-3.5" />
              Back
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => tourStore.setAutoAdvance(!autoAdvance)}
              title={autoAdvance ? "Pause auto-advance" : "Resume auto-advance"}
            >
              {autoAdvance ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
              {autoAdvance ? "Pause" : "Resume"}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => tourStore.gotoStep(0)}
              disabled={stepIndex === 0}
              title="Restart from step 1"
            >
              <RotateCcw className="h-3.5 w-3.5" />
              Restart
            </Button>
            <div className="ml-auto flex gap-1.5">
              <Button variant="outline" size="sm" onClick={end}>
                <X className="h-3.5 w-3.5" />
                End &amp; reset
              </Button>
              {isLast ? (
                <Button size="sm" onClick={end}>
                  <Check className="h-3.5 w-3.5" />
                  Finish
                </Button>
              ) : (
                <Button size="sm" onClick={() => tourStore.nextStep()}>
                  Next
                  <ChevronRight className="h-3.5 w-3.5" />
                </Button>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
