// SimulatorPage — the live-data control room for UC-III. A separate route
// (`/simulator`, opened on its own screen/tab) with per-faction controls: a
// master clock (play / pause / speed), guided What-If scenarios, gate truck
// queues, corridor-segment congestion, vehicle flow, vehicle injection, customs
// scan depth, parking availability, and incident injection.
//
// Every control writes to simStore, which the SimAdapter wrap overlays onto the
// dashboard's reads and SimBridge flushes to the live board (this tab or another
// via BroadcastChannel). Gate / segment ids come from the adapter so controls
// reference the *real* assets the map draws, and driving one highlights + pulses
// it on the Live Operations map (see LiveOperations + ArcgisMap).
//
// Layout follows jnpa_poc_2 apps/web/src/sim/SimulatorPage.tsx — a centred
// max-width column of titled control blocks — adapted to UC-III and the target's
// Calcite shell.

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  CalciteBlock,
  CalciteButton,
  CalciteChip,
  CalciteIcon,
  CalciteLabel,
  CalciteNavigation,
  CalciteNavigationLogo,
  CalciteNotice,
  CalcitePanel,
  CalciteSegmentedControl,
  CalciteSegmentedControlItem,
  CalciteShell,
  CalciteSlider,
} from "@esri/calcite-components-react";
import { getAdapter } from "@/data";
import { STATUS } from "@/lib/tokens";
import type { Gate, CorridorGeometry } from "@/lib/types";
import { simStore, TOUR_STEP_MS } from "./simStore";
import { useSimStore } from "./useSimStore";
import { SCENARIO_SCRIPTS, getScript } from "./scenarioPlayer";
import "./sim.css";

const SPEEDS = [0.5, 1, 2, 4, 8];
// Incident kinds that generate Traffic-Police Reports (see applyPoliceReport).
const INCIDENT_KINDS = ["WRONG_WAY", "ACCIDENT", "ROAD_BLOCKAGE", "CONGESTION"];
/** Severity per incident kind (drives the alert + report severity badge). */
const INCIDENT_SEVERITY: Record<string, string> = {
  WRONG_WAY: "REPORT_TO_POLICE",
  ACCIDENT: "critical",
  ROAD_BLOCKAGE: "critical",
  CONGESTION: "warning",
};

const fmtSigned = (n: number) => (n > 0 ? `+${n}` : String(n));

function queueColour(q: number): string {
  return q > 24 ? STATUS.critical : q > 12 ? STATUS.warning : STATUS.ok;
}

/** Recompute the highlight set from whatever overrides currently exist. */
function recomputeHighlights() {
  const s = simStore.getState();
  const ids = new Set<string>([...Object.keys(s.gates), ...Object.keys(s.segments)]);
  simStore.setHighlights([...ids]);
}

/** Open the Live Operations dashboard in a NEW browser tab so the operator can
 *  keep the simulator and the dashboard side by side on a demo screen. */
function openDashboard() {
  window.open("/live", "_blank", "noopener");
}

export default function SimulatorPage() {
  const sim = useSimStore();

  // Gate + corridor geometry (ids, lat/lon, segments). These reads are stable —
  // the SimAdapter doesn't touch ids/geometry — so they're safe baselines.
  const gatesQ = useQuery({ queryKey: ["sim", "gates"], queryFn: () => getAdapter().gates() });
  const corridorQ = useQuery({
    queryKey: ["sim", "corridor"],
    queryFn: () => getAdapter().corridor(),
    staleTime: Infinity,
  });

  const gates: Gate[] = gatesQ.data ?? [];
  const corridor: CorridorGeometry | undefined = corridorQ.data;
  const segments = useMemo(() => (corridor?.segments ?? []).slice(0, 10), [corridor]);

  const clock = new Date(sim.clockMs);
  const openIncidents = sim.incidents.filter((i) => i.status === "OPEN").length;
  const resolvedIncidents = sim.incidents.filter((i) => i.status === "RESOLVED").length;
  const activeScript = sim.tour.scenarioId ? getScript(sim.tour.scenarioId) : undefined;
  const activeStep = activeScript?.steps[sim.tour.stepIndex];
  const isLastStep = activeScript ? sim.tour.stepIndex >= activeScript.steps.length - 1 : true;

  return (
    <CalciteShell className="sim-root" style={{ height: "100vh" }}>
      {/* Standalone simulator chrome — its own navigation header with the app
          title on the left and the status / clock / Open-dashboard actions on the
          right. No dashboard nav rail (this route renders outside the Shell). */}
      <CalciteNavigation slot="header">
        <CalciteNavigationLogo
          slot="logo"
          heading="UC-III Live Data Simulator"
          description="Drive the dashboard in real time"
          icon="play"
        />
        <div slot="content-end" className="sim-header-actions">
          <span className={`sim-status ${sim.running ? "is-running" : ""}`}>
            <span
              className={`sim-dot ${sim.running ? "sim-dot--running" : ""}`}
              style={{ marginInlineEnd: 0, background: sim.running ? STATUS.ok : "#9aa4b2" }}
            />
            {sim.running ? "RUNNING" : "PAUSED"}
          </span>
          <span className="sim-clock">
            <CalciteIcon icon="clock" scale="s" />
            {clock.toLocaleTimeString()}
          </span>
          <CalciteButton appearance="solid" kind="brand" iconStart="launch" scale="s" onClick={openDashboard}>
            Open dashboard
          </CalciteButton>
        </div>
      </CalciteNavigation>

      <CalcitePanel heading="Simulation controls" style={{ overflow: "auto" }}>
        <div className="sim-canvas">
        <CalciteNotice open icon="lightning" kind="brand" scale="s">
          <div slot="message">
            Changes here stream live to the dashboard — open Live Operations in another tab
            (“Open dashboard”). Press play to let metrics auto-advance, or set values manually.
            Whatever you drive is highlighted and labelled on the map.
          </div>
        </CalciteNotice>

        {/* ---- Master clock ---- */}
        <CalciteBlock open iconStart="clock" heading="Clock & playback" description="Master tick engine">
          <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
            <CalciteButton
              iconStart={sim.running ? "pause" : "play"}
              kind={sim.running ? "danger" : "brand"}
              onClick={() => simStore.setRunning(!sim.running)}
            >
              {sim.running ? "Pause" : "Play"}
            </CalciteButton>
            <CalciteButton appearance="outline" iconStart="reset" onClick={() => simStore.reset()}>
              Reset all
            </CalciteButton>
            <CalciteLabel layout="inline" style={{ marginInlineStart: 16, marginBlock: 0 }}>
              Speed
              <CalciteSegmentedControl
                onCalciteSegmentedControlChange={(e) =>
                  simStore.setSpeed(Number((e.target as unknown as { value: string }).value))
                }
              >
                {SPEEDS.map((sp) => (
                  <CalciteSegmentedControlItem key={sp} value={String(sp)} checked={sim.speed === sp}>
                    {sp}×
                  </CalciteSegmentedControlItem>
                ))}
              </CalciteSegmentedControl>
            </CalciteLabel>
          </div>
        </CalciteBlock>

        {/* ---- Guided scenarios ---- */}
        <CalciteBlock open iconStart="play" heading="Guided scenarios" description="Timed What-If playback">
          <div className="sim-scenario-grid">
            {SCENARIO_SCRIPTS.map((sc) => {
              const active = sim.tour.scenarioId === sc.id;
              return (
                <div key={sc.id} className={`sim-scenario-card ${active ? "is-active" : ""}`}>
                  <span className="sim-scenario-title">{sc.title}</span>
                  <p className="sim-scenario-blurb">{sc.blurb}</p>
                  <CalciteButton
                    scale="s"
                    width="full"
                    kind={active ? "danger" : "brand"}
                    iconStart={active ? "x" : "play"}
                    onClick={() => (active ? simStore.stopScenario() : simStore.startScenario(sc.id))}
                  >
                    {active ? "Stop scenario" : "Start scenario"}
                  </CalciteButton>
                </div>
              );
            })}
          </div>

          {activeScript && activeStep && (
            <div className="sim-step">
              <div className="sim-step-head">
                <span className="sim-step-title">
                  Step {sim.tour.stepIndex + 1}/{activeScript.steps.length}: {activeStep.title}
                </span>
                <CalciteChip scale="s" value="view" icon="map-pin">
                  {activeStep.view}
                </CalciteChip>
              </div>
              <p className="sim-step-explain">{activeStep.explain}</p>
              {activeStep.action && (
                <CalciteChip scale="s" kind="brand" icon="lightning" value="action">
                  {activeStep.action.kind}: {activeStep.action.detail}
                </CalciteChip>
              )}
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 12, flexWrap: "wrap" }}>
                <CalciteButton
                  scale="s"
                  appearance="outline"
                  iconStart="chevron-left"
                  disabled={sim.tour.stepIndex === 0 || undefined}
                  onClick={() => simStore.prevStep()}
                >
                  Prev
                </CalciteButton>
                <CalciteButton
                  scale="s"
                  appearance="outline"
                  iconEnd="chevron-right"
                  disabled={isLastStep || undefined}
                  onClick={() => simStore.nextStep()}
                >
                  Next
                </CalciteButton>
                <CalciteButton
                  scale="s"
                  appearance={sim.tour.autoAdvance ? "solid" : "outline"}
                  kind="neutral"
                  iconStart={sim.tour.autoAdvance ? "pause" : "play"}
                  onClick={() => simStore.setTourAutoAdvance(!sim.tour.autoAdvance)}
                >
                  {sim.tour.autoAdvance ? "Auto-advance: on" : "Auto-advance: off"}
                </CalciteButton>
              </div>
              {sim.tour.autoAdvance && !isLastStep && (
                <div className="sim-progress">
                  {/* key on stepStartedAt so the bar restarts each step */}
                  <span
                    key={sim.tour.stepStartedAt}
                    style={{ animationDuration: `${TOUR_STEP_MS}ms` }}
                  />
                </div>
              )}
            </div>
          )}
        </CalciteBlock>

        {/* ---- Gates ---- */}
        <CalciteBlock open iconStart="car" heading="Gate queues" description="Trucks queued per gate">
          {gates.length === 0 ? (
            <p className="sim-meta">Loading gates…</p>
          ) : (
            <div>
              {gates.map((g) => {
                const val = sim.gates[g.id]?.queueLength ?? 0;
                return (
                  <div key={g.id} className="sim-row" style={{ gridTemplateColumns: "150px 1fr 52px" }}>
                    <span className="sim-row-label">{g.id.replace("G-", "")}</span>
                    <CalciteSlider
                      min={0}
                      max={40}
                      value={val}
                      labelHandles
                      onCalciteSliderInput={(e) => {
                        const q = Number((e.target as unknown as { value: number }).value);
                        // Returning the slider to 0 clears the override so the
                        // gate goes back to baseline (no lingering "• 0" label).
                        if (q === 0) simStore.clearGate(g.id);
                        else
                          simStore.setGate(g.id, {
                            queueLength: q,
                            utilisation: Math.min(1.3, q / 30),
                            lat: g.lat,
                            lon: g.lon,
                          });
                        recomputeHighlights();
                      }}
                    />
                    <CalciteChip value={String(val)} style={{ ["--calcite-chip-text-color" as never]: queueColour(val) }}>
                      {val}
                    </CalciteChip>
                  </div>
                );
              })}
            </div>
          )}
        </CalciteBlock>

        {/* ---- Congestion ---- */}
        <CalciteBlock open iconStart="road-sign" heading="Corridor congestion" description="Jam factor per segment (0–10)">
          {segments.length === 0 ? (
            <p className="sim-meta">Loading corridor…</p>
          ) : (
            <div>
              {segments.map((seg) => {
                const val = sim.segments[seg.id]?.jamFactor ?? 0;
                return (
                  <div key={seg.id} className="sim-row" style={{ gridTemplateColumns: "120px 1fr 52px" }}>
                    <span className="sim-row-label">{seg.id}</span>
                    <CalciteSlider
                      min={0}
                      max={10}
                      step={0.5}
                      value={val}
                      labelHandles
                      onCalciteSliderInput={(e) => {
                        const jam = Number((e.target as unknown as { value: number }).value);
                        if (jam === 0) simStore.clearSegment(seg.id);
                        else simStore.setSegment(seg.id, { jamFactor: jam, speedKmh: Math.max(4, 40 - jam * 3.6) });
                        recomputeHighlights();
                      }}
                    />
                    <span className="sim-value" style={{ color: queueColour(val * 3) }}>
                      {val.toFixed(1)}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </CalciteBlock>

        {/* ---- Flow + injection ---- */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
          <CalciteBlock open iconStart="graph-time-series" heading="Vehicle flow" description="Throughput multiplier">
            <div className="sim-row" style={{ gridTemplateColumns: "1fr 56px" }}>
              <CalciteSlider
                min={0}
                max={3}
                step={0.1}
                value={sim.flowRate}
                labelHandles
                ticks={1}
                onCalciteSliderInput={(e) =>
                  simStore.setFlowRate(Number((e.target as unknown as { value: number }).value))
                }
              />
              <CalciteChip value="rate">{sim.flowRate.toFixed(1)}×</CalciteChip>
            </div>
          </CalciteBlock>
          <CalciteBlock open iconStart="plus" heading="Vehicle injection" description="Extra trucks on the corridor">
            <div className="sim-row" style={{ gridTemplateColumns: "1fr 56px" }}>
              <CalciteSlider
                min={0}
                max={200}
                step={10}
                value={sim.vehicleInjection}
                labelHandles
                onCalciteSliderInput={(e) =>
                  simStore.setVehicleInjection(Number((e.target as unknown as { value: number }).value))
                }
              />
              <span className="sim-value">{sim.vehicleInjection}</span>
            </div>
          </CalciteBlock>
        </div>

        {/* ---- Scan + parking ---- */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
          <CalciteBlock open iconStart="security" heading="Customs scan" description="Pending scans in the queue">
            {sim.scanQueue != null && (
              <div style={{ marginBottom: 8 }}>
                <CalciteButton scale="s" appearance="transparent" iconStart="x" onClick={() => simStore.setScanQueue(null)}>
                  Clear override
                </CalciteButton>
              </div>
            )}
            <div className="sim-row" style={{ gridTemplateColumns: "1fr 52px" }}>
              <CalciteSlider
                min={0}
                max={80}
                value={sim.scanQueue ?? 0}
                labelHandles
                onCalciteSliderInput={(e) =>
                  simStore.setScanQueue(Number((e.target as unknown as { value: number }).value))
                }
              />
              <span className="sim-value">{sim.scanQueue ?? "—"}</span>
            </div>
          </CalciteBlock>
          <CalciteBlock open iconStart="grid" heading="Parking pool" description="Availability delta (± slots)">
            <div className="sim-row" style={{ gridTemplateColumns: "1fr 56px" }}>
              <CalciteSlider
                min={-200}
                max={200}
                step={10}
                value={sim.parkingDelta}
                labelHandles
                onCalciteSliderInput={(e) =>
                  simStore.setParkingDelta(Number((e.target as unknown as { value: number }).value))
                }
              />
              <span className="sim-value">{fmtSigned(sim.parkingDelta)}</span>
            </div>
          </CalciteBlock>
        </div>

        {/* ---- Incident injection ---- */}
        <CalciteBlock
          open
          iconStart="exclamation-mark-triangle"
          heading="Incident injection"
          description="Raise alerts → Traffic-Police Reports (clearing resolves the report)"
        >
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            {INCIDENT_KINDS.map((kind) => (
              <CalciteButton
                key={kind}
                scale="s"
                appearance="outline"
                kind="danger"
                iconStart="exclamation-mark-triangle"
                onClick={() => simStore.injectIncident(kind, INCIDENT_SEVERITY[kind] ?? "warning", gates[0]?.id)}
              >
                {kind.replace(/_/g, " ")}
              </CalciteButton>
            ))}
            {openIncidents > 0 && (
              <CalciteChip scale="s" kind="brand" value="open" icon="exclamation-mark-triangle">
                {openIncidents} active
              </CalciteChip>
            )}
            {resolvedIncidents > 0 && (
              <CalciteChip scale="s" kind="neutral" value="resolved" icon="check-circle">
                {resolvedIncidents} resolved
              </CalciteChip>
            )}
            {openIncidents > 0 && (
              <CalciteButton scale="s" appearance="transparent" iconStart="check" onClick={() => simStore.clearIncidents()}>
                Resolve all
              </CalciteButton>
            )}
          </div>
        </CalciteBlock>

        <p className="sim-meta">
          Gates: {gates.length} · segments: {segments.length} · highlighted assets: {sim.highlights.length}
        </p>
        </div>
      </CalcitePanel>
    </CalciteShell>
  );
}
