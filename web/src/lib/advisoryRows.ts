// Row helpers for the Driver-Advisory (Congestion Rerouting) console.
//
// The console now renders TWO kinds of row side by side and must never let one
// be mistaken for the other:
//
//   * `truck-sim`      — a synthetic simulator truck MEASURED to be in the
//                        requested TruckState, with a real ETA and distance;
//   * `pwa-registered` — a device a real driver is signed in on. Real, but its
//                        queue position was never measured, so its state, ETA,
//                        remaining distance and gate are null.
//
// These live outside the component because the rule they encode — never print a
// figure nobody measured — is the whole point of the change, and a rule worth
// stating is worth testing.

import type { TruckDevice } from "./types";

/** `source` value the gateway stamps on a device a driver is signed in on. */
export const PWA_SOURCE = "pwa-registered";
/** `source` value the gateway stamps on a simulator truck. */
export const SIM_SOURCE = "truck-sim";

/** True for a row that came from a real driver's PWA registration. */
export function isRegisteredDevice(truck: TruckDevice): boolean {
  return truck.source === PWA_SOURCE;
}

// Free-flow highway speed (km/h) used as a client-side safety net when the
// truck-sim payload lacks `eta_s`. The backend now always supplies one (seeded
// at inject + a serializer fallback), so this rarely triggers; the value mirrors
// the backend's speed_highway_kmh so the estimate stays consistent if it does.
export const FREE_FLOW_KMH = 55;

/**
 * ETA-to-gate in seconds: prefer the live `eta_s`; otherwise derive it from the
 * remaining distance at free-flow speed.
 *
 * Returns null when NEITHER input exists — a registered device is a
 * registration, not a position fix, so there is no distance to derive from. The
 * caller renders null as "—". Deriving one anyway would have printed "<1 min"
 * from a null `remaining_km` coerced to 0.
 */
export function etaSeconds(truck: TruckDevice): number | null {
  if (truck.eta_s != null) return truck.eta_s;
  if (truck.remaining_km == null) return null;
  return (truck.remaining_km / FREE_FLOW_KMH) * 3600;
}

/** Remaining distance for display, or null when it was never measured. */
export function remainingKmLabel(truck: TruckDevice): string {
  return truck.remaining_km == null ? "—" : `${truck.remaining_km.toFixed(1)} km`;
}

/**
 * Case-insensitive match over the identifiers an operator would search by, so a
 * driver who has just signed in can be found without knowing whether the
 * simulator happens to have them queueing.
 */
export function matchesQuery(truck: TruckDevice, needle: string): boolean {
  const q = (needle || "").trim().toLowerCase();
  if (!q) return true;
  return [truck.device_id, truck.plate, truck.driver_name, truck.driver_id].some((v) =>
    (v ?? "").toLowerCase().includes(q),
  );
}
