// Unit tests for the traffic presentation helpers that drive TrafficTile and
// the DriverAdvisory traffic card (lib/traffic.ts). The repo has no DOM test
// environment (vitest only, same as weather.test.ts), so the tile's render
// logic is factored into these pure helpers and verified here: congestion /
// severity / status / source tones, speed + delay formatting, and the
// free-flow speed ratio caption.
import { describe, expect, it } from "vitest";
import type { TrafficCurrent } from "./types";
import {
  congestionTone,
  fmtDelay,
  fmtSpeed,
  incidentSeverityTone,
  speedRatioPct,
  trafficSourceTone,
  trafficStatusTone,
} from "./traffic";

function response(overrides: Partial<TrafficCurrent> = {}): TrafficCurrent {
  return {
    status: "LIVE",
    source: "TOMTOM",
    decision_path: "LIVE",
    location: { latitude: 18.9489, longitude: 72.9492 },
    traffic: {
      current_speed: 43,
      free_flow_speed: 50,
      current_travel_time: 540,
      free_flow_travel_time: 465,
      congestion_level: "LOW",
      delay_seconds: 75,
      road_closure: false,
      confidence: 0.94,
      road_class: "FRC0",
    },
    incidents: [
      {
        type: "ROAD_WORKS",
        description: "Roadworks",
        severity: "MODERATE",
        road: "NH-348",
        delay: 120,
      },
    ],
    incident_count: 1,
    sources: { traffic: "LIVE", incidents: "LIVE" },
    cache_age_s: null,
    units: {},
    timestamp: "2026-07-28T10:00:00+00:00",
    ...overrides,
  };
}

describe("trafficStatusTone", () => {
  it("maps LIVE/DEGRADED/OFFLINE to ok/warn/critical", () => {
    expect(trafficStatusTone("LIVE")).toBe("ok");
    expect(trafficStatusTone("DEGRADED")).toBe("warn");
    expect(trafficStatusTone("OFFLINE")).toBe("critical");
    expect(trafficStatusTone(undefined)).toBe("neutral");
  });
});

describe("trafficSourceTone", () => {
  it("live TOMTOM is ok, fallback rungs warn, synthetic info", () => {
    expect(trafficSourceTone("TOMTOM")).toBe("ok");
    expect(trafficSourceTone("TOMTOM_CACHE")).toBe("warn");
    expect(trafficSourceTone("TOMTOM_DB")).toBe("warn");
    expect(trafficSourceTone("SYNTHETIC")).toBe("info");
    expect(trafficSourceTone(undefined)).toBe("neutral");
  });
});

describe("congestionTone", () => {
  it("escalates LOW→MEDIUM→HIGH/SEVERE and neutralises UNKNOWN", () => {
    expect(congestionTone("LOW")).toBe("ok");
    expect(congestionTone("MEDIUM")).toBe("warn");
    expect(congestionTone("HIGH")).toBe("critical");
    expect(congestionTone("SEVERE")).toBe("critical");
    expect(congestionTone("UNKNOWN")).toBe("neutral");
    expect(congestionTone(null)).toBe("neutral");
  });
});

describe("incidentSeverityTone", () => {
  it("maps TomTom magnitude labels onto chip tones", () => {
    expect(incidentSeverityTone("MINOR")).toBe("info");
    expect(incidentSeverityTone("MODERATE")).toBe("warn");
    expect(incidentSeverityTone("MAJOR")).toBe("critical");
    expect(incidentSeverityTone("CLOSURE")).toBe("critical");
    expect(incidentSeverityTone("UNKNOWN")).toBe("neutral");
    expect(incidentSeverityTone(null)).toBe("neutral");
  });
});

describe("fmtSpeed", () => {
  it("rounds to whole km/h and is null-safe", () => {
    expect(fmtSpeed(43.4)).toBe("43 km/h");
    expect(fmtSpeed(49.5)).toBe("50 km/h");
    expect(fmtSpeed(null)).toBe("—");
    expect(fmtSpeed(undefined)).toBe("—");
  });
});

describe("fmtDelay", () => {
  it("shows seconds under a minute, minutes above, null-safe", () => {
    expect(fmtDelay(45)).toBe("45 s");
    expect(fmtDelay(75)).toBe("1.3 min");
    expect(fmtDelay(210)).toBe("3.5 min");
    expect(fmtDelay(null)).toBe("—");
  });
});

describe("speedRatioPct", () => {
  it("derives the percent-of-free-flow caption from the flow block", () => {
    expect(speedRatioPct(response())).toBe(86); // 43/50
  });
  it("is null when either speed is missing or free flow is zero", () => {
    expect(
      speedRatioPct(response({ traffic: { ...response().traffic, current_speed: null } })),
    ).toBeNull();
    expect(
      speedRatioPct(response({ traffic: { ...response().traffic, free_flow_speed: 0 } })),
    ).toBeNull();
    expect(speedRatioPct(null)).toBeNull();
    expect(speedRatioPct(undefined)).toBeNull();
  });
});
