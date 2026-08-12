// Assign-a-Job selection logic: which options the form may offer, and what it
// pre-selects. Pure functions, so the rules are testable without a DOM and the
// component stays a renderer.
//
// The one invariant everything here rests on: the ONLY source of selectable
// resources is the availability API response (/api/vehicles/available,
// /api/identity/drivers/available), which the backend has already restricted to
// ACTIVE + no open container job. Nothing in this module may add a resource the
// backend did not return — auto-selection picks FROM the list, never around it,
// so an occupied vehicle or driver cannot be chosen even by accident.

import type { ActiveDriver } from "./api";
import type { AvailableVehicle } from "./types";

/** A driving licence identifies the person; a Driver ID identifies a RECORD. */
export function driverIdentity(d: Pick<ActiveDriver, "driver_id" | "license_no">): string {
  const licence = (d.license_no ?? "").toUpperCase().replace(/[^A-Z0-9]/g, "");
  return licence || d.driver_id;
}

/** A registration identifies the truck; a Vehicle ID identifies a RECORD. */
export function vehicleIdentity(
  v: Pick<AvailableVehicle, "vehicle_id" | "vehicle_number">,
): string {
  const plate = (v.vehicle_number ?? "").toUpperCase().replace(/[^A-Z0-9]/g, "");
  return plate || v.vehicle_id;
}

/**
 * Collapse records that describe the same physical resource to one option.
 *
 * The backend already de-duplicates (SELECT DISTINCT ON the same identity), so
 * this is belt-and-braces for a page assembled from more than one response —
 * never a substitute for it, and never a filter that could hide an available
 * resource: the FIRST record for each identity is kept, in the order the API
 * returned them.
 */
export function dedupeBy<T>(items: T[], identity: (item: T) => string): T[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    const key = identity(item);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/**
 * The available driver record that IS the person bound to this truck, or null.
 *
 * The binding names a RECORD (core.driver_identity is keyed on driver_id), while
 * the availability list carries one record per PERSON — so the bound record is
 * frequently not the listed one even though the driver is free. Matching on the
 * Driver ID alone then reports a free driver as busy, and the console falls back
 * to a stranger. The licence is the person, so it is tried second.
 *
 * Returns only what the availability response contains: a driver who is out on a
 * job is not in `drivers`, so this cannot resolve to them.
 */
export function boundDriverFor(
  vehicle: Pick<AvailableVehicle, "driver_id" | "driver_licence"> | null | undefined,
  drivers: ActiveDriver[],
): ActiveDriver | null {
  if (!vehicle) return null;
  const byId = vehicle.driver_id
    ? (drivers.find((d) => d.driver_id === vehicle.driver_id) ?? null)
    : null;
  if (byId) return byId;
  const identity = (vehicle.driver_licence ?? "").toUpperCase().replace(/[^A-Z0-9]/g, "");
  if (!identity) return null;
  return drivers.find((d) => driverIdentity(d) === identity) ?? null;
}

export interface AutoSelection {
  vehicleId: string;
  driverId: string;
}

/**
 * What the form should hold once a container is chosen: the first genuinely
 * available truck, and the driver to go with it.
 *
 * The driver preference is the one the console already had — the driver bound to
 * the chosen truck (core.driver_identity), so the person who can actually sign
 * into the PWA on that vehicle is the one dispatched — but ONLY when that driver
 * is in the available list (see `boundDriverFor`, which matches the person, not
 * the record). A bound driver who is out on another truck's job falls through to
 * the first available driver instead of being selected, which is the difference
 * between "convenient" and "wrong".
 *
 * `keep` is the operator's current choice: it survives untouched as long as it
 * is still available, so re-rendering never fights a manual selection.
 */
export function autoSelect(
  vehicles: AvailableVehicle[],
  drivers: ActiveDriver[],
  keep: Partial<AutoSelection> = {},
): AutoSelection {
  const vehicle = vehicles.find((v) => v.vehicle_id === keep.vehicleId) ?? vehicles[0] ?? null;

  const freeDriver = (id: string | null | undefined) =>
    id ? (drivers.find((d) => d.driver_id === id) ?? null) : null;

  const driver =
    freeDriver(keep.driverId) ?? boundDriverFor(vehicle, drivers) ?? drivers[0] ?? null;

  return { vehicleId: vehicle?.vehicle_id ?? "", driverId: driver?.driver_id ?? "" };
}
