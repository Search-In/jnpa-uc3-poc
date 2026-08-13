// Congestion Rerouting rows: the simulator's measured queue vs the devices a
// real driver is signed in on.
//
// The console shows both. The rule these tests pin is that it never prints a
// figure nobody measured: a registered device carries no state, ETA, distance or
// gate, and the honest rendering of that is "—". The previous helper derived an
// ETA from `remaining_km`, so a null distance would have surfaced as "<1 min" —
// a live-looking measurement invented from nothing.

import { describe, expect, it } from "vitest";
import {
  PWA_SOURCE,
  SIM_SOURCE,
  etaSeconds,
  isRegisteredDevice,
  matchesQuery,
  remainingKmLabel,
} from "./advisoryRows";
import type { TruckDevice } from "./types";

// A synthetic simulator truck, measured to be queueing at a gate.
const simTruck: TruckDevice = {
  device_id: "TRK-000014",
  plate: "KL07WB9662",
  gate_id: "G-JNPCT",
  state: "AT_GATE_QUEUE",
  position: { lat: 18.95, lon: 72.95 },
  speed_kmh: 0,
  heading: 90,
  remaining_km: 0,
  eta_s: 1500,
  source: SIM_SOURCE,
};

// A device a driver is signed in on: real, but nothing about a queue was
// measured for it. This is exactly the shape the gateway sends.
const pwaDevice: TruckDevice = {
  device_id: "TRK-000026",
  plate: "MH04QA9911",
  gate_id: null,
  state: null,
  position: null,
  speed_kmh: null,
  heading: null,
  remaining_km: null,
  eta_s: null,
  source: PWA_SOURCE,
  driver_id: "DRV-0001",
  driver_name: "A. Driver",
  last_seen: null,
};

describe("provenance", () => {
  it("tells a registered driver device apart from a simulator truck", () => {
    expect(isRegisteredDevice(pwaDevice)).toBe(true);
    expect(isRegisteredDevice(simTruck)).toBe(false);
  });

  it("does not treat an unlabelled row as a registered device", () => {
    expect(isRegisteredDevice({ ...simTruck, source: undefined })).toBe(false);
  });
});

describe("etaSeconds", () => {
  it("uses the live ETA when the simulator supplied one", () => {
    expect(etaSeconds(simTruck)).toBe(1500);
  });

  it("derives an ETA from a MEASURED remaining distance", () => {
    // 55 km at free-flow 55 km/h -> one hour.
    expect(etaSeconds({ ...simTruck, eta_s: null, remaining_km: 55 })).toBeCloseTo(3600);
  });

  it("returns null — never a derived figure — for a registered device", () => {
    // THE REGRESSION: `remaining_km` is null, and the old helper divided it as
    // if it were 0, producing an ETA of 0 s that rendered as "<1 min".
    expect(etaSeconds(pwaDevice)).toBeNull();
  });

  it("still reports a genuine zero distance as an imminent arrival", () => {
    // 0 km measured is a MEASUREMENT and must not be confused with "unknown".
    expect(etaSeconds({ ...simTruck, eta_s: null, remaining_km: 0 })).toBe(0);
  });
});

describe("remainingKmLabel", () => {
  it("renders an unmeasured distance as an em dash", () => {
    expect(remainingKmLabel(pwaDevice)).toBe("—");
  });

  it("renders a measured zero as 0.0 km, not as unknown", () => {
    expect(remainingKmLabel(simTruck)).toBe("0.0 km");
  });

  it("renders a measured distance", () => {
    expect(remainingKmLabel({ ...simTruck, remaining_km: 12.34 })).toBe("12.3 km");
  });
});

describe("matchesQuery", () => {
  it("matches nothing away when the box is empty", () => {
    expect(matchesQuery(pwaDevice, "")).toBe(true);
    expect(matchesQuery(pwaDevice, "   ")).toBe(true);
  });

  it("finds a signed-in driver by device id, case-insensitively", () => {
    expect(matchesQuery(pwaDevice, "trk-000026")).toBe(true);
    expect(matchesQuery(pwaDevice, "000026")).toBe(true);
  });

  it("finds them by plate or by driver", () => {
    expect(matchesQuery(pwaDevice, "MH04QA")).toBe(true);
    expect(matchesQuery(pwaDevice, "a. driver")).toBe(true);
    expect(matchesQuery(pwaDevice, "DRV-0001")).toBe(true);
  });

  it("does not match an unrelated device", () => {
    expect(matchesQuery(pwaDevice, "TRK-000014")).toBe(false);
  });

  it("tolerates rows with no plate or driver", () => {
    const bare = { ...pwaDevice, plate: null, driver_id: null, driver_name: null };
    expect(matchesQuery(bare, "TRK-000026")).toBe(true);
    expect(matchesQuery(bare, "anything")).toBe(false);
  });
});
