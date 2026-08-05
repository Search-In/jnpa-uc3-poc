// Pure presentation helpers for the air-quality surface (AirQualityTile).
// Kept free of React/DOM so they are unit-testable with vitest (see
// air_quality.test.ts), the same split as lib/traffic.ts. All data comes from
// GET /api/air-quality/current — the browser never talks to api.openaq.org.
import type { Tone } from "@/components/ui/dtccc";
import type { AirQualityCurrent, AqStatus } from "./types";

/** Tone for the LIVE / DEGRADED / OFFLINE status chip. */
export function airQualityStatusTone(status?: AirQualityCurrent["status"]): Tone {
  if (status === "LIVE") return "ok";
  if (status === "DEGRADED") return "warn";
  if (status === "OFFLINE") return "critical";
  return "neutral";
}

/** Tone for the source chip (worst fallback rung that fired). */
export function airQualitySourceTone(source?: AirQualityCurrent["source"]): Tone {
  if (source === "OPENAQ") return "ok";
  if (source === "SYNTHETIC") return "info";
  if (source == null) return "neutral";
  return "warn"; // cache / database rungs
}

/** Tone for the GOOD / MODERATE / UNHEALTHY / VERY_UNHEALTHY AQ chip. */
export function aqStatusTone(status?: AqStatus | null): Tone {
  if (status === "GOOD") return "ok";
  if (status === "MODERATE") return "warn";
  if (status === "UNHEALTHY" || status === "VERY_UNHEALTHY") return "critical";
  return "neutral"; // UNKNOWN / absent
}

/** "—"-safe concentration formatter: `fmtConc(48.4) -> "48 µg/m³"`. */
export function fmtConc(value: number | null | undefined): string {
  return value == null ? "—" : `${Math.round(value)} µg/m³`;
}

/**
 * The pollutant driving the overall AQ status (highest fraction of its
 * UNHEALTHY breakpoint), for the tile caption; null when nothing is measured.
 * Breakpoints mirror integrations/openaq/schemas.py (_BREAKPOINTS upper
 * UNHEALTHY bounds).
 */
const UNHEALTHY_BOUND: Record<string, number> = {
  pm25: 120,
  pm10: 250,
  no2: 180,
  so2: 380,
  o3: 168,
  co: 10_000,
};
const POLLUTANT_LABEL: Record<string, string> = {
  pm25: "PM2.5",
  pm10: "PM10",
  no2: "NO₂",
  so2: "SO₂",
  o3: "O₃",
  co: "CO",
};

export function dominantPollutant(d?: AirQualityCurrent | null): string | null {
  if (!d) return null;
  let best: string | null = null;
  let bestRatio = -1;
  for (const [key, bound] of Object.entries(UNHEALTHY_BOUND)) {
    const value = (d.air_quality as unknown as Record<string, unknown>)[key];
    if (typeof value !== "number") continue;
    const ratio = value / bound;
    if (ratio > bestRatio) {
      bestRatio = ratio;
      best = key;
    }
  }
  return best ? POLLUTANT_LABEL[best] : null;
}
