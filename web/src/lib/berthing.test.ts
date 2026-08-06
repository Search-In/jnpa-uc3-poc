// Unit test for the Berthing blank-value classifier. Pure logic, no network — this is
// the machine-checked statement of WHEN the Berthing UI is allowed to say "Anomaly".
//
// The fixtures are lifted from the real corpus in client-data/7-Berthing Reports so the
// regressions this guards against are the ones the terminals actually produce.

import { describe, expect, it } from "vitest";
import {
  arrivalWatch,
  callAnomalies,
  classifyField,
  statusRank,
  type BerthingRow,
} from "./berthing";

const NOW = Date.parse("2026-06-09T23:59:00+05:30");

/** NSICT, "VESSELS EXPECTED" section — schedule published, nothing else known yet. */
const expected: BerthingRow = {
  terminal: "NSICT",
  vessel_name: "XIN HANG ZHOU",
  voyage_number: "S0552",
  status: "EXPECTED",
  eta: "2026-06-06T18:00:00+05:30",
};

/** BMCT "BMCT04 JOLLY BIANCO S0605 283 04-Jun PUP@06:30" — berth pre-allocated, pilot
 *  pick-up booked, vessel not yet alongside. */
const berthAssignedPreArrival: BerthingRow = {
  terminal: "BMCT",
  vessel_name: "JOLLY BIANCO",
  voyage_number: "S0605",
  status: "BERTH_ASSIGNED",
  berth_number: "BMCT04",
};

/** NSIGT sailed row — the full actual sequence is published. */
const departed: BerthingRow = {
  terminal: "NSIGT",
  vessel_name: "APL HOLLAND",
  voyage_number: "S0595",
  status: "DEPARTED",
  berth_number: "CB06",
  ata: "2026-06-07T21:48:00+05:30",
  berthing_time: "2026-06-07T21:48:00+05:30",
  cargo_operation_start: "2026-06-07T22:19:00+05:30",
  cargo_operation_end: "2026-06-08T12:45:00+05:30",
  departure_time: "2026-06-08T13:40:00+05:30",
};

describe("statusRank", () => {
  it("orders the lifecycle and rejects unknowns", () => {
    expect(statusRank("EXPECTED")).toBeLessThan(statusRank("DEPARTED"));
    expect(statusRank("CARGO_OPERATION")).toBeGreaterThan(statusRank("BERTH_ASSIGNED"));
    expect(statusRank(null)).toBe(-1);
    expect(statusRank("NOT_A_STATUS")).toBe(-1);
  });
});

describe("callAnomalies — stays quiet on legitimate operational states", () => {
  it("clears a clean expected call", () => {
    expect(callAnomalies(expected)).toEqual([]);
  });

  it("clears a clean departed call", () => {
    expect(callAnomalies(departed)).toEqual([]);
  });

  // The brief proposed "berth assigned but operational timestamps missing" as an anomaly
  // rule. BMCT publishes exactly that shape for pre-arrival berth allocation, so the rule
  // would be wrong on real data. ATA is due at BERTHING_STARTED, not BERTH_ASSIGNED.
  it("does NOT flag a berth allocated before the vessel arrives", () => {
    expect(callAnomalies(berthAssignedPreArrival)).toEqual([]);
  });

  // Likewise "ETA passed but ATA unavailable" — 55% of real calls match it.
  it("does NOT flag an expected vessel whose ETA has passed", () => {
    expect(callAnomalies({ ...expected, eta: "2026-05-23T03:30:00+05:30" })).toEqual([]);
  });

  it("does not demand fields the terminal never publishes", () => {
    // NSFT reports carry no berth column; APMT/BMCT publish no ops-completed time.
    expect(callAnomalies({ ...departed, terminal: "NSFT", berth_number: null })).toEqual([]);
    expect(callAnomalies({ ...departed, terminal: "BMCT", cargo_operation_end: null })).toEqual([]);
  });
});

describe("callAnomalies — fires on genuine business-rule violations", () => {
  it("flags a mandatory field missing after import", () => {
    const found = callAnomalies({ ...expected, vessel_name: "  " });
    expect(found).toHaveLength(1);
    expect(found[0]).toMatchObject({ field: "vessel_name", code: "mandatory_missing" });
  });

  // BMCT ZULFA 1 S0560: ata 04-Jun 22:00, departure 06-May 01:12.
  it("flags ATD before ATA", () => {
    const found = callAnomalies({
      ...departed,
      terminal: "BMCT",
      ata: "2026-06-04T22:00:00+05:30",
      departure_time: "2026-05-06T01:12:00+05:30",
      cargo_operation_end: null,
    });
    expect(found.some((a) => a.code === "sequence_invalid" && a.field === "departure_time")).toBe(
      true,
    );
  });

  // BMCT GFS JUNO S0571: alongside 03-Jun 23:45, ops commenced printed as 03-Jun 00:38.
  it("flags ops commenced before arrival", () => {
    const found = callAnomalies({
      terminal: "BMCT",
      vessel_name: "GFS JUNO",
      voyage_number: "S0571",
      status: "CARGO_OPERATION",
      berth_number: "BMCT05",
      ata: "2026-06-03T23:45:00+05:30",
      berthing_time: "2026-06-03T23:45:00+05:30",
      cargo_operation_start: "2026-06-03T00:38:00+05:30",
    });
    expect(found).toHaveLength(1);
    expect(found[0]).toMatchObject({ code: "sequence_invalid", field: "cargo_operation_start" });
  });

  it("flags a milestone missing once the call has passed it", () => {
    const found = callAnomalies({ ...departed, departure_time: null });
    expect(found).toHaveLength(1);
    expect(found[0]).toMatchObject({ field: "departure_time", code: "milestone_missing" });
  });
});

describe("classifyField", () => {
  it("passes a present value straight through", () => {
    expect(classifyField(departed, "ata").state).toBe("value");
  });

  it("calls a not-yet-reached milestone Pending", () => {
    const v = classifyField(expected, "ata");
    expect(v.state).toBe("pending");
    expect(v.label).toBe("Pending");
    expect(v.hint).toContain("berthing started");
  });

  it("calls an unallocated berth Not allocated", () => {
    const v = classifyField(expected, "berth_number");
    expect(v.state).toBe("pending");
    expect(v.label).toBe("Not allocated");
  });

  it("calls a field the layout never carries Not reported", () => {
    // No JNPA terminal publishes IMO in the daily berthing report.
    expect(classifyField(expected, "imo_number")).toMatchObject({
      state: "not-reported",
      label: "Not reported",
    });
    // NSFT has no berth column at all — not "pending", it will never arrive.
    expect(
      classifyField({ ...departed, terminal: "NSFT", berth_number: null }, "berth_number"),
    ).toMatchObject({ state: "not-reported" });
  });

  it("explains a missing ETA on a berthed call rather than calling it pending", () => {
    const v = classifyField({ ...departed, eta: null }, "eta");
    expect(v.state).toBe("not-reported");
    expect(v.hint).toContain("expected");
  });

  it("surfaces Anomaly only where a rule is violated", () => {
    expect(classifyField({ ...departed, departure_time: null }, "departure_time")).toMatchObject({
      state: "anomaly",
      label: "Anomaly",
      tone: "critical",
    });
    // ...and leaves the sibling fields of the same row untouched.
    expect(classifyField({ ...departed, departure_time: null }, "ata").state).toBe("value");
  });
});

describe("arrivalWatch — graduated freshness, never an anomaly", () => {
  it("says nothing when the ETA is still in the future", () => {
    expect(arrivalWatch({ ...expected, eta: "2026-06-20T10:00:00+05:30" }, NOW)).toBeNull();
  });

  it("says nothing once the vessel has arrived", () => {
    expect(arrivalWatch({ ...expected, ata: "2026-06-07T10:00:00+05:30" }, NOW)).toBeNull();
  });

  it("treats a few hours past ETA as routine", () => {
    const w = arrivalWatch({ ...expected, eta: "2026-06-09T12:00:00+05:30" }, NOW);
    expect(w).toMatchObject({ level: "awaiting", tone: "neutral" });
  });

  it("escalates to Overdue after a day", () => {
    const w = arrivalWatch({ ...expected, eta: "2026-06-08T06:00:00+05:30" }, NOW);
    expect(w).toMatchObject({ level: "overdue", tone: "warn" });
  });

  it("marks a long-stale expected row Unconfirmed, not Anomaly", () => {
    const stale = { ...expected, eta: "2026-05-23T03:30:00+05:30" };
    expect(arrivalWatch(stale, NOW)).toMatchObject({ level: "unconfirmed", tone: "warn" });
    expect(callAnomalies(stale)).toEqual([]);
  });
});
