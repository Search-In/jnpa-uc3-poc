// Shared alert helpers — used by the header notification drawer (and previously
// the live screen). Pure utilities; no API/business-logic changes.

import type { Alert } from "@/lib/types";

/** Stable identity for an alert (id when present, else a content composite). */
export function alertKey(a: Alert): string {
  return a.id || `${a.kind}-${a.ts}-${a.plate}`;
}

/** Trimmed string form of a payload value; "" for objects/null/blank. */
function str(v: unknown): string {
  if (typeof v === "string") return v.trim();
  if (typeof v === "number" && Number.isFinite(v)) return String(v);
  return "";
}

/**
 * Coordinates carried by an alert, if any. Producers are inconsistent: the
 * anomaly engine puts `lat`/`lon` at the payload root (ai/anomaly/engine.py),
 * while some geofence producers nest them under `location`.
 */
export function alertCoords(a: Alert): { lat: number; lon: number } | null {
  const p = (a.payload ?? {}) as Record<string, unknown>;
  const nested = (
    typeof p.location === "object" && p.location !== null ? p.location : {}
  ) as Record<string, unknown>;
  const lat = [p.lat, p.latitude, nested.lat, nested.latitude].find((v) => typeof v === "number");
  const lon = [p.lon, p.lng, p.longitude, nested.lon, nested.lng, nested.longitude].find(
    (v) => typeof v === "number",
  );
  if (typeof lat !== "number" || typeof lon !== "number") return null;
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  return { lat, lon };
}

/**
 * Human-readable place for an alert, most specific locator first.
 *
 * Display-only: it reads the fields the producers already emit and never
 * changes them. Previously the alert cards only looked at `gate_id` and
 * `payload.zone_id`, so every camera-sourced alert (ANPR, ANOMALOUS_TRAJECTORY,
 * WRONG_WAY — see ai/anomaly/engine.py) rendered an empty "—" even though it
 * carries `camera_id` and `lat`/`lon`.
 */
export function alertLocation(a: Alert, fallback = "—"): string {
  const p = (a.payload ?? {}) as Record<string, unknown>;

  const gate = str(a.gate_id) || str(p.gate_id) || str(p.gate);
  if (gate) return gate;

  const zone = str(p.zone_name) || str(p.zone_id) || str(p.zone) || str(p.geofence_id);
  if (zone) return zone;

  const segment = str(p.segment_id) || str(p.segment) || str(p.road_segment) || str(p.corridor);
  if (segment) return segment;

  // Free-text place, when a producer supplies one (`location` may also be an
  // object of coordinates — str() yields "" for that and we fall through).
  const place = str(p.location) || str(p.place) || str(p.landmark) || str(p.address);
  if (place) return place;

  const camera = str(p.camera_id) || str(p.camera) || str(p.cam_id) || str(p.device_id);
  if (camera) return /^(cam|c-)/i.test(camera) ? camera : `Cam ${camera}`;

  const coords = alertCoords(a);
  if (coords) return `${coords.lat.toFixed(4)}, ${coords.lon.toFixed(4)}`;

  return fallback;
}

/** Merge WS-live alerts with the adapter seed, de-duped, newest-first, capped. */
export function mergeAlerts(live: Alert[], seed: Alert[], limit = 50): Alert[] {
  const seen = new Set<string>();
  const out: Alert[] = [];
  for (const a of [...live, ...seed]) {
    const key = alertKey(a);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(a);
  }
  return out.slice(0, limit);
}
