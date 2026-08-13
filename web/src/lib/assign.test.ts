import { describe, expect, it } from "vitest";

import { autoSelect, boundDriverFor, dedupeBy, driverIdentity, vehicleIdentity } from "./assign";
import type { ActiveDriver } from "./api";
import type { AvailableVehicle } from "./types";

const veh = (
  id: string,
  plate: string,
  driver_id?: string,
  driver_licence?: string,
): AvailableVehicle => ({
  vehicle_id: id,
  vehicle_number: plate,
  plate,
  driver_id: driver_id ?? null,
  driver_licence: driver_licence ?? null,
});
const drv = (id: string, name: string, license_no?: string): ActiveDriver => ({
  driver_id: id,
  name,
  license_no: license_no ?? null,
});

// The lists below stand for what the AVAILABILITY endpoints returned. The
// backend has already excluded ACTIVE-but-occupied resources, so an occupied one
// simply is not here — which is what makes "pick the first" safe.
const AVAILABLE_VEHICLES = [
  veh("TRK-1", "MH04DV0411"),
  veh("TRK-2", "MH04EC0548"),
  veh("TRK-3", "MH04EJ0685"),
];
const AVAILABLE_DRIVERS = [
  drv("DRV-1", "AABHIMAN BATULE"),
  drv("DRV-2", "AABID KHAN"),
  drv("DRV-3", "AADARSH GOSHWAMI"),
];

describe("identity", () => {
  it("identifies a driver by licence, not by name", () => {
    // Two records, one person: same licence punctuated differently.
    expect(driverIdentity(drv("DRV-7", "AAKIL KHAN", "MH01 20100095262"))).toBe(
      driverIdentity(drv("DRV-9", "AAKIL KHAN", "mh0120100095262")),
    );
    // Same name, different people -> different identities.
    expect(driverIdentity(drv("DRV-1", "AAKIL KHAN", "MH0120100095262"))).not.toBe(
      driverIdentity(drv("DRV-2", "AAKIL KHAN", "UP6420140008203")),
    );
  });

  it("falls back to the id when there is no licence on file", () => {
    expect(driverIdentity(drv("DRV-5", "No Licence"))).toBe("DRV-5");
  });

  it("identifies a vehicle by registration, not by Vehicle ID", () => {
    expect(vehicleIdentity(veh("TRK-1", "MH04QA9911"))).toBe(
      vehicleIdentity(veh("TRK-2", "mh04 qa 9911")),
    );
  });
});

describe("dedupeBy", () => {
  it("collapses the duplicate driver records the roster carries", () => {
    // The reported symptom: one person, three identical-looking options.
    const rows = [
      drv("DRV-7", "AAKIL KHAN", "MH01 20100095262"),
      drv("DRV-8", "AAKIL KHAN", "MH01 20100095262"),
      drv("DRV-9", "AAKIL KHAN", "MH01 20100095262"),
      drv("DRV-2", "AABID KHAN", "UP6420140008203"),
    ];
    const out = dedupeBy(rows, driverIdentity);
    expect(out.map((d) => d.driver_id)).toEqual(["DRV-7", "DRV-2"]);
  });

  it("keeps the first record and the API's ordering", () => {
    expect(dedupeBy(AVAILABLE_DRIVERS, driverIdentity)).toEqual(AVAILABLE_DRIVERS);
  });

  it("never drops distinct resources", () => {
    expect(dedupeBy(AVAILABLE_VEHICLES, vehicleIdentity)).toHaveLength(3);
  });
});

describe("autoSelect", () => {
  it("selects the first available vehicle and driver", () => {
    expect(autoSelect(AVAILABLE_VEHICLES, AVAILABLE_DRIVERS)).toEqual({
      vehicleId: "TRK-1", // MH04DV0411
      driverId: "DRV-1", // AABHIMAN BATULE
    });
  });

  it("cannot select a vehicle the availability API did not return", () => {
    // MH04QA9911 is occupied, so it is absent from the list. Whatever else
    // happens, the selection is one of the three the backend offered.
    const picked = autoSelect(AVAILABLE_VEHICLES, AVAILABLE_DRIVERS).vehicleId;
    expect(AVAILABLE_VEHICLES.map((v) => v.vehicle_id)).toContain(picked);
  });

  it("cannot select a driver the availability API did not return", () => {
    const picked = autoSelect(AVAILABLE_VEHICLES, AVAILABLE_DRIVERS).driverId;
    expect(AVAILABLE_DRIVERS.map((d) => d.driver_id)).toContain(picked);
  });

  it("prefers the driver bound to the chosen truck when they are free", () => {
    const vehicles = [veh("TRK-1", "MH04DV0411", "DRV-3")];
    expect(autoSelect(vehicles, AVAILABLE_DRIVERS).driverId).toBe("DRV-3");
  });

  it("will NOT select a bound driver who is out on another truck's job", () => {
    // DRV-OCCUPIED is bound to this truck in core.driver_identity but holds an
    // open job, so the availability response omits them. The old panel spliced
    // such a driver back in; auto-selection must fall through instead.
    const vehicles = [veh("TRK-1", "MH04DV0411", "DRV-OCCUPIED")];
    const { driverId } = autoSelect(vehicles, AVAILABLE_DRIVERS);
    expect(driverId).toBe("DRV-1");
    expect(driverId).not.toBe("DRV-OCCUPIED");
  });

  it("keeps a selection the operator has already made", () => {
    expect(
      autoSelect(AVAILABLE_VEHICLES, AVAILABLE_DRIVERS, {
        vehicleId: "TRK-3",
        driverId: "DRV-2",
      }),
    ).toEqual({ vehicleId: "TRK-3", driverId: "DRV-2" });
  });

  it("drops a kept selection that has since become occupied", () => {
    // The truck the operator had chosen is no longer in the availability
    // response, so it is replaced rather than left selected and rejected on POST.
    expect(autoSelect(AVAILABLE_VEHICLES, AVAILABLE_DRIVERS, { vehicleId: "TRK-GONE" })).toEqual({
      vehicleId: "TRK-1",
      driverId: "DRV-1",
    });
  });

  it("selects nothing when nothing is available", () => {
    expect(autoSelect([], [])).toEqual({ vehicleId: "", driverId: "" });
  });

  it("still selects a truck when no driver is free", () => {
    expect(autoSelect(AVAILABLE_VEHICLES, [])).toEqual({ vehicleId: "TRK-1", driverId: "" });
  });
});

describe("boundDriverFor", () => {
  const LICENCE = "MH01 20100095262";
  // The person is available — but under DRV-1B, not the DRV-9C the vehicle is
  // bound to. Both records are the same licence, so both are the same driver.
  const drivers = [drv("DRV-1B", "AAKIL KHAN", LICENCE), ...AVAILABLE_DRIVERS];

  it("resolves the bound driver by their record when that record is listed", () => {
    const vehicle = veh("TRK-1", "MH04DV0411", "DRV-1B", LICENCE);
    expect(boundDriverFor(vehicle, drivers)?.driver_id).toBe("DRV-1B");
  });

  it("resolves the bound PERSON when the list carries their other record", () => {
    // De-duplication keeps one record per licence, so the bound record is
    // routinely not the listed one. Matching on the Driver ID alone reported the
    // driver as busy and dispatched a stranger instead.
    const vehicle = veh("TRK-1", "MH04DV0411", "DRV-9C", LICENCE);
    expect(boundDriverFor(vehicle, drivers)?.driver_id).toBe("DRV-1B");
  });

  it("resolves nobody when the bound person is out on a job", () => {
    // Neither their record nor their licence is in the availability response.
    const vehicle = veh("TRK-1", "MH04DV0411", "DRV-BUSY", "UP99 20200000001");
    expect(boundDriverFor(vehicle, drivers)).toBeNull();
  });

  it("resolves nobody for a truck with no driver on file", () => {
    expect(boundDriverFor(veh("TRK-1", "MH04DV0411"), drivers)).toBeNull();
    expect(boundDriverFor(null, drivers)).toBeNull();
  });
});

describe("auto-select never reaches an occupied resource", () => {
  // Acceptance 10, stated as the property rather than a case: whatever the
  // availability response is, the selection is drawn FROM it. The occupied
  // resources below are simply absent from the lists, as the backend leaves them.
  const OCCUPIED_VEHICLE = "TRK-BUSY";
  const OCCUPIED_DRIVER = "DRV-BUSY";

  it("never selects an occupied vehicle, however the form is seeded", () => {
    for (const keep of [
      {},
      { vehicleId: OCCUPIED_VEHICLE },
      { vehicleId: OCCUPIED_VEHICLE, driverId: OCCUPIED_DRIVER },
    ]) {
      const { vehicleId } = autoSelect(AVAILABLE_VEHICLES, AVAILABLE_DRIVERS, keep);
      expect(vehicleId).not.toBe(OCCUPIED_VEHICLE);
      expect(AVAILABLE_VEHICLES.map((v) => v.vehicle_id)).toContain(vehicleId);
    }
  });

  it("never selects an occupied driver, not even the truck's own", () => {
    // The truck is bound to an occupied driver by BOTH id and licence.
    const vehicles = [veh("TRK-1", "MH04DV0411", OCCUPIED_DRIVER, "UP99 20200000001")];
    for (const keep of [{}, { driverId: OCCUPIED_DRIVER }]) {
      const { driverId } = autoSelect(vehicles, AVAILABLE_DRIVERS, keep);
      expect(driverId).not.toBe(OCCUPIED_DRIVER);
      expect(AVAILABLE_DRIVERS.map((d) => d.driver_id)).toContain(driverId);
    }
  });

  it("leaves the driver empty when the availability response is empty", () => {
    // "No available drivers" — the panel says so rather than selecting anyone.
    const vehicles = [veh("TRK-1", "MH04DV0411", OCCUPIED_DRIVER, "UP99 20200000001")];
    expect(autoSelect(vehicles, [])).toEqual({ vehicleId: "TRK-1", driverId: "" });
  });

  it("selects nothing at all when a search returns no available resource", () => {
    // Searching for an occupied truck returns [] from the availability endpoint
    // (the search runs inside the exclusion), so there is nothing to select.
    expect(autoSelect([], AVAILABLE_DRIVERS, { vehicleId: OCCUPIED_VEHICLE })).toEqual({
      vehicleId: "",
      driverId: "DRV-1",
    });
  });
});
