// Unit test for the violation/event heatmap resolver. Pure logic, no ArcGIS —
// this is the machine-checked statement of the location-fallback chain and the
// click-time aggregation the heatmap popup relies on.

import { describe, expect, it } from "vitest";
import {
  incidentsNear,
  resolveIncidents,
  summariseIncidents,
  zoneCentroid,
} from "./incidents";
import type { AiEvent, GeofenceEvent, TruckDevice, Zone } from "./types";

const zone = (id: string, ring: [number, number][]): Zone => ({
  id,
  name: `${id} name`,
  kind: "restricted",
  polygon: ring,
  escalation: { warn_min: 1, notice_min: 2, challan_min: 3 },
  enabled: true,
});

const square: [number, number][] = [
  [73.0, 18.9],
  [73.2, 18.9],
  [73.2, 19.1],
  [73.0, 19.1],
];

const viol = (id: number, over: Partial<GeofenceEvent>): GeofenceEvent => ({
  id,
  vehicle_id: null,
  driver_id: null,
  zone_id: null,
  event_type: null,
  entry_time: null,
  exit_time: null,
  dwell_seconds: null,
  violation_type: "OVERSTAY",
  action_taken: null,
  created_at: "2026-07-14T10:00:00Z",
  ...over,
});

describe("zoneCentroid", () => {
  it("averages ring vertices; rejects degenerate rings", () => {
    expect(zoneCentroid(square)).toEqual([73.1, 19.0]);
    expect(zoneCentroid([[1, 1]] as any)).toBeNull();
    expect(zoneCentroid(undefined)).toBeNull();
  });
});

describe("resolveIncidents — location fallback chain", () => {
  it("locates a violation by zone centroid when it has no coords", () => {
    const out = resolveIncidents({
      violations: [viol(1, { zone_id: "Z1", vehicle_id: "V1" })],
      zones: [zone("Z1", square)],
    });
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ lon: 73.1, lat: 19.0, located_by: "zone", severity: "HIGH" });
  });

  it("falls back to last-known vehicle position when the zone is unknown", () => {
    const trucks: TruckDevice[] = [
      {
        device_id: "V1",
        plate: "MH-01",
        gate_id: null,
        state: "MOVING",
        position: { lon: 72.5, lat: 18.5 },
        speed_kmh: 10,
        heading: 0,
        remaining_km: 1,
        eta_s: 60,
      },
    ];
    const out = resolveIncidents({
      violations: [viol(1, { zone_id: "MISSING", vehicle_id: "V1" })],
      zones: [zone("Z1", square)],
      trucks,
    });
    expect(out[0]).toMatchObject({ lon: 72.5, lat: 18.5, located_by: "vehicle" });
  });

  it("prefers explicit coords on an AI event over its zone", () => {
    const ai: AiEvent = {
      id: 9,
      event_type: "HELMET_MISSING",
      vehicle_id: "V2",
      driver_id: null,
      location: { lat: 18.95, lon: 73.05, zone_id: "Z1" },
      payload: {},
      created_at: "2026-07-14T11:00:00Z",
    };
    const out = resolveIncidents({ aiEvents: [ai], zones: [zone("Z1", square)] });
    expect(out[0]).toMatchObject({ lon: 73.05, lat: 18.95, located_by: "coords", severity: "MEDIUM" });
  });

  it("drops incidents that cannot be located at all", () => {
    const out = resolveIncidents({ violations: [viol(1, { zone_id: "X", vehicle_id: "Y" })] });
    expect(out).toHaveLength(0);
  });

  it("only plots ENTER/EXIT from the events stream, at low weight", () => {
    const out = resolveIncidents({
      events: [
        viol(1, { zone_id: "Z1", event_type: "ENTER", violation_type: null }),
        viol(2, { zone_id: "Z1", event_type: "DWELL", violation_type: null }),
      ],
      zones: [zone("Z1", square)],
    });
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ kind: "entry_exit", weight: 1, severity: "LOW" });
  });
});

describe("incidentsNear + summariseIncidents", () => {
  const out = resolveIncidents({
    violations: [
      viol(1, { zone_id: "Z1", vehicle_id: "V1", violation_type: "OVERSTAY" }),
      viol(2, { zone_id: "Z1", vehicle_id: "V2", violation_type: "OVERSTAY" }),
    ],
    zones: [zone("Z1", square)],
  });

  it("finds incidents within the radius and excludes far ones", () => {
    expect(incidentsNear(out, 73.1, 19.0, 500)).toHaveLength(2);
    expect(incidentsNear(out, 80.0, 25.0, 500)).toHaveLength(0);
  });

  it("aggregates counts, unique vehicles and top issue", () => {
    const s = summariseIncidents(incidentsNear(out, 73.1, 19.0, 500));
    expect(s.total).toBe(2);
    expect(s.violations).toBe(2);
    expect(s.vehicles).toBe(2);
    expect(s.topIssue).toBe("OVERSTAY");
    expect(s.dominantZone).toBe("Z1");
  });
});
