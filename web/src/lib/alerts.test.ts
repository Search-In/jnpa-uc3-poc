// alertLocation() — the notification cards showed an empty "—" for every
// camera-sourced alert because only gate_id / payload.zone_id were consulted.
import { describe, expect, it } from "vitest";
import { alertCoords, alertLocation } from "./alerts";
import type { Alert } from "./types";

const base: Alert = { id: "a1", ts: "2026-08-05T10:00:00Z", kind: "TEST", severity: "warning" };

describe("alertLocation", () => {
  it("prefers the gate column", () => {
    expect(alertLocation({ ...base, gate_id: "G-JNPT-1", payload: { zone_id: "Z-1" } })).toBe(
      "G-JNPT-1",
    );
  });

  it("falls back to the geofence zone", () => {
    expect(alertLocation({ ...base, payload: { zone_id: "Z-PARKING" } })).toBe("Z-PARKING");
    expect(alertLocation({ ...base, payload: { zone: "Karal Phata" } })).toBe("Karal Phata");
  });

  it("falls back to the corridor segment", () => {
    expect(alertLocation({ ...base, payload: { segment_id: "NH348-04" } })).toBe("NH348-04");
  });

  it("uses the camera id for ANOMALOUS_TRAJECTORY alerts", () => {
    const a: Alert = {
      ...base,
      kind: "ANOMALOUS_TRAJECTORY",
      payload: { track_id: "t9", camera_id: "CAM-GATE-3" },
    };
    expect(alertLocation(a)).toBe("CAM-GATE-3");
  });

  it("labels a bare camera/device id", () => {
    expect(alertLocation({ ...base, payload: { device_id: "9931" } })).toBe("Cam 9931");
  });

  it("falls back to coordinates when only lat/lon are present", () => {
    expect(alertLocation({ ...base, payload: { lat: 18.94871, lon: 72.95123 } })).toBe(
      "18.9487, 72.9512",
    );
  });

  it("returns the placeholder only when nothing locates the alert", () => {
    expect(alertLocation({ ...base, payload: { track_id: "t1" } })).toBe("—");
    expect(alertLocation(base)).toBe("—");
  });

  it("ignores blank strings and non-finite coordinates", () => {
    expect(alertLocation({ ...base, gate_id: "  ", payload: { zone_id: "Z-2" } })).toBe("Z-2");
    expect(alertLocation({ ...base, payload: { lat: Number.NaN, lon: 72.9 } })).toBe("—");
  });
});

describe("alertCoords", () => {
  it("reads root lat/lon and nested location objects", () => {
    expect(alertCoords({ ...base, payload: { lat: 1, lon: 2 } })).toEqual({ lat: 1, lon: 2 });
    expect(alertCoords({ ...base, payload: { location: { lat: 3, lng: 4 } } })).toEqual({
      lat: 3,
      lon: 4,
    });
    expect(alertCoords(base)).toBeNull();
  });
});
