// Vehicle Number is the operator-facing identity of a truck — "MH04QA9911", the
// registration painted on the vehicle. `TRK-000031` is the Vehicle Master's
// internal key: it is what every API is addressed by, and what NOBODY at the gate,
// in the control room, or in the cab recognises.
//
// The two live in different places. A driver-identity record (core.driver_identity,
// core.driver_enrollment) stores only the Vehicle ID in its `vehicle_no` column —
// the registration lives on the Vehicle Master row. So a screen that renders an
// ASSIGNED vehicle has an ID and needs the number, and this module is the one
// place that resolves it, from the vehicle list the Vehicle Master screen already
// loads (`/api/vehicles`, query key ["fleet-vehicles"] — shared cache, no extra
// request when both are open).
//
// DISPLAY LAYER ONLY. Nothing here changes what is sent to an API: callers keep
// passing the Vehicle ID they already hold and use the returned label purely to
// render it.

import { useQuery } from "@tanstack/react-query";

import { getAdapter } from "@/data";
import type { FleetVehicle } from "@/lib/types";

/** True for the Vehicle Master's internal key format, e.g. `TRK-000031`. */
export function isVehicleId(value?: string | null): boolean {
  return /^TRK-\d{6}$/i.test((value ?? "").trim());
}

/** Registration if the record carries one, else the internal ID (never blank). */
export function vehicleLabel(vehicle?: {
  vehicle_number?: string | null;
  plate?: string | null;
  vehicle_id?: string | null;
}): string {
  if (!vehicle) return "";
  const number = (vehicle.vehicle_number ?? vehicle.plate ?? "").trim();
  return number || (vehicle.vehicle_id ?? "").trim();
}

export interface VehicleNumbers {
  /** Vehicle ID -> registration number, for every ACTIVE master vehicle. */
  byId: Map<string, string>;
  /**
   * The label to SHOW for a Vehicle ID: its registration number when the master
   * knows one, otherwise the ID itself. Falling back to the ID is deliberate — an
   * unregistered or not-yet-loaded vehicle must still render something an operator
   * can quote to support, never an empty cell.
   */
  label: (vehicleId?: string | null) => string;
  /** The registration only — null when the master has no number for this ID. */
  numberOf: (vehicleId?: string | null) => string | null;
  isLoading: boolean;
}

/**
 * Resolve Vehicle IDs to the registration numbers operators read.
 *
 * One cached list read for the whole screen — never one request per row.
 */
export function useVehicleNumbers(): VehicleNumbers {
  const q = useQuery({
    queryKey: ["fleet-vehicles"],
    queryFn: () => getAdapter().vehicles(),
    staleTime: 60_000,
  });

  const byId = new Map<string, string>();
  for (const v of (q.data ?? []) as FleetVehicle[]) {
    const number = (v.vehicle_number ?? "").trim();
    if (v.vehicle_id && number) byId.set(v.vehicle_id.trim().toUpperCase(), number);
  }

  const numberOf = (vehicleId?: string | null): string | null => {
    const key = (vehicleId ?? "").trim().toUpperCase();
    return (key && byId.get(key)) || null;
  };

  return {
    byId,
    numberOf,
    label: (vehicleId) => numberOf(vehicleId) ?? (vehicleId ?? "").trim(),
    isLoading: q.isLoading,
  };
}
