// UC-3 Reactive Guide — explainable-AI causal chains (Cause → Impact → Action →
// Expected outcome). Adapts the UC-2 twin's causalGraph approach
// (apps/web/src/whatif/causalGraph.ts) into a compact, self-contained UC-3
// model with NO external deps.
//
// Each chain answers the evaluator's question "why did congestion happen and
// what does the system do about it?" for one reactive scenario. Chains are
// keyed by the What-If scenario ids (TFC-1/2/3), plus two standalone teaching
// chains (heavy-vehicle surge — the spec example — and Monsoon Friday).
//
// Integrity: every magnitude flagged `simulated: true` is a shadow-run figure
// under the stated assumptions, anchored to the documented KPI baselines in
// shared/jnpa_shared/kpi.py — never a claimed live JNPA measurement.

export type StageKind = "cause" | "impact" | "action" | "outcome";

export interface CausalMagnitude {
  from: string;
  to: string;
  unit?: string;
  /** True when this is a simulated propagation figure, not a live measurement. */
  simulated?: boolean;
}

export interface CausalStage {
  kind: StageKind;
  label: string;
  /** The mechanism / "why" sentence on this step. */
  mechanism: string;
  magnitude?: CausalMagnitude;
  /** KPI this stage touches (matches KPI_TARGETS keys where applicable). */
  kpi?: string;
  /** Corridor segment / gate / camera id for an optional map highlight. */
  where?: string;
}

export interface CausalChain {
  id: string;
  title: string;
  summary: string;
  stages: CausalStage[]; // always ordered cause → impact → action → outcome
}

export const CAUSAL_CHAINS: Record<string, CausalChain> = {
  "HEAVY-VEHICLE-SURGE": {
    id: "HEAVY-VEHICLE-SURGE",
    title: "Heavy-vehicle surge",
    summary: "The spec's worked example: an HGV burst at the gates and how the twin recovers ETA.",
    stages: [
      {
        kind: "cause",
        label: "Heavy vehicle (HGV) surge on the NH-348 approach",
        mechanism: "A burst of HGVs arrives at the port gates above the corridor baseline.",
        where: "SEG-11",
      },
      {
        kind: "impact",
        label: "Gate queue length & wait time increase",
        mechanism: "HGV service time exceeds a car's, so the inbound queue builds at the gates.",
        magnitude: { from: "25", to: "41", unit: "veh", simulated: true },
        kpi: "queue_length",
        where: "SEG-11",
      },
      {
        kind: "action",
        label: "Dynamic lane allocation",
        mechanism: "Open an additional inbound lane and rebalance the lane mix / gate load for HGVs.",
        kpi: "gate_throughput",
      },
      {
        kind: "outcome",
        label: "ETA recovery",
        mechanism: "Service rate rises to match arrivals; the queue drains and ETA returns toward plan.",
        magnitude: { from: "14.5", to: "8.2", unit: "min wait", simulated: true },
        kpi: "gate_queue_wait",
      },
    ],
  },

  "TFC-1": {
    id: "TFC-1",
    title: "Gate closure (TFC-1)",
    summary: "A terminal gate goes offline at peak; the twin reroutes to hold ETA.",
    stages: [
      {
        kind: "cause",
        label: "Gate G-NSICT closed for 120 min",
        mechanism: "A terminal gate goes offline during peak inbound flow.",
        where: "G-NSICT",
      },
      {
        kind: "impact",
        label: "Spillover congestion on feeding segments",
        mechanism: "Inbound trucks pool on the approach; the forecaster raises P(congested) past 0.7.",
        magnitude: { from: "0.20", to: "0.74", unit: "P(onset)", simulated: true },
        kpi: "congestion_onset",
        where: "SEG-10",
      },
      {
        kind: "action",
        label: "Auto-reroute to best alternate gate + TAS reslot",
        mechanism: "/api/routing/best_alt_gate reassigns inbound trucks; TAS slots move to RESCHEDULED.",
        kpi: "queue_length",
      },
      {
        kind: "outcome",
        label: "ETA held, queue drained",
        mechanism: "Load redistributes to a gate with spare capacity; queue wait returns toward target.",
        magnitude: { from: "14.5", to: "8.0", unit: "min wait", simulated: true },
        kpi: "gate_queue_wait",
      },
    ],
  },

  "TFC-2": {
    id: "TFC-2",
    title: "Wrong-way vehicle (TFC-2)",
    summary: "A safety anomaly on the exit corridor and the enforcement response.",
    stages: [
      {
        kind: "cause",
        label: "Wrong-way vehicle detected on the exit corridor",
        mechanism: "A vehicle travels against the lane direction near the Karal exit.",
        where: "C-KARAL-EXIT",
      },
      {
        kind: "impact",
        label: "WRONG_WAY anomaly + collision risk",
        mechanism: "ByteTrack flags a reverse heading versus the lane vector; the autoencoder scores it anomalous.",
        kpi: "safety",
        where: "C-KARAL-EXIT",
      },
      {
        kind: "action",
        label: "e-Challan issued + operator banner + evidence clip",
        mechanism: "Auto-LEO issues a challan stub, persists an MP4 evidence clip, and raises a WS operator banner.",
      },
      {
        kind: "outcome",
        label: "Violation deterred, corridor cleared",
        mechanism: "The advisory is pushed and the incident is logged for enforcement follow-up.",
      },
    ],
  },

  "TFC-3": {
    id: "TFC-3",
    title: "Cross-twin DPD surge (TFC-3)",
    summary: "A UC-II release spike propagates into UC-III truck demand and the pre-emptive response.",
    stages: [
      {
        kind: "cause",
        label: "UC-II DPD release spike (2.5×)",
        mechanism: "Direct Port Delivery volumes surge upstream, published on cargo.dpd_release.",
      },
      {
        kind: "impact",
        label: "Upstream truck demand rises",
        mechanism: "uc2_bridge.translate_release maps the spike into a corridor demand profile over ~40 min.",
        magnitude: { from: "240", to: "600", unit: "trucks/h", simulated: true },
        kpi: "truck_demand",
      },
      {
        kind: "action",
        label: "Pre-emptive gate-slot reissue + PWA push",
        mechanism: "TAS reissues slots ahead of the wave and drivers are notified via WebPush / WS reroute.",
        kpi: "queue_length",
      },
      {
        kind: "outcome",
        label: "Gate queue capped, idle carbon avoided",
        mechanism: "Demand is spread across the window instead of spiking, so the queue stays bounded.",
        magnitude: { from: "41", to: "26", unit: "veh", simulated: true },
        kpi: "queue_length",
      },
    ],
  },

  "MONSOON-FRIDAY": {
    id: "MONSOON-FRIDAY",
    title: "Monsoon Friday (master)",
    summary: "Weather + weekly peak stack up; the end-to-end reactive response.",
    stages: [
      {
        kind: "cause",
        label: "Heavy monsoon rain during the Friday evening peak",
        mechanism: "Rain coincides with the weekly demand peak on the corridor.",
      },
      {
        kind: "impact",
        label: "Speeds drop, congestion spreads, throughput falls",
        mechanism: "Wet-road speed loss lifts the jam factor; the forecaster flags onset across SEG-06…SEG-12 and ANPR degrades in rain.",
        magnitude: { from: "0.20", to: "0.78", unit: "P(onset)", simulated: true },
        kpi: "congestion_onset",
        where: "SEG-09",
      },
      {
        kind: "action",
        label: "Reroute + dynamic lane allocation + advisory push",
        mechanism: "Best-alt-gate routing, extra inbound lanes and PWA advisories are applied; TAS reslots.",
        kpi: "gate_throughput",
      },
      {
        kind: "outcome",
        label: "ETA recovery + reduced idle emissions",
        mechanism: "Load spreads to unaffected gates; idle dwell falls, cutting CO₂e.",
        magnitude: { from: "19", to: "12", unit: "min dwell", simulated: true },
        kpi: "avg_dwell",
      },
    ],
  },
};

/** Default teaching chain shown when no scenario is active. */
export const DEFAULT_CHAIN_ID = "HEAVY-VEHICLE-SURGE";

/** Resolve a scenario id (TFC-1/2/3, or a teaching-chain id) to its chain. */
export function getCausalChain(id?: string | null): CausalChain | null {
  if (!id) return null;
  return CAUSAL_CHAINS[id] ?? null;
}

/** Ordered list for a scenario picker (teaching chains first). */
export const CHAIN_ORDER: string[] = [
  "HEAVY-VEHICLE-SURGE",
  "MONSOON-FRIDAY",
  "TFC-1",
  "TFC-3",
  "TFC-2",
];
