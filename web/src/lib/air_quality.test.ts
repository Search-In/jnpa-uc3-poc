// Unit tests for the air-quality presentation helpers that drive
// AirQualityTile (lib/air_quality.ts). The repo has no DOM test environment
// (vitest only, same as traffic.test.ts), so the tile's render logic is
// factored into these pure helpers and verified here: status / source / AQ
// tones, the µg/m³ formatter, and the dominant-pollutant caption.
import { describe, expect, it } from "vitest";
import type { AirQualityCurrent } from "./types";
import {
  airQualitySourceTone,
  airQualityStatusTone,
  aqStatusTone,
  dominantPollutant,
  fmtConc,
} from "./air_quality";

function response(overrides: Partial<AirQualityCurrent> = {}): AirQualityCurrent {
  return {
    status: "LIVE",
    source: "OPENAQ",
    decision_path: "LIVE",
    location: { latitude: 18.95, longitude: 72.95 },
    air_quality: {
      pm25: 48.2,
      pm10: 92.5,
      no2: 31,
      so2: 14.4,
      co: 610,
      o3: 42,
      air_quality_status: "MODERATE",
      source: "OPENAQ",
      observed_at: "2026-07-29T06:00:00Z",
      stations: ["Nhava Sheva"],
    },
    cache_age_s: null,
    units: { pm25: "µg/m³" },
    timestamp: "2026-07-29T06:05:00Z",
    ...overrides,
  };
}

describe("airQualityStatusTone", () => {
  it("maps LIVE/DEGRADED/OFFLINE to ok/warn/critical", () => {
    expect(airQualityStatusTone("LIVE")).toBe("ok");
    expect(airQualityStatusTone("DEGRADED")).toBe("warn");
    expect(airQualityStatusTone("OFFLINE")).toBe("critical");
    expect(airQualityStatusTone(undefined)).toBe("neutral");
  });
});

describe("airQualitySourceTone", () => {
  it("live source is ok, synthetic is info, fallback rungs warn", () => {
    expect(airQualitySourceTone("OPENAQ")).toBe("ok");
    expect(airQualitySourceTone("OPENAQ_CACHE")).toBe("warn");
    expect(airQualitySourceTone("OPENAQ_DB")).toBe("warn");
    expect(airQualitySourceTone("SYNTHETIC")).toBe("info");
    expect(airQualitySourceTone(undefined)).toBe("neutral");
  });
});

describe("aqStatusTone", () => {
  it("maps the four AQ labels to escalating tones", () => {
    expect(aqStatusTone("GOOD")).toBe("ok");
    expect(aqStatusTone("MODERATE")).toBe("warn");
    expect(aqStatusTone("UNHEALTHY")).toBe("critical");
    expect(aqStatusTone("VERY_UNHEALTHY")).toBe("critical");
    expect(aqStatusTone("UNKNOWN")).toBe("neutral");
    expect(aqStatusTone(null)).toBe("neutral");
  });
});

describe("fmtConc", () => {
  it("rounds and appends µg/m³, dash-safe for null", () => {
    expect(fmtConc(48.4)).toBe("48 µg/m³");
    expect(fmtConc(92.5)).toBe("93 µg/m³");
    expect(fmtConc(0)).toBe("0 µg/m³");
    expect(fmtConc(null)).toBe("—");
    expect(fmtConc(undefined)).toBe("—");
  });
});

describe("dominantPollutant", () => {
  it("picks the pollutant closest to its UNHEALTHY breakpoint", () => {
    // pm25 48.2/120 = 0.40 vs pm10 92.5/250 = 0.37 -> PM2.5 dominates
    expect(dominantPollutant(response())).toBe("PM2.5");
  });

  it("switches when another pollutant dominates", () => {
    const d = response();
    d.air_quality = { ...d.air_quality, pm25: 10, pm10: 240 };
    expect(dominantPollutant(d)).toBe("PM10");
  });

  it("is null-safe for missing data", () => {
    const d = response();
    d.air_quality = {
      ...d.air_quality,
      pm25: null,
      pm10: null,
      no2: null,
      so2: null,
      co: null,
      o3: null,
    };
    expect(dominantPollutant(d)).toBeNull();
    expect(dominantPollutant(undefined)).toBeNull();
    expect(dominantPollutant(null)).toBeNull();
  });
});
