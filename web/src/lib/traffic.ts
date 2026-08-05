// Pure presentation helpers for the traffic surfaces (TrafficTile,
// DriverAdvisory). Kept free of React/DOM so they are unit-testable with
// vitest (see traffic.test.ts), the same split as lib/weather.ts. All data
// comes from GET /api/traffic/current — the TomTom key is backend-only and
// the browser never talks to api.tomtom.com.
import type { Tone } from "@/components/ui/dtccc";
import type { CongestionLevel, TrafficCurrent } from "./types";

/** Tone for the LIVE / DEGRADED / OFFLINE status chip. */
export function trafficStatusTone(status?: TrafficCurrent["status"]): Tone {
  if (status === "LIVE") return "ok";
  if (status === "DEGRADED") return "warn";
  if (status === "OFFLINE") return "critical";
  return "neutral";
}

/** Tone for the source chip (worst fallback rung that fired). */
export function trafficSourceTone(source?: TrafficCurrent["source"]): Tone {
  if (source === "TOMTOM") return "ok";
  if (source === "SYNTHETIC") return "info";
  if (source == null) return "neutral";
  return "warn"; // cache / database rungs
}

/** Tone for the LOW / MEDIUM / HIGH / SEVERE congestion chip. */
export function congestionTone(level?: CongestionLevel | null): Tone {
  if (level === "LOW") return "ok";
  if (level === "MEDIUM") return "warn";
  if (level === "HIGH" || level === "SEVERE") return "critical";
  return "neutral"; // UNKNOWN / absent
}

/** Tone for an incident severity label (MINOR/MODERATE/MAJOR/CLOSURE). */
export function incidentSeverityTone(severity?: string | null): Tone {
  if (severity === "MINOR") return "info";
  if (severity === "MODERATE") return "warn";
  if (severity === "MAJOR" || severity === "CLOSURE") return "critical";
  return "neutral";
}

/** "—"-safe speed formatter: `fmtSpeed(43.4) -> "43 km/h"`. */
export function fmtSpeed(value: number | null | undefined): string {
  return value == null ? "—" : `${Math.round(value)} km/h`;
}

/** "—"-safe delay formatter: seconds -> "45 s" / "3.5 min". */
export function fmtDelay(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  return `${(seconds / 60).toFixed(1)} min`;
}

/**
 * Percentage of free-flow speed currently achieved (0–100+), for the
 * congestion caption; null when either speed is missing/zero.
 */
export function speedRatioPct(t?: TrafficCurrent | null): number | null {
  const cur = t?.traffic.current_speed;
  const free = t?.traffic.free_flow_speed;
  if (cur == null || free == null || free <= 0) return null;
  return Math.round((cur / free) * 100);
}
