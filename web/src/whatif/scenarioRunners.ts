/**
 * The backend runner behind each scenario id, with the parameters the console sends.
 *
 * Lifted out of WhatIfConsole so it is reachable from more than the console screen. A
 * cross-twin deep link (`?scenario=MONSOON-FRIDAY`, opened from UC-2's hand-off) has to
 * start the SAME run the console's button does — same runner, same params — or the
 * narration would step through with no backend run behind it.
 */
import type { ScenarioId } from "@/hooks/ScenarioContext";

export const SCENARIOS: { id: ScenarioId; runner: string; blurb: string; params: Record<string, any> }[] =
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
