// Unit tests for the logistics presentation helpers that drive
// LogisticsTile (lib/logistics.ts). The repo has no DOM test environment
// (vitest only, same as air_quality.test.ts), so the tile's render logic is
// factored into these pure helpers and verified here: status / source /
// tracking tones, the event caption/label builders, and the count formatter —
// plus a contract check that a representative /api/logistics/current answer
// (mirroring services/logistics/service.py) satisfies the frontend types.
import { describe, expect, it } from "vitest";
import type { LogisticsCurrent, LogisticsEvent } from "./types";
import {
  eventCaption,
  eventTypeLabel,
  fmtCount,
  logisticsSourceTone,
  logisticsStatusTone,
  trackingStatusTone,
} from "./logistics";

function event(overrides: Partial<LogisticsEvent> = {}): LogisticsEvent {
  return {
    ref_type: "VEHICLE",
    ref_id: "MH46AB1234",
    event_type: "TOLL_CROSSING",
    event_ts: "2026-07-29T06:15:29+00:00",
    location: "Karal Phata Toll Plaza",
    latitude: 18.842,
    longitude: 73.041,
    source: "ULIP",
    source_api: "FASTAG",
    ...overrides,
  };
}

function response(overrides: Partial<LogisticsCurrent> = {}): LogisticsCurrent {
  return {
    status: "LIVE",
    source: "ULIP",
    decision_path: "LIVE",
    logistics: {
      window_h: 24,
      event_count: 3,
      vehicle_count: 2,
      container_count: 1,
      events_by_type: { TOLL_CROSSING: 2, CONTAINER_MOVEMENT: 1 },
      last_event_ts: "2026-07-29T06:15:29+00:00",
      latest_events: [event()],
      tracked: [
        {
          ref_type: "VEHICLE",
          ref_id: "MH46AB1234",
          status: "IN_TRANSIT",
          last_event: "TOLL_CROSSING",
          last_location: "Karal Phata Toll Plaza",
          last_event_ts: "2026-07-29T06:15:29+00:00",
          event_count: 2,
          updated_at: "2026-07-29T06:16:00+00:00",
        },
      ],
      data_available: true,
    },
    ulip: {
      configured: true,
      last_call_at: "2026-07-29T06:16:00+00:00",
      last_call_ok: true,
      fresh: true,
    },
    cache_age_s: null,
    timestamp: "2026-07-29T06:16:05+00:00",
    ...overrides,
  };
}

describe("logisticsStatusTone", () => {
  it("maps LIVE/DEGRADED/OFFLINE to ok/warn/critical", () => {
    expect(logisticsStatusTone("LIVE")).toBe("ok");
    expect(logisticsStatusTone("DEGRADED")).toBe("warn");
    expect(logisticsStatusTone("OFFLINE")).toBe("critical");
    expect(logisticsStatusTone(undefined)).toBe("neutral");
  });
});

describe("logisticsSourceTone", () => {
  it("live source is ok, empty fallback is info, cache/db rungs warn", () => {
    expect(logisticsSourceTone("ULIP")).toBe("ok");
    expect(logisticsSourceTone("ULIP_CACHE")).toBe("warn");
    expect(logisticsSourceTone("ULIP_DB")).toBe("warn");
    expect(logisticsSourceTone("NONE")).toBe("info");
    expect(logisticsSourceTone(undefined)).toBe("neutral");
  });
});

describe("trackingStatusTone", () => {
  it("maps IN_TRANSIT/IDLE/UNKNOWN to ok/warn/neutral", () => {
    expect(trackingStatusTone("IN_TRANSIT")).toBe("ok");
    expect(trackingStatusTone("IDLE")).toBe("warn");
    expect(trackingStatusTone("UNKNOWN")).toBe("neutral");
    expect(trackingStatusTone(null)).toBe("neutral");
  });
});

describe("eventTypeLabel", () => {
  it("labels the known normalised event types", () => {
    expect(eventTypeLabel("TOLL_CROSSING")).toBe("Toll crossing");
    expect(eventTypeLabel("CONTAINER_MOVEMENT")).toBe("Container movement");
  });

  it("degrades unknown types readably and is null-safe", () => {
    expect(eventTypeLabel("RAIL_OUT")).toBe("rail out");
    expect(eventTypeLabel(null)).toBe("Event");
    expect(eventTypeLabel(undefined)).toBe("Event");
  });
});

describe("eventCaption", () => {
  it("joins label, location and reference", () => {
    expect(eventCaption(event())).toBe("Toll crossing · Karal Phata Toll Plaza (MH46AB1234)");
  });

  it("omits a missing location", () => {
    expect(eventCaption(event({ location: null }))).toBe("Toll crossing (MH46AB1234)");
  });

  it("handles container movements", () => {
    expect(
      eventCaption(
        event({
          ref_type: "CONTAINER",
          ref_id: "MSKU1234565",
          event_type: "CONTAINER_MOVEMENT",
          location: "NSICT Yard Block-B",
          source_api: "LDB",
        }),
      ),
    ).toBe("Container movement · NSICT Yard Block-B (MSKU1234565)");
  });
});

describe("fmtCount", () => {
  it("rounds, floors at zero, dash-safe for null", () => {
    expect(fmtCount(3)).toBe("3");
    expect(fmtCount(2.6)).toBe("3");
    expect(fmtCount(0)).toBe("0");
    expect(fmtCount(-1)).toBe("0");
    expect(fmtCount(null)).toBe("—");
    expect(fmtCount(undefined)).toBe("—");
  });
});

describe("LogisticsCurrent contract", () => {
  it("a LIVE answer satisfies the tile's reads", () => {
    const d = response();
    expect(d.logistics.latest_events[0].source).toBe("ULIP");
    expect(d.logistics.data_available).toBe(true);
    expect(logisticsStatusTone(d.status)).toBe("ok");
    expect(logisticsSourceTone(d.source)).toBe("ok");
  });

  it("the empty FALLBACK answer is representable — no fabricated data", () => {
    const d = response({
      status: "OFFLINE",
      source: "NONE",
      decision_path: "FALLBACK",
      logistics: {
        window_h: 24,
        event_count: 0,
        vehicle_count: 0,
        container_count: 0,
        events_by_type: {},
        last_event_ts: null,
        latest_events: [],
        tracked: [],
        data_available: false,
      },
      ulip: { configured: false, last_call_at: null, last_call_ok: null, fresh: false },
    });
    expect(d.logistics.latest_events).toHaveLength(0);
    expect(d.logistics.data_available).toBe(false);
    expect(logisticsStatusTone(d.status)).toBe("critical");
    expect(logisticsSourceTone(d.source)).toBe("info");
  });
});
