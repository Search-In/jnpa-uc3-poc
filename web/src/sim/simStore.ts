// simStore — a tiny framework-agnostic pub/sub store that drives "live" data
// into the otherwise-quiet dashboard. The Simulator page (a separate route,
// often a separate browser tab/window on a demo screen) writes to it; the
// dashboard screens and the ArcGIS map subscribe (via the SimAdapter wrap +
// React Query) and re-render in real time.
//
// Cross-tab sync uses BroadcastChannel (live push between open tabs) backed by
// localStorage (so a freshly-opened dashboard tab hydrates the current state).
// The store holds *overrides* — deltas keyed by asset id — that are merged over
// the adapter's base data by applySim() (see applySim.ts). It never mutates the
// adapter, so determinism is preserved and "reset" returns to baseline.
//
// A built-in tick engine auto-advances the overrides while running, scaled by
// `speed`, so the board feels like a live system. Manual controls on the
// Simulator page set target values that the tick eases toward.
//
// Ported from jnpa_poc_2 apps/web/src/sim/simStore.ts, adapted to UC-III traffic
// levers (gate truck queues, corridor congestion, vehicle flow, incidents).

import { getScript, type StepPatch } from "./scenarioPlayer";

export type Faction = "gates" | "segments" | "flow" | "vehicles" | "scan" | "parking" | "incidents";

/** Per-gate live override. Optional lat/lon let applySim place injected
 *  queue-trucks on the map at the gate; the Simulator page fills them from the
 *  real gates() geometry when it drives a slider. */
export interface GateOverride {
  queueLength?: number;
  utilisation?: number;
  throughput60min?: number;
  lat?: number;
  lon?: number;
}

/** Per-corridor-segment congestion override. */
export interface SegmentOverride {
  jamFactor?: number;
  speedKmh?: number;
}

/** An injected incident, surfaced through the alerts feed AND the Traffic-Police
 *  Reports module. `status` lets a cleared incident transition to RESOLVED in the
 *  report (and drop out of the active-alerts feed) instead of vanishing. */
export interface InjectedIncident {
  id: string;
  kind: string;
  severity: string;
  gate_id: string | null;
  segment_id: string | null;
  ts: string;
  /** OPEN = live/active; RESOLVED = cleared (kept for the report record). */
  status: "OPEN" | "RESOLVED";
  /** Guided-scenario id that raised it, when applicable. */
  scenario: string | null;
}

/**
 * Guided What-If tour state. When a scenario is playing, `scenarioId` is set and
 * `stepIndex` points at the current storyline step; `autoAdvance` drives the
 * step-by-step playback on a timer. The dashboard reads this to show the
 * coach-mark tour, spotlight the right view, and badge the running scenario.
 */
export interface TourState {
  scenarioId: string | null;
  stepIndex: number;
  autoAdvance: boolean;
  /** Bumped each time a step changes, so the UI can show a progress bar. */
  stepStartedAt: number;
}

export interface SimState {
  /** Whether the tick engine is advancing values. */
  running: boolean;
  /** Playback speed multiplier (0.5×–8×). */
  speed: number;
  /** Sim clock in epoch ms; advances ~ speed × wall time while running. */
  clockMs: number;
  /** Monotonic tick counter. */
  tick: number;
  /** Per-gate overrides keyed by gateId. */
  gates: Record<string, GateOverride>;
  /** Per-segment congestion overrides keyed by segmentId. */
  segments: Record<string, SegmentOverride>;
  /** Global vehicle-flow throughput multiplier (1 = baseline). */
  flowRate: number;
  /** Extra synthetic trucks injected onto the corridor (absolute count). */
  vehicleInjection: number;
  /** Customs scan queue depth override (absolute), or null when disengaged. */
  scanQueue: number | null;
  /** Parking / empty-pool availability delta (signed). */
  parkingDelta: number;
  /** Incidents the operator (or a scenario) has injected. */
  incidents: InjectedIncident[];
  /** Asset ids the operator is actively driving — highlighted on the map. */
  highlights: string[];
  /** The single asset most-recently changed — the dashboard map zooms to it and
   *  animates a focus pulse on it, so the operator sees exactly what they drove.
   *  Bumped (with `lastTouchedNonce`) on every change so repeat edits re-fire. */
  lastTouched: string | null;
  lastTouchedNonce: number;
  /** Guided What-If scenario tour (null scenarioId = no tour). */
  tour: TourState;
}

const STORAGE_KEY = "jnpa.uc3.sim.state.v1";
const CHANNEL = "jnpa-uc3-sim";
const TICK_MS = 1000;
/** How long each guided scenario step stays on screen before auto-advancing. */
const TOUR_STEP_MS = 6500;

function baseState(): SimState {
  return {
    running: false,
    speed: 1,
    clockMs: Date.UTC(2026, 5, 27, 9, 0, 0),
    tick: 0,
    gates: {},
    segments: {},
    flowRate: 1,
    vehicleInjection: 0,
    scanQueue: null,
    parkingDelta: 0,
    incidents: [],
    highlights: [],
    lastTouched: null,
    lastTouchedNonce: 0,
    tour: { scenarioId: null, stepIndex: 0, autoAdvance: true, stepStartedAt: 0 },
  };
}

type Listener = () => void;

class SimStore {
  private state: SimState = baseState();
  private listeners = new Set<Listener>();
  private channel: BroadcastChannel | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  /** Auto-advance timer for the guided scenario tour. */
  private tourTimer: ReturnType<typeof setTimeout> | null = null;
  /** Monotonic stamp source (avoids Date.now in the store for determinism). */
  private stamp = 0;
  /** Set when an update arrives from another tab, to avoid echo loops. */
  private applyingRemote = false;

  constructor() {
    // Hydrate from localStorage so a newly-opened tab sees current sim state.
    try {
      const raw = typeof localStorage !== "undefined" ? localStorage.getItem(STORAGE_KEY) : null;
      if (raw) this.state = { ...baseState(), ...(JSON.parse(raw) as Partial<SimState>) };
    } catch {
      /* ignore corrupt storage */
    }

    if (typeof BroadcastChannel !== "undefined") {
      this.channel = new BroadcastChannel(CHANNEL);
      this.channel.onmessage = (e: MessageEvent) => {
        if (e.data?.type === "state") {
          this.applyingRemote = true;
          this.state = e.data.state as SimState;
          this.applyingRemote = false;
          this.emit(/* broadcast */ false);
          this.syncTimer();
          this.armTourTimer();
        }
      };
    }

    // The tab that owns the running flag runs the tick. Any tab can own it;
    // whichever last toggled `running` keeps the clock for everyone via sync.
    this.syncTimer();
  }

  getState = (): SimState => this.state;

  subscribe = (fn: Listener): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  /** Replace state via a producer, then notify + broadcast + persist. */
  set = (producer: (s: SimState) => SimState): void => {
    this.state = producer(this.state);
    this.emit(true);
    this.syncTimer();
  };

  // ---- high-level actions used by the Simulator page ----

  setRunning = (running: boolean) => this.set((s) => ({ ...s, running }));
  setSpeed = (speed: number) => this.set((s) => ({ ...s, speed }));

  setGate = (gateId: string, patch: GateOverride) =>
    this.set((s) => ({
      ...s,
      gates: { ...s.gates, [gateId]: { ...s.gates[gateId], ...patch } },
      lastTouched: gateId,
      lastTouchedNonce: ++this.stamp,
    }));

  setSegment = (segmentId: string, patch: SegmentOverride) =>
    this.set((s) => ({
      ...s,
      segments: { ...s.segments, [segmentId]: { ...s.segments[segmentId], ...patch } },
      lastTouched: segmentId,
      lastTouchedNonce: ++this.stamp,
    }));

  /** Remove a gate override entirely (slider returned to 0 → back to baseline,
   *  and the map drops its highlight + "• N" label rather than pinning it to 0). */
  clearGate = (gateId: string) =>
    this.set((s) => {
      if (!s.gates[gateId]) return s;
      const gates = { ...s.gates };
      delete gates[gateId];
      return { ...s, gates, lastTouched: gateId, lastTouchedNonce: ++this.stamp };
    });

  /** Remove a segment congestion override entirely (jam returned to 0). */
  clearSegment = (segmentId: string) =>
    this.set((s) => {
      if (!s.segments[segmentId]) return s;
      const segments = { ...s.segments };
      delete segments[segmentId];
      return { ...s, segments, lastTouched: segmentId, lastTouchedNonce: ++this.stamp };
    });

  setFlowRate = (flowRate: number) => this.set((s) => ({ ...s, flowRate }));
  setVehicleInjection = (vehicleInjection: number) => this.set((s) => ({ ...s, vehicleInjection }));
  setScanQueue = (scanQueue: number | null) => this.set((s) => ({ ...s, scanQueue }));
  setParkingDelta = (parkingDelta: number) => this.set((s) => ({ ...s, parkingDelta }));

  /** Inject a one-off incident (manual button on the Simulator page). */
  injectIncident = (kind: string, severity: string, gate_id?: string, segment_id?: string) =>
    this.set((s) => ({
      ...s,
      incidents: [
        this.makeIncident(kind, severity, gate_id ?? null, segment_id ?? null),
        ...s.incidents,
      ].slice(0, 25),
      lastTouched: gate_id ?? segment_id ?? s.lastTouched,
      lastTouchedNonce: ++this.stamp,
    }));

  /** Clear = transition every OPEN incident to RESOLVED (it stays in the police
   *  report as a closed record and drops out of the active-alerts feed) rather
   *  than deleting it, so reports never lose data inconsistently. */
  clearIncidents = () =>
    this.set((s) => ({
      ...s,
      incidents: s.incidents.map((i) => (i.status === "OPEN" ? { ...i, status: "RESOLVED" } : i)),
      lastTouchedNonce: ++this.stamp,
    }));

  /** Mark assets as actively-driven so the map highlights them. */
  setHighlights = (highlights: string[]) => this.set((s) => ({ ...s, highlights }));

  /** Clear all overrides back to baseline. */
  reset = () => this.set(() => baseState());

  // ---- guided What-If scenario tour ----

  /**
   * Start a scenario tour: clear prior overrides, apply step 0, spotlight its
   * assets, and (if autoAdvance) arm the step timer. The board animates live as
   * each step's patch lands; the coach-mark overlay reads `tour` to narrate.
   */
  startScenario = (scenarioId: string, autoAdvance = true) => {
    const script = getScript(scenarioId);
    if (!script || script.steps.length === 0) return;
    this.set((s) => {
      const fresh = baseState();
      // Keep the clock/speed the operator already set; reset only the overrides.
      const seeded: SimState = {
        ...fresh,
        running: s.running,
        speed: s.speed,
        clockMs: s.clockMs,
        tick: s.tick,
        tour: { scenarioId, stepIndex: 0, autoAdvance, stepStartedAt: ++this.stamp },
      };
      return this.applyStep(seeded, scenarioId, 0);
    });
    this.armTourTimer();
  };

  /** Jump to a specific step (used by the prev/next buttons & progress dots). */
  gotoStep = (index: number) => {
    this.set((s) => {
      const id = s.tour.scenarioId;
      const script = id ? getScript(id) : undefined;
      if (!script) return s;
      const i = Math.max(0, Math.min(script.steps.length - 1, index));
      const stepped: SimState = {
        ...s,
        tour: { ...s.tour, stepIndex: i, stepStartedAt: ++this.stamp },
      };
      return this.applyStep(stepped, script.id, i);
    });
    this.armTourTimer();
  };

  nextStep = () => this.gotoStep(this.state.tour.stepIndex + 1);
  prevStep = () => this.gotoStep(this.state.tour.stepIndex - 1);

  /** Toggle auto-advance without changing the current step. */
  setTourAutoAdvance = (autoAdvance: boolean) => {
    this.set((s) =>
      s.tour.scenarioId
        ? { ...s, tour: { ...s.tour, autoAdvance, stepStartedAt: ++this.stamp } }
        : s,
    );
    this.armTourTimer();
  };

  /** End the tour and clear every override the scenario applied. */
  stopScenario = () => {
    this.clearTourTimer();
    this.set((s) => ({
      ...baseState(),
      running: s.running,
      speed: s.speed,
      clockMs: s.clockMs,
      tick: s.tick,
    }));
  };

  // ---- internals ----

  private makeIncident(
    kind: string,
    severity: string,
    gate_id: string | null,
    segment_id: string | null,
  ): InjectedIncident {
    const id = `SIM-INC-${kind}-${gate_id ?? segment_id ?? "x"}-${++this.stamp}`;
    return {
      id,
      kind,
      severity,
      gate_id,
      segment_id,
      ts: new Date(this.state.clockMs).toISOString(),
      status: "OPEN",
      scenario: this.state.tour.scenarioId,
    };
  }

  /** Merge a scenario step's patch into the override fields of the sim state. */
  private mergePatch(s: SimState, patch: StepPatch): SimState {
    const next: SimState = { ...s };
    if (patch.gates) {
      next.gates = { ...s.gates };
      for (const [id, g] of Object.entries(patch.gates)) {
        next.gates[id] = { ...next.gates[id], ...g };
      }
    }
    if (patch.segments) {
      next.segments = { ...s.segments };
      for (const [id, sg] of Object.entries(patch.segments)) {
        next.segments[id] = { ...next.segments[id], ...sg };
      }
    }
    if (patch.flowRate != null) next.flowRate = patch.flowRate;
    if (patch.vehicleInjection != null) next.vehicleInjection = patch.vehicleInjection;
    if (patch.scanQueue !== undefined) next.scanQueue = patch.scanQueue;
    if (patch.parkingDelta != null) next.parkingDelta = patch.parkingDelta;
    if (patch.incidents) {
      next.incidents = patch.incidents.map((i) =>
        this.makeIncident(i.kind, i.severity, i.gate_id ?? null, i.segment_id ?? null),
      );
    }
    return next;
  }

  /**
   * Compose the cumulative effect of all steps up to and including `index` so a
   * jump-back leaves the board exactly where that step's narrative says it is
   * (steps are a running storyline, later patches superseding earlier ones).
   * Also sets the map spotlight to the current step's assets.
   */
  private applyStep(s: SimState, scenarioId: string, index: number): SimState {
    const script = getScript(scenarioId);
    if (!script) return s;
    // Start from a clean override surface, replay patches 0..index in order.
    let acc: SimState = {
      ...s,
      gates: {},
      segments: {},
      flowRate: 1,
      vehicleInjection: 0,
      scanQueue: null,
      parkingDelta: 0,
      incidents: [],
    };
    for (let i = 0; i <= index; i++) {
      const step = script.steps[i];
      if (step) acc = this.mergePatch(acc, step.patch);
    }
    const step = script.steps[index];
    acc.highlights = step ? [...step.spotlight] : [];
    // Focus + pulse the first spotlighted asset of the step so the map zooms to
    // whatever the scenario is currently acting upon.
    acc.lastTouched = step?.spotlight[0] ?? null;
    acc.lastTouchedNonce = ++this.stamp;
    return acc;
  }

  private armTourTimer() {
    this.clearTourTimer();
    const { scenarioId, autoAdvance, stepIndex } = this.state.tour;
    if (!scenarioId || !autoAdvance) return;
    const script = getScript(scenarioId);
    if (!script || stepIndex >= script.steps.length - 1) return; // last step: stop
    this.tourTimer = setTimeout(() => this.nextStep(), TOUR_STEP_MS);
  }

  private clearTourTimer() {
    if (this.tourTimer) {
      clearTimeout(this.tourTimer);
      this.tourTimer = null;
    }
  }

  // ---- tick engine ----

  private syncTimer() {
    const shouldRun = this.state.running;
    if (shouldRun && !this.timer) {
      this.timer = setInterval(() => this.advance(), TICK_MS);
    } else if (!shouldRun && this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  /**
   * One tick: advance the clock and nudge driven metrics by a small,
   * deterministic, speed-scaled amount so the board breathes. Gate queues drift
   * around their set value; segment congestion wobbles; flow rate decays toward
   * 1. Only assets with an existing override are advanced — the sim never
   * invents new driven assets on its own.
   */
  private advance() {
    this.set((s) => {
      const dtMin = (TICK_MS / 60000) * s.speed * 10; // 1s real ≈ speed×10 sim-min
      const gates: Record<string, GateOverride> = {};
      for (const [id, g] of Object.entries(s.gates)) {
        const q = g.queueLength ?? 0;
        const wobble = Math.sin((s.tick + hash(id)) / 6) * 0.6 * s.speed;
        gates[id] = { ...g, queueLength: Math.max(0, Math.round(q + wobble)) };
      }
      const segments: Record<string, SegmentOverride> = {};
      for (const [id, sg] of Object.entries(s.segments)) {
        const jam = sg.jamFactor ?? 0;
        const drift = Math.cos((s.tick + hash(id)) / 8) * 0.3 * s.speed;
        segments[id] = { ...sg, jamFactor: clamp(jam + drift, 0, 10) };
      }
      const scanQueue =
        s.scanQueue == null
          ? null
          : Math.max(0, Math.round(s.scanQueue + Math.sin(s.tick / 5) * 0.8 * s.speed));
      return {
        ...s,
        tick: s.tick + 1,
        clockMs: s.clockMs + dtMin * 60000,
        gates,
        segments,
        scanQueue,
        // flow rate eases back toward baseline so manual spikes relax
        flowRate: ease(s.flowRate, 1, 0.04 * s.speed),
      };
    });
  }

  private emit(broadcast: boolean) {
    this.listeners.forEach((l) => l());
    if (broadcast && !this.applyingRemote) {
      try {
        if (typeof localStorage !== "undefined")
          localStorage.setItem(STORAGE_KEY, JSON.stringify(this.state));
      } catch {
        /* storage may be full / unavailable */
      }
      this.channel?.postMessage({ type: "state", state: this.state });
    }
  }
}

function hash(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

function ease(from: number, to: number, k: number): number {
  return Math.abs(from - to) < 0.01 ? to : from + (to - from) * k;
}

function clamp(x: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, x));
}

/** Singleton store shared by every component in the tab. */
export const simStore = new SimStore();

/** Exposed for the tour progress bar so the UI matches the auto-advance pace. */
export { TOUR_STEP_MS };
