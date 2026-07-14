// Incident resolution + aggregation for the Geo Analytics violation/event
// heatmap. Framework-agnostic (no ArcGIS imports) so it stays unit-testable and
// reusable: it turns RDS-backed geo-fence violations, entry/exit events and AI
// events into geolocated points the Esri HeatmapRenderer can consume.
//
// The source rows carry NO lat/lon of their own (GeofenceEvent/AiEvent are keyed
// by zone_id / vehicle_id), so every incident is geolocated via a fallback chain:
//   1. explicit coordinates (AI events sometimes embed {lat,lon} in `location`)
//   2. the centroid of its zone polygon
//   3. the vehicle's last-known position (from the live truck feed)
// Anything that resolves to none of these is dropped — we never plot an incident
// at a made-up location.

import type { AiEvent, GeofenceEvent, TruckDevice, Zone } from "./types";

export type IncidentKind = "violation" | "ai" | "entry_exit";
export type IncidentSeverity = "HIGH" | "MEDIUM" | "LOW";
export type LocatedBy = "coords" | "zone" | "vehicle";

export interface IncidentPoint {
  id: string;
  kind: IncidentKind;
  lat: number;
  lon: number;
  /** Heatmap weight — higher = hotter (violations outweigh plain events). */
  weight: number;
  event_type: string;
  vehicle_id: string | null;
  zone_id: string | null;
  severity: IncidentSeverity;
  status: string | null;
  created_at: string;
  located_by: LocatedBy;
}

export interface ResolveInput {
  violations?: GeofenceEvent[];
  /** Entry/exit timeline rows (only ENTER/EXIT are plotted, at low weight). */
  events?: GeofenceEvent[];
  aiEvents?: AiEvent[];
  zones?: Zone[];
  trucks?: TruckDevice[];
}

// Severity → heatmap weight. Violations dominate the surface; AI detections are
// mid; raw entry/exit crossings barely tint it (they're context, not hotspots).
const WEIGHT: Record<IncidentSeverity, number> = { HIGH: 3, MEDIUM: 2, LOW: 1 };

/** Average-of-vertices centroid of a [lon,lat] ring. Null for degenerate rings. */
export function zoneCentroid(polygon?: [number, number][]): [number, number] | null {
  if (!polygon || polygon.length < 3) return null;
  let sx = 0;
  let sy = 0;
  for (const [lon, lat] of polygon) {
    sx += lon;
    sy += lat;
  }
  return [sx / polygon.length, sy / polygon.length];
}

/** Pull explicit [lon,lat] from an AI event's free-form `location` blob. */
function coordsFromLocation(loc: Record<string, unknown> | null | undefined): [number, number] | null {
  if (!loc || typeof loc !== "object") return null;
  const lat = (loc as any).lat ?? (loc as any).latitude;
  const lon = (loc as any).lon ?? (loc as any).lng ?? (loc as any).longitude;
  if (typeof lat === "number" && typeof lon === "number") return [lon, lat];
  const nlat = Number(lat);
  const nlon = Number(lon);
  if (Number.isFinite(nlat) && Number.isFinite(nlon) && lat != null && lon != null) return [nlon, nlat];
  return null;
}

function zoneIdFromLocation(loc: Record<string, unknown> | null | undefined): string | null {
  if (!loc || typeof loc !== "object") return null;
  const z = (loc as any).zone_id ?? (loc as any).gate_id;
  return z != null ? String(z) : null;
}

/**
 * Resolve RDS rows into geolocated, weighted heatmap points. Pure — no fetching.
 */
export function resolveIncidents(input: ResolveInput): IncidentPoint[] {
  const { violations = [], events = [], aiEvents = [], zones = [], trucks = [] } = input;

  const zoneC = new Map<string, [number, number]>();
  for (const z of zones) {
    const c = zoneCentroid(z.polygon);
    if (c) zoneC.set(z.id, c);
  }

  // Last-known vehicle position, keyed by both device_id and plate so either
  // identifier on an incident row can hit.
  const vehC = new Map<string, [number, number]>();
  for (const t of trucks) {
    if (typeof t.position?.lon !== "number" || typeof t.position?.lat !== "number") continue;
    const p: [number, number] = [t.position.lon, t.position.lat];
    if (t.device_id) vehC.set(t.device_id, p);
    if (t.plate) vehC.set(t.plate, p);
  }

  const locate = (
    zoneId: string | null,
    vehicleId: string | null,
    coords: [number, number] | null,
  ): { lon: number; lat: number; by: LocatedBy } | null => {
    if (coords) return { lon: coords[0], lat: coords[1], by: "coords" };
    if (zoneId && zoneC.has(zoneId)) {
      const [lon, lat] = zoneC.get(zoneId)!;
      return { lon, lat, by: "zone" };
    }
    if (vehicleId && vehC.has(vehicleId)) {
      const [lon, lat] = vehC.get(vehicleId)!;
      return { lon, lat, by: "vehicle" };
    }
    return null;
  };

  const out: IncidentPoint[] = [];

  for (const v of violations) {
    const loc = locate(v.zone_id, v.vehicle_id, null);
    if (!loc) continue;
    out.push({
      id: `viol-${v.id}`,
      kind: "violation",
      lat: loc.lat,
      lon: loc.lon,
      weight: WEIGHT.HIGH,
      event_type: v.violation_type ?? v.event_type ?? "Violation",
      vehicle_id: v.vehicle_id,
      zone_id: v.zone_id,
      severity: "HIGH",
      status: v.action_taken,
      created_at: v.created_at,
      located_by: loc.by,
    });
  }

  for (const e of aiEvents) {
    const coords = coordsFromLocation(e.location);
    const zoneId = zoneIdFromLocation(e.location);
    const loc = locate(zoneId, e.vehicle_id, coords);
    if (!loc) continue;
    out.push({
      id: `ai-${e.id}`,
      kind: "ai",
      lat: loc.lat,
      lon: loc.lon,
      weight: WEIGHT.MEDIUM,
      event_type: e.event_type,
      vehicle_id: e.vehicle_id,
      zone_id: zoneId,
      severity: "MEDIUM",
      status: null,
      created_at: e.created_at,
      located_by: loc.by,
    });
  }

  for (const ev of events) {
    if (ev.event_type !== "ENTER" && ev.event_type !== "EXIT") continue;
    const loc = locate(ev.zone_id, ev.vehicle_id, null);
    if (!loc) continue;
    out.push({
      id: `evt-${ev.id}`,
      kind: "entry_exit",
      lat: loc.lat,
      lon: loc.lon,
      weight: WEIGHT.LOW,
      event_type: ev.event_type ?? "Crossing",
      vehicle_id: ev.vehicle_id,
      zone_id: ev.zone_id,
      severity: "LOW",
      status: null,
      created_at: ev.created_at,
      located_by: loc.by,
    });
  }

  return out;
}

// --- click-time aggregation --------------------------------------------------

const EARTH_R_M = 6_371_000;

/** Great-circle distance in metres between two [lon,lat] points. */
function haversineM(lon1: number, lat1: number, lon2: number, lat2: number): number {
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_R_M * Math.asin(Math.min(1, Math.sqrt(a)));
}

/** Incidents within `radiusM` of a clicked [lon,lat]. */
export function incidentsNear(
  incidents: IncidentPoint[],
  lon: number,
  lat: number,
  radiusM: number,
): IncidentPoint[] {
  return incidents.filter((i) => haversineM(lon, lat, i.lon, i.lat) <= radiusM);
}

export interface IncidentSummary {
  total: number;
  violations: number;
  vehicles: number;
  topIssue: string | null;
  lastEvent: string | null;
  dominantZone: string | null;
  recent: IncidentPoint[];
}

function mostCommon(values: (string | null | undefined)[]): string | null {
  const m = new Map<string, number>();
  for (const v of values) {
    if (!v) continue;
    m.set(v, (m.get(v) ?? 0) + 1);
  }
  let best: string | null = null;
  let bestN = 0;
  for (const [k, n] of m) {
    if (n > bestN) {
      best = k;
      bestN = n;
    }
  }
  return best;
}

/** Aggregate a cluster of incidents into the fields the hotspot popup renders. */
export function summariseIncidents(near: IncidentPoint[]): IncidentSummary {
  const recent = [...near].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
  return {
    total: near.length,
    violations: near.filter((i) => i.kind === "violation").length,
    vehicles: new Set(near.map((i) => i.vehicle_id).filter(Boolean)).size,
    topIssue: mostCommon(near.map((i) => i.event_type)),
    lastEvent: recent[0]?.created_at ?? null,
    dominantZone: mostCommon(near.map((i) => i.zone_id)),
    recent: recent.slice(0, 5),
  };
}
