import { describe, expect, it } from "vitest";

import { assignableCount, isVehicleId, vehicleLabel } from "./vehicles";

describe("assignableCount", () => {
  it("prefers the backend's available_total over the page length", () => {
    // 28 in the master, 8 on open jobs -> the DB says 20, the page shows 5.
    expect(assignableCount(20, 5)).toBe(20);
  });

  it("falls back to the page length when the backend omits the total", () => {
    expect(assignableCount(undefined, 7)).toBe(7);
  });

  it("subtracts trucks that became busy after the dropdown was fetched", () => {
    expect(assignableCount(20, 20, 3)).toBe(17);
  });

  it("never goes negative", () => {
    expect(assignableCount(2, 2, 5)).toBe(0);
  });

  it("reports zero when nothing is assignable", () => {
    expect(assignableCount(0, 0)).toBe(0);
  });
});

describe("vehicle identity helpers", () => {
  it("recognises the Vehicle Master key format", () => {
    expect(isVehicleId("TRK-000031")).toBe(true);
    expect(isVehicleId("MH04DV3973")).toBe(false);
  });

  it("labels a vehicle by its registration, falling back to the ID", () => {
    expect(vehicleLabel({ vehicle_number: "MH04DV3973", vehicle_id: "TRK-000029" })).toBe(
      "MH04DV3973",
    );
    expect(vehicleLabel({ vehicle_id: "TRK-000029" })).toBe("TRK-000029");
  });
});
