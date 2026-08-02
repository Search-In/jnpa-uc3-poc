// Unit tests for the weather presentation helpers that drive WeatherTile,
// DriverAdvisory and the UC3 report row (lib/weather.ts). The repo has no DOM
// test environment (vitest only, same as incidents.test.ts), so the tile's
// render logic is factored into these pure helpers and verified here:
// OpenWeather field selection, provider chips, and the status/source tones
// behind the loading/error/degraded presentations.
import { describe, expect, it } from "vitest";
import type { WeatherCurrent } from "./types";
import {
  fmtMeasure,
  weatherCloudsPct,
  weatherCondition,
  weatherHumidityPct,
  weatherLabelTone,
  weatherProviders,
  weatherRainMm,
  weatherSourceTone,
  weatherStatusTone,
} from "./weather";

function response(overrides: Partial<WeatherCurrent> = {}): WeatherCurrent {
  return {
    status: "LIVE",
    source: "OPEN_METEO+OPENWEATHER",
    decision_path: "LIVE",
    location: { latitude: 18.9489, longitude: 72.9492 },
    weather: {
      temperature: 29.8,
      wind_speed: 15,
      wind_direction: 240,
      wind_gusts: 28,
      visibility: 8000,
      precipitation: 0.2,
      weather_code: 3,
      condition: "Overcast",
      observed_at: "2026-07-28T10:00",
    },
    marine: {
      wave_height: 1.2,
      wave_period: 5,
      swell_wave_height: 0.9,
      sea_level_height: 0.6,
      observed_at: "2026-07-28T10:00",
    },
    openweather: {
      temperature: 30.4,
      feels_like: 35.1,
      humidity: 70,
      pressure: 1004,
      rain: 0,
      clouds: 40,
      condition: "Cloudy",
      condition_id: 802,
      description: "scattered clouds",
      label: "CLOUDY",
      wind_speed: 18,
      wind_direction: 250,
      visibility: 6000,
      station: "Uran",
      observed_at: "2026-07-28T10:00:00+00:00",
      temperature_delta: 0.6,
      temperature_consistent: true,
    },
    sources: { weather: "LIVE", marine: "LIVE", openweather: "LIVE" },
    cache_age_s: null,
    units: {},
    timestamp: "2026-07-28T10:01:00+00:00",
    ...overrides,
  };
}

describe("WeatherTile OpenWeather field selection", () => {
  it("renders the OpenWeather fields when the provider is live", () => {
    const w = response();
    expect(weatherCondition(w)).toBe("Cloudy"); // OpenWeather wins over Open-Meteo
    expect(weatherHumidityPct(w)).toBe(70);
    expect(weatherRainMm(w)).toBe(0);
    expect(weatherCloudsPct(w)).toBe(40);
    expect(fmtMeasure(weatherHumidityPct(w), "%", 0)).toBe("70 %");
    expect(weatherProviders(w)).toEqual(["OPEN_METEO", "OPENWEATHER"]);
  });

  it("falls back to Open-Meteo when OpenWeather is disabled (no API key)", () => {
    const w = response({
      openweather: null,
      source: "OPEN_METEO",
      sources: { weather: "LIVE", marine: "LIVE", openweather: "DISABLED" },
    });
    expect(weatherCondition(w)).toBe("Overcast");
    expect(weatherRainMm(w)).toBe(0.2); // Open-Meteo precipitation backs rain up
    expect(weatherHumidityPct(w)).toBeNull(); // OpenWeather-only observation
    expect(weatherCloudsPct(w)).toBeNull();
    expect(weatherProviders(w)).toEqual(["OPEN_METEO"]);
  });

  it("formats missing values as an em dash", () => {
    expect(fmtMeasure(null, "°C")).toBe("—");
    expect(fmtMeasure(undefined, "mm")).toBe("—");
    expect(fmtMeasure(1.234, "m")).toBe("1.2 m");
  });
});

describe("loading / error / degraded state tones", () => {
  it("has no status chip while loading (undefined -> neutral)", () => {
    // While the query is loading the tile renders a spinner and no chips;
    // an undefined status must never look LIVE.
    expect(weatherStatusTone(undefined)).toBe("neutral");
    expect(weatherSourceTone(undefined)).toBe("neutral");
  });

  it("maps the fallback ladder onto chip tones", () => {
    expect(weatherStatusTone("LIVE")).toBe("ok");
    expect(weatherStatusTone("DEGRADED")).toBe("warn");
    expect(weatherStatusTone("OFFLINE")).toBe("critical");
    expect(weatherSourceTone("OPEN_METEO+OPENWEATHER")).toBe("ok");
    expect(weatherSourceTone("OPEN_METEO")).toBe("ok");
    expect(weatherSourceTone("OPEN_METEO_CACHE")).toBe("warn");
    expect(weatherSourceTone("SYNTHETIC")).toBe("info");
  });

  it("maps operational weather labels onto tones", () => {
    expect(weatherLabelTone("CLEAR")).toBe("ok");
    expect(weatherLabelTone("CLOUDY")).toBe("neutral");
    expect(weatherLabelTone("RAIN")).toBe("warn");
    expect(weatherLabelTone("LOW_VISIBILITY")).toBe("warn");
    expect(weatherLabelTone("STORM")).toBe("critical");
    expect(weatherLabelTone(null)).toBe("neutral");
  });
});
