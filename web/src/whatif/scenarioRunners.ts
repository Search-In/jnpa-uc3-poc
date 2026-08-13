/**
 * The backend runner behind each scenario id, with the parameters the console sends.
 *
 * Lifted out of WhatIfConsole so it is reachable from more than the console screen. A
 * cross-twin deep link (`?scenario=MONSOON-FRIDAY`, opened from UC-2's hand-off) has to
 * start the SAME run the console's button does — same runner, same params — or the
 * narration would step through with no backend run behind it.
 */
import type { ScenarioId } from "@/hooks/ScenarioContext";

export const SCENARIOS: {
  id: ScenarioId;
  runner: string;
  blurb: string;
  /** Operator-facing parameter chips shown on the card. */
  params: Record<string, any>;
  /** Body actually POSTed to the runner; defaults to `params`. */
  runParams?: Record<string, any>;
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
  {
    // TFC-4 drives the EXISTING UC-3 implementation (migration 0144 +
    // services/yard_capacity + gateway/routers/yard.py) through
    // scenarios/tfc4.py. It runs on exactly the same run/reset/timeline
    // wiring as TFC-1/2/3 — no second implementation, no frontend animation.
    id: "TFC-4",
    runner: "tfc4",
    blurb:
      "Yard utilization reaches 95% and internal truck traffic creates arrival pressure. Hold affected truck arrivals, recommend authorized CPP/nearby parking, notify drivers, and release trucks when yard capacity becomes available.",
    params: {
      yard_utilization_pct: 95,
      yard_status: "CRITICAL",
      arrival_trucks: 14,
      recommended_parking: "CPP",
    },
    // The parameter chips above are the operator-facing summary; these are the
    // keys scenarios/tfc4.py actually accepts. Kept separate so the card can
    // read "Yard status: CRITICAL" without inventing a backend parameter.
    runParams: {
      yard_id: "JNPA-NSICT-YARD",
      gate_id: "G-NSICT",
      arrival_trucks: 14,
      target_utilization_pct: 95,
      release_containers: 5,
    },
  },
  {
    id: "MONSOON-FRIDAY",
    runner: "monsoon_friday",
    blurb:
      "Heavy Rain — cascades to driver & fuel shortage + reactive recommendations. Monsoon rain + Friday peak → congestion → demand surge → gate queue → reroute → carbon impact.",
    params: { gate_id: "G-NSICT", rain_intensity: "heavy", demand_trucks: 120 },
  },
];
