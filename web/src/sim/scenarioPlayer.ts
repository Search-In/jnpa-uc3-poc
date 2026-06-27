// scenarioPlayer — turns each UC-III traffic What-If into a *guided, timed
// playback* that drives the live dashboard instead of a static before/after
// card. Running a scenario:
//   1. seeds simStore levers to a clean baseline for the scenario,
//   2. steps through a short storyline; each step pushes real sim overrides
//      (gate queues, segment congestion, vehicle flow, scan depth, incidents)
//      so every tile, the KPI strip and the map halos animate in real time,
//   3. carries, per step, plain-language coach-mark copy + an anchor (which
//      view/route + which map asset to spotlight) so a viewer can follow *what*
//      is changing and *why*.
//
// The whole thing is deterministic (no Date.now / Math.random) and reversible —
// `simStore.stopScenario()` clears every override back to baseline. This mirrors
// jnpa_poc_2's apps/web/src/sim/scenarioPlayer.ts, adapted from cargo concepts
// (CFS pendency, rail sidings, customs scan) to UC-III traffic concepts (gate
// truck queues, corridor-segment congestion, vehicle flow, incidents).

/** Which dashboard view a step is about (a route the host can navigate to). */
export type ViewId = "/live" | "/advisory" | "/geofencing" | "/reports" | "/health";

/** A single human-readable metric change surfaced in the coach-mark. */
export interface MetricChange {
  /** Short label, e.g. "Gate NSICT queue". */
  label: string;
  /** Value before this step. */
  from: number | string;
  /** Value after this step. */
  to: number | string;
  /** Unit suffix, e.g. "trucks", "min", "vph". */
  unit?: string;
  /** Direction the operator should read this as. */
  tone: "worse" | "better" | "neutral";
}

/** A patch applied to simStore when a step fires (all optional, additive). */
export interface StepPatch {
  /** Per-gate truck-queue / utilisation overrides keyed by gate id. */
  gates?: Record<string, { queueLength?: number; utilisation?: number; throughput60min?: number }>;
  /** Per-corridor-segment congestion overrides keyed by segment id. */
  segments?: Record<string, { jamFactor?: number; speedKmh?: number }>;
  /** Global vehicle-flow throughput multiplier (1 = baseline). */
  flowRate?: number;
  /** Extra synthetic trucks injected onto the corridor. */
  vehicleInjection?: number;
  /** Customs-scan backlog depth (absolute), or null to clear. */
  scanQueue?: number | null;
  /** Parking / empty-pool availability delta (signed). */
  parkingDelta?: number;
  /** Incidents (alerts) this step raises. Replaces the injected-incident set. */
  incidents?: { kind: string; severity: string; gate_id?: string; segment_id?: string }[];
}

/**
 * A precise, value-level highlight target. Resolves to a single DOM node on the
 * dashboard (a KPI card, a gate tile) that the coach-mark rings and tags with
 * the live value — so the viewer sees the *exact* number moving.
 */
export type ValueTarget =
  /** A KPI strip card, by KPI key (matches KpiResult.key / data-kpi). */
  | { kind: "kpi"; key: string }
  /** A gate tile / map asset, by gate id (matches data-asset). */
  | { kind: "asset"; id: string };

export interface ScenarioStep {
  /** Coach-mark title — what's happening, in plain words. */
  title: string;
  /** One or two sentences a non-expert can follow. */
  explain: string;
  /** Which dashboard view to switch to + spotlight for this step. */
  view: ViewId;
  /** Map asset ids to spotlight (gate ids / segment ids). [] = none. */
  spotlight: string[];
  /** Exact dashboard values to ring (KPI cards / gate tiles). */
  valueTargets?: ValueTarget[];
  /** Metric deltas shown as little chips in the coach-mark. */
  metrics: MetricChange[];
  /** Sim overrides this step writes (drives the live board + map). */
  patch: StepPatch;
  /** Optional automated-action tag shown as a badge (the "so the system did X"). */
  action?: { kind: string; detail: string };
}

export interface ScenarioScript {
  id: string;
  /** Title shown on the launcher card + tour header. */
  title: string;
  /** One-line "what this explores" for the launcher card. */
  blurb: string;
  /** Calcite icon for the launcher card. */
  icon: string;
  /** Ordered storyline. */
  steps: ScenarioStep[];
}

// Real UC-III asset ids so the map spotlights land on drawn markers. Gate ids
// match the gates() contract exactly (mock + gateway expose G-NSICT, G-JNPCT,
// G-NSIGT, G-BMCT — there is no G-GTI in UC-III); segment ids match the corridor
// snapshot segments (SEG-NN).
const G_NSICT = "G-NSICT";
const G_ALT = "G-JNPCT"; // alternate gate used for re-routing
const SEG_SPILL = ["SEG-03", "SEG-04", "SEG-05"];
const SEG_SURGE = ["SEG-07", "SEG-08", "SEG-09"];

/**
 * UC-III guided storylines. Numbers are illustrative but directionally faithful
 * to the backend scenarios (tfc1/tfc2/tfc3) — the same KPI levers move. These
 * drive the *frontend* sim layer for a live, animated What-If walk-through; the
 * existing /what-if console still runs the real backend scenarios.
 */
export const SCENARIO_SCRIPTS: ScenarioScript[] = [
  {
    id: "SIM-TFC1",
    title: "Gate Closure & Spillover",
    blurb:
      "A terminal gate closes; trucks pile up and the corridor congests — watch the twin re-route and recover.",
    icon: "exclamation-mark-triangle",
    steps: [
      {
        title: "Gate NSICT is taken out of service",
        explain:
          "The NSICT gate closes for an incident. Inbound trucks have nowhere to go, so the queue at the gate begins to climb.",
        view: "/live",
        spotlight: [G_NSICT],
        valueTargets: [
          { kind: "asset", id: G_NSICT },
          { kind: "kpi", key: "queue_length" },
        ],
        metrics: [{ label: "Gate NSICT queue", from: 8, to: 34, unit: "trucks", tone: "worse" }],
        patch: {
          gates: { [G_NSICT]: { queueLength: 34, utilisation: 1.15 } },
          vehicleInjection: 40,
        },
      },
      {
        title: "Spillover congests the approach corridor",
        explain:
          "The backed-up gate spills onto the approach segments. Speeds drop and the jam factor rises across the spillover corridor.",
        view: "/live",
        spotlight: [...SEG_SPILL, G_NSICT],
        valueTargets: [{ kind: "kpi", key: "avg_dwell" }],
        metrics: [{ label: "Corridor jam factor", from: 2.5, to: 7.5, unit: "/10", tone: "worse" }],
        patch: {
          gates: { [G_NSICT]: { queueLength: 38, utilisation: 1.2 } },
          segments: Object.fromEntries(SEG_SPILL.map((s) => [s, { jamFactor: 7.5, speedKmh: 8 }])),
          vehicleInjection: 60,
        },
      },
      {
        title: "The twin raises an incident & re-routes",
        explain:
          "The digital twin detects the spillover, raises a congestion incident, and pushes a best-alternate-gate advisory to inbound drivers via the UC-III app.",
        view: "/live",
        spotlight: [G_NSICT, G_ALT],
        metrics: [{ label: "Re-routed to", from: "—", to: "Gate JNPCT", tone: "neutral" }],
        patch: {
          gates: {
            [G_NSICT]: { queueLength: 30, utilisation: 1.0 },
            [G_ALT]: { queueLength: 16, utilisation: 0.85 },
          },
          segments: Object.fromEntries(SEG_SPILL.map((s) => [s, { jamFactor: 5.5, speedKmh: 14 }])),
          vehicleInjection: 40,
          incidents: [
            {
              kind: "CONGESTION",
              severity: "critical",
              gate_id: G_NSICT,
              segment_id: SEG_SPILL[0],
            },
          ],
        },
        action: {
          kind: "REROUTE",
          detail: "Best-alt-gate advisory pushed to inbound trucks → Gate JNPCT",
        },
      },
      {
        title: "Queues drain, corridor recovers",
        explain:
          "With trucks diverted and TAS slots rescheduled, the NSICT queue drains and corridor speeds recover. The queue-length KPI eases back toward target.",
        view: "/live",
        spotlight: [G_NSICT, G_ALT],
        valueTargets: [
          { kind: "kpi", key: "queue_length" },
          { kind: "asset", id: G_NSICT },
        ],
        metrics: [{ label: "Gate NSICT queue", from: 30, to: 12, unit: "trucks", tone: "better" }],
        patch: {
          gates: {
            [G_NSICT]: { queueLength: 12, utilisation: 0.7 },
            [G_ALT]: { queueLength: 14, utilisation: 0.8 },
          },
          segments: Object.fromEntries(SEG_SPILL.map((s) => [s, { jamFactor: 3.0, speedKmh: 26 }])),
          vehicleInjection: 10,
        },
        action: {
          kind: "RECOMMENDATION",
          detail: "Reschedule TAS slots to GTI; drain NSICT queue first",
        },
      },
    ],
  },
  {
    id: "SIM-TFC2",
    title: "Wrong-Way Incident",
    blurb:
      "A wrong-way vehicle is detected at a gate exit — the twin raises an alert and issues an e-challan.",
    icon: "security",
    steps: [
      {
        title: "A vehicle enters against the flow",
        explain:
          "A camera at the GTI exit spots a vehicle travelling the wrong way. This is an immediate safety hazard on a busy corridor segment.",
        view: "/live",
        spotlight: [G_ALT, SEG_SURGE[0]],
        metrics: [{ label: "Heading", from: "135°", to: "315° (wrong-way)", tone: "worse" }],
        patch: {
          segments: { [SEG_SURGE[0]]: { jamFactor: 6.0, speedKmh: 12 } },
          incidents: [
            { kind: "WRONG_WAY", severity: "critical", gate_id: G_ALT, segment_id: SEG_SURGE[0] },
          ],
        },
      },
      {
        title: "The twin raises a wrong-way alert",
        explain:
          "The anomaly detector confirms the wrong-way track over several frames and raises a critical alert to the traffic-police console — no one had to be watching the feed.",
        view: "/reports",
        spotlight: [G_ALT],
        metrics: [{ label: "Alert", from: "—", to: "WRONG_WAY → POLICE", tone: "worse" }],
        patch: {
          segments: { [SEG_SURGE[0]]: { jamFactor: 6.0, speedKmh: 12 } },
          incidents: [
            {
              kind: "WRONG_WAY",
              severity: "REPORT_TO_POLICE",
              gate_id: G_ALT,
              segment_id: SEG_SURGE[0],
            },
          ],
        },
        action: {
          kind: "NOTIFICATION",
          detail: "Critical WRONG_WAY alert escalated to TRAFFIC_POLICE",
        },
      },
      {
        title: "An e-challan is issued",
        explain:
          "The plate is resolved through the Vahan fallback chain and an e-challan is generated automatically, with the camera clip attached as evidence.",
        view: "/reports",
        spotlight: [G_ALT],
        metrics: [{ label: "e-Challan", from: "—", to: "ECH issued (MVA s.184)", tone: "neutral" }],
        patch: {
          segments: { [SEG_SURGE[0]]: { jamFactor: 3.5, speedKmh: 22 } },
          incidents: [
            { kind: "WRONG_WAY", severity: "warning", gate_id: G_ALT, segment_id: SEG_SURGE[0] },
          ],
        },
        action: {
          kind: "E_CHALLAN",
          detail: "e-Challan issued via Vahan chain; evidence clip attached",
        },
      },
    ],
  },
  {
    id: "SIM-TFC3",
    title: "DPD Release Surge",
    blurb:
      "A spike in DPD container releases floods the corridor with trucks — the twin forecasts the build-up and reissues slot windows.",
    icon: "graph-time-series",
    steps: [
      {
        title: "A surge of releases hits the corridor",
        explain:
          "A direct-port-delivery release spike (2.5×) means many more trucks head to the port at once. The corridor vehicle flow jumps well above baseline.",
        view: "/live",
        spotlight: SEG_SURGE,
        valueTargets: [{ kind: "kpi", key: "gate_throughput" }],
        metrics: [{ label: "Vehicle flow", from: "1.0×", to: "2.2×", unit: "", tone: "worse" }],
        patch: { flowRate: 2.2, vehicleInjection: 80 },
      },
      {
        title: "Congestion builds across the surge segments",
        explain:
          "The extra trucks build up on the inbound segments. The forecaster predicts the jam will worsen over the next 30 minutes if nothing changes.",
        view: "/live",
        spotlight: SEG_SURGE,
        valueTargets: [{ kind: "kpi", key: "tat_inside_port" }],
        metrics: [{ label: "Predicted jam", from: "—", to: "rising 30 min", tone: "worse" }],
        patch: {
          flowRate: 2.2,
          vehicleInjection: 120,
          segments: Object.fromEntries(SEG_SURGE.map((s) => [s, { jamFactor: 6.5, speedKmh: 16 }])),
          gates: { [G_NSICT]: { queueLength: 26, utilisation: 1.05 } },
        },
      },
      {
        title: "Driver-advisory reissues slot windows",
        explain:
          "The twin reissues gate-slot windows to spread the arrivals, telling some drivers to come later. Flow normalises and the segments breathe again.",
        view: "/advisory",
        spotlight: SEG_SURGE,
        valueTargets: [{ kind: "kpi", key: "gate_throughput" }],
        metrics: [{ label: "Vehicle flow", from: "2.2×", to: "1.3×", unit: "", tone: "better" }],
        patch: {
          flowRate: 1.3,
          vehicleInjection: 30,
          segments: Object.fromEntries(SEG_SURGE.map((s) => [s, { jamFactor: 3.5, speedKmh: 24 }])),
          gates: { [G_NSICT]: { queueLength: 14, utilisation: 0.82 } },
        },
        action: {
          kind: "CROSS_TWIN_PUSH",
          detail: "Gate-slot windows reissued to the UC-III driver app",
        },
      },
    ],
  },
];

export function getScript(id: string): ScenarioScript | undefined {
  return SCENARIO_SCRIPTS.find((s) => s.id === id);
}
