// Pure presentation helpers for the weather surfaces (WeatherTile,
// DriverAdvisory, UC3 reports). Kept free of React/DOM so they are
// unit-testable with vitest (see weather.test.ts), the same split as
// lib/incidents.ts. The OpenWeather block is optional everywhere — when the
// backend has no OPENWEATHER_API_KEY it is null and every helper falls back
// to the Open-Meteo fields, so the UI renders exactly as before.
import type { Tone } from "@/components/ui/dtccc";
import type { WeatherCurrent } from "./types";

/** Tone for the LIVE / DEGRADED / OFFLINE status chip. */
export function weatherStatusTone(status?: WeatherCurrent["status"]): Tone {
  if (status === "LIVE") return "ok";
  if (status === "DEGRADED") return "warn";
  if (status === "OFFLINE") return "critical";
  return "neutral";
}

/** Tone for the source chip (worst fallback rung that fired). */
export function weatherSourceTone(source?: WeatherCurrent["source"]): Tone {
  if (source === "OPEN_METEO" || source === "OPEN_METEO+OPENWEATHER") return "ok";
  if (source === "SYNTHETIC") return "info";
  if (source == null) return "neutral";
  return "warn"; // cache rungs
}

/** "—"-safe numeric formatter: `fmtMeasure(1.23, "m") -> "1.2 m"`. */
export function fmtMeasure(value: number | null | undefined, unit: string, digits = 1): string {
  return value == null ? "—" : `${value.toFixed(digits)} ${unit}`;
}

/** Display condition — OpenWeather's label wins (richer), Open-Meteo backs it up. */
export function weatherCondition(w?: WeatherCurrent | null): string | null {
  return w?.openweather?.condition ?? w?.weather.condition ?? null;
}

/** Rain in mm — OpenWeather last-hour rain, else Open-Meteo precipitation. */
export function weatherRainMm(w?: WeatherCurrent | null): number | null {
  return w?.openweather?.rain ?? w?.weather.precipitation ?? null;
}

/** Humidity % — OpenWeather-only observation. */
export function weatherHumidityPct(w?: WeatherCurrent | null): number | null {
  return w?.openweather?.humidity ?? null;
}

/** Cloud cover % — OpenWeather-only observation. */
export function weatherCloudsPct(w?: WeatherCurrent | null): number | null {
  return w?.openweather?.clouds ?? null;
}

/** Tone for the operational weather label chip (openweather.label). */
export function weatherLabelTone(label?: string | null): Tone {
  if (label === "CLEAR") return "ok";
  if (label === "RAIN" || label === "LOW_VISIBILITY") return "warn";
  if (label === "STORM" || label === "SNOW") return "critical";
  return "neutral"; // CLOUDY / UNKNOWN / absent
}

/**
 * Providers actively feeding this response, for the sources footer:
 * always OPEN_METEO; plus OPENWEATHER unless the provider is disabled
 * (no API key -> sources.openweather === "DISABLED" and block is null).
 */
export function weatherProviders(w?: WeatherCurrent | null): string[] {
  const providers = ["OPEN_METEO"];
  if (w?.openweather != null && w.sources?.openweather !== "DISABLED") {
    providers.push("OPENWEATHER");
  }
  return providers;
}
