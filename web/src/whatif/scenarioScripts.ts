/**
 * scenarioScripts — the guided coach-mark storylines for the What-If Console,
 * with a SEMANTIC per-step target: each step explicitly names the business
 * object it describes and where to find it, so the guided runtime highlights the
 * exact component on the current page (never a generic default, never the map
 * unless the event is genuinely map-related).
 *
 * This mirrors the reference project's per-step `tab` + `spotlight` +
 * `valueTargets`, unified here into one `GuidedTarget` so every step declares:
 *   - page          (which existing route shows it)
 *   - kind           ("map" → ring the map asset; "dom" → ring a DOM component)
 *   - component      (human label of the business object)
 *   - selector        (data-guided-id of the DOM element, for kind === "dom")
 *   - mapAssets       (gate/segment ids, for kind === "map")
 *   - highlightType   ("halo" for map, "ring" for DOM)
 *   - scrollBehaviour ("center" to scroll the element into view, "none")
 *
 * Targets are resolved at runtime by data-guided-id (DOM) or asset id (map) —
 * no hard-coded coordinates. The step order is 1:1 with the real step_no order
 * emitted by scenarios/tfc{1,2,3}.py.
 */
import type { ScenarioId } from "@/hooks/ScenarioContext";

/** A human-readable metric change surfaced as a chip in the coach-mark. */
export interface MetricChange {
  label: string;
  from: number | string;
  to: number | string;
  unit?: string;
  /** Direction the operator should read this as (drives the chip colour). */
  tone: "worse" | "better" | "neutral";
}

/** The business object a step points at, and how to find + highlight it. */
export interface GuidedTarget {
  /** Existing route that shows this object (drives view navigation). */
  page: string;
  /** "map" rings the map asset (halo + goTo); "dom" rings a tagged component. */
  kind: "map" | "dom";
  /** Human label of the business object (shown to the operator). */
  component: string;
  /** DOM: the data-guided-id of the element to ring (kind === "dom"). */
  selector?: string;
  /** Map: gate/segment ids to halo + frame (kind === "map"). */
  mapAssets?: string[];
  /** Visual highlight: map halo vs DOM ring. */
  highlightType: "halo" | "ring";
  /** DOM scroll behaviour when bringing the element into view. */
  scrollBehaviour?: "center" | "none";
}

export interface GuidedStep {
  /** Short coach-mark title — what's happening, in plain words. */
  title: string;
  /** One or two sentences a non-expert can follow. */
  explain: string;
  /** The business object this step describes + where/how to highlight it. */
  target: GuidedTarget;
  /** Metric deltas shown as chips (before → after). */
  metrics: MetricChange[];
  /** The automated action the twin took at this step (the "so the system did X"). */
  action?: { kind: string; detail: string };
}

export interface GuidedScript {
  id: ScenarioId;
  /** Runner name (tfc1/tfc2/tfc3) — matches the run mutation. */
  runner: string;
  /** Title shown in the coach-mark header. */
  title: string;
  /** Ordered storyline (1:1 with the runner's real step_no order). */
  steps: GuidedStep[];
}

// TFC-1 — scenarios/tfc1.py: close gate → queue build-up → forecaster spillover
// → auto-reroute → TAS reschedule.
const TFC1: GuidedScript = {
  id: "TFC-1",
  runner: "tfc1",
  title: "TFC-1 · Gate closure",
  steps: [
    {
      title: "Gate G-NSICT is marked CLOSED",
      target: {
        page: "/live",
        kind: "map",
        component: "Gate G-NSICT marker on the Live Operations map",
        mapAssets: ["G-NSICT"],
        highlightType: "halo",
      },
      explain:
        "The scenario takes the NSICT gate out of service. The affected gate is ringed on the map — from this moment no trucks can clear there.",
      metrics: [{ label: "G-NSICT status", from: "OPEN", to: "CLOSED", tone: "worse" }],
      action: { kind: "GATE_CLOSURE", detail: "G-NSICT closed_at set — gate removed from service" },
    },
    {
      title: "The gate queue builds up",
      target: {
        page: "/advisory",
        kind: "dom",
        component: "Queued-trucks table (the AT_GATE_QUEUE backlog)",
        selector: "advisory-queue",
        highlightType: "ring",
        scrollBehaviour: "center",
      },
      explain:
        "Inbound trucks pile into AT_GATE_QUEUE behind the closed gate. The queue table — the growing backlog — is highlighted.",
      metrics: [
        { label: "AT_GATE_QUEUE @ G-NSICT", from: 0, to: 12, unit: "trucks", tone: "worse" },
      ],
    },
    {
      title: "Forecaster predicts spillover to neighbouring gates",
      target: {
        page: "/live",
        kind: "map",
        component: "Gates G-JNPCT & G-NSIGT (spillover) on the map",
        mapAssets: ["G-JNPCT", "G-NSIGT"],
        highlightType: "halo",
      },
      explain:
        "The congestion forecaster predicts the jam will spill over to G-JNPCT and G-NSIGT (P ≥ 0.7). Those two gates are ringed on the map.",
      metrics: [{ label: "Spillover probability", from: "—", to: "P ≥ 0.7", tone: "worse" }],
      action: {
        kind: "FORECAST_RERUN",
        detail: "Spillover predicted to G-JNPCT & G-NSIGT (P ≥ 0.7)",
      },
    },
    {
      title: "Trucks are auto-re-routed off the closed gate",
      target: {
        page: "/advisory",
        kind: "dom",
        component: "Push Re-route action (the re-routing control)",
        selector: "advisory-reroute",
        highlightType: "ring",
        scrollBehaviour: "center",
      },
      explain:
        "The twin re-routes EN_ROUTE_TO_PORT trucks to the least-loaded alternative. The Push Re-route control that drives the advisory is highlighted.",
      metrics: [{ label: "Re-routed trucks", from: 0, to: 8, unit: "trucks", tone: "better" }],
      action: {
        kind: "AUTO_REROUTE",
        detail: "Inbound trucks re-routed off G-NSICT via the gateway",
      },
    },
    {
      title: "TAS slots are rescheduled",
      target: {
        page: "/live",
        kind: "dom",
        component: "Terminal Appointment System widget (the slot board)",
        selector: "tas-widget",
        highlightType: "ring",
        scrollBehaviour: "center",
      },
      explain:
        "The Terminal Appointment System marks the affected G-NSICT slots RESCHEDULED so arrivals line up with the new gate plan. The TAS slot board is scrolled into view and highlighted — watch its Rescheduled count.",
      metrics: [{ label: "RESCHEDULED slots", from: 0, to: 14, unit: "slots", tone: "better" }],
      action: { kind: "TAS_RESCHEDULE", detail: "TAS slots at G-NSICT marked RESCHEDULED" },
    },
  ],
};

// TFC-2 — scenarios/tfc2.py: wrong-way track → anomaly alert → e-Challan →
// payload enriched → evidence clip.
const TFC2: GuidedScript = {
  id: "TFC-2",
  runner: "tfc2",
  title: "TFC-2 · Congestion surge",
  steps: [
    {
      title: "A wrong-way track is injected at Karal Phata",
      target: {
        page: "/live",
        kind: "map",
        component: "Wrong-way location on the NH-348 corridor (Karal Phata, SEG-03)",
        mapAssets: ["SEG-03"],
        highlightType: "halo",
      },
      explain:
        "A synthetic GPS track moves against traffic past Karal Phata. The corridor location of the wrong-way event is ringed on the map.",
      metrics: [{ label: "Wrong-way pings", from: 0, to: 6, unit: "pings", tone: "worse" }],
    },
    {
      title: "The anomaly service raises a WRONG_WAY alert",
      target: {
        page: "/live",
        kind: "dom",
        component: "The WRONG_WAY alert card in the Active-alerts panel",
        selector: "alert-WRONG_WAY",
        highlightType: "ring",
        scrollBehaviour: "center",
      },
      explain:
        "The AI anomaly detector emits a WRONG_WAY alert. The matching alert card in the Active-alerts panel is highlighted — the exact alert, not the panel.",
      metrics: [{ label: "Alert", from: "—", to: "WRONG_WAY", tone: "worse" }],
      action: { kind: "ANOMALY_ALERT", detail: "WRONG_WAY alert emitted for plate MH04WW1234" },
    },
    {
      title: "An e-Challan is issued for the offence",
      target: {
        page: "/reports",
        kind: "dom",
        component: "The WRONG_WAY incident row (the e-Challan) in Police Reports",
        selector: "report-WRONG_WAY",
        highlightType: "ring",
        scrollBehaviour: "center",
      },
      explain:
        "The plate is resolved via Vahan and an e-Challan is issued. The corresponding incident row in the Police Reports table is highlighted.",
      metrics: [{ label: "e-Challan", from: "—", to: "ISSUED", tone: "neutral" }],
      action: { kind: "E_CHALLAN", detail: "e-Challan issued for MH04WW1234 (WRONG_WAY)" },
    },
    {
      title: "The alert is enriched with the e-Challan",
      // Runtime (tfc2.py step 4): _enrich_alert(alert.id, {echallan_id,
      // echallan_pdf_url, evidence_mp4_url}) — the business object is THE ALERT,
      // not a report row. It lives in the Live Operations "Active alerts" panel.
      target: {
        page: "/live",
        kind: "dom",
        component: "The WRONG_WAY alert (now carrying the e-Challan) in Active alerts",
        selector: "alert-WRONG_WAY",
        highlightType: "ring",
        scrollBehaviour: "center",
      },
      explain:
        "The alert's payload is updated with the echallan_id + PDF link. The WRONG_WAY alert card is highlighted — open it to see the e-Challan stamped on the violation.",
      metrics: [{ label: "Alert payload", from: "alert", to: "alert + challan", tone: "better" }],
      action: {
        kind: "ALERT_ENRICH",
        detail: "echallan_id + echallan_pdf_url stamped on the alert",
      },
    },
    {
      title: "Evidence clip is attached to the alert",
      // Runtime (tfc2.py step 5): "Evidence clip (last 10 s) available for the
      // alert drawer" — the alert's evidence drawer, on Live Operations.
      target: {
        page: "/live",
        kind: "dom",
        component: "The WRONG_WAY alert (its drawer holds the 10 s clip)",
        selector: "alert-WRONG_WAY",
        highlightType: "ring",
        scrollBehaviour: "center",
      },
      explain:
        "The last 10 s of footage is bound to this alert's evidence drawer. The WRONG_WAY alert is highlighted — open it to play the clip and view the e-Challan PDF.",
      metrics: [{ label: "Evidence clip", from: "—", to: "10 s", tone: "better" }],
      action: { kind: "EVIDENCE", detail: "10 s evidence clip available in the alert drawer" },
    },
  ],
};

// TFC-3 — scenarios/tfc3.py: cross-twin DPD release → demand surge → forecaster
// build-up → gate-slot reissue → cross-twin link.
const TFC3: GuidedScript = {
  id: "TFC-3",
  runner: "tfc3",
  title: "TFC-3 · GPS / re-route",
  steps: [
    {
      title: "UC-II publishes a DPD release spike",
      target: {
        page: "/live",
        kind: "map",
        component: "Corridor entry where the DPD demand lands (SEG-07)",
        mapAssets: ["SEG-07"],
        highlightType: "halo",
      },
      explain:
        "The neighbouring cargo twin releases a 2.5× DPD spike. The corridor entry where that demand lands is ringed on the map.",
      metrics: [{ label: "DPD release", from: "1×", to: "2.5×", tone: "worse" }],
      action: { kind: "CROSS_TWIN_PUSH", detail: "UC-II published cargo.dpd_release spike ×2.5" },
    },
    {
      title: "Corridor demand surges",
      target: {
        page: "/live",
        kind: "map",
        component: "Surge across NH-348 corridor segments (SEG-07 … SEG-12)",
        mapAssets: ["SEG-07", "SEG-08", "SEG-09", "SEG-10", "SEG-11", "SEG-12"],
        highlightType: "halo",
      },
      explain:
        "The release becomes real demand — a wave of inbound trucks. The surge stretch of the corridor is ringed and framed on the map.",
      metrics: [{ label: "Inbound trucks", from: 0, to: 18, unit: "trucks", tone: "worse" }],
    },
    {
      title: "Forecaster predicts a build-up on NH-348",
      target: {
        page: "/live",
        kind: "map",
        component: "NH-348 build-up segments (SEG-08 … SEG-12)",
        mapAssets: ["SEG-08", "SEG-09", "SEG-10", "SEG-11", "SEG-12"],
        highlightType: "halo",
      },
      explain:
        "The forecaster flags a build-up on NH-348 segments 8–14. Exactly those segments are ringed on the map.",
      metrics: [{ label: "Segments building up", from: 0, to: 7, unit: "segments", tone: "worse" }],
      action: { kind: "FORECAST_RERUN", detail: "Build-up predicted on NH-348 segments 8–14" },
    },
    {
      title: "Gate-slot windows are reissued",
      // Runtime (tfc3.py step 4): driver-advisory reissues GATE-SLOT WINDOWS for
      // the surge trucks. The gate-slot board is the TAS slot widget. (/advisory
      // would be empty here — TFC-3's trucks are EN_ROUTE_TO_PORT, not the
      // AT_GATE_QUEUE the Driver Advisory table shows.)
      target: {
        page: "/live",
        kind: "dom",
        component: "Terminal Appointment System widget (gate-slot windows)",
        selector: "tas-widget",
        highlightType: "ring",
        scrollBehaviour: "center",
      },
      explain:
        "Driver-advisory reissues gate-slot windows for the surge trucks. The TAS slot board — the gate-slot windows — is scrolled into view and highlighted.",
      metrics: [{ label: "Reissued windows", from: 0, to: 18, unit: "trucks", tone: "better" }],
      action: {
        kind: "SLOT_REISSUE",
        detail: "Gate-slot windows reissued to drivers via the gateway",
      },
    },
    {
      title: "Cross-twin link is recorded",
      target: {
        page: "/what-if",
        kind: "dom",
        component: "The cross-twin link badge (UC-II → UC-III) in the timeline",
        selector: "crosstwin-link",
        highlightType: "ring",
        scrollBehaviour: "center",
      },
      explain:
        "The twin records the causal link UC-II DPD release → UC-III demand. The cross-twin badge on the timeline step is scrolled into view and highlighted.",
      metrics: [{ label: "Cross-twin link", from: "—", to: "UC-II → UC-III", tone: "neutral" }],
      action: { kind: "CROSS_TWIN_LINK", detail: "UC-II DPD release → UC-III demand recorded" },
    },
  ],
};

export const GUIDED_SCRIPTS: GuidedScript[] = [TFC1, TFC2, TFC3];

export function getScript(id: ScenarioId | string | null): GuidedScript | undefined {
  return GUIDED_SCRIPTS.find((s) => s.id === id || s.runner === id);
}
