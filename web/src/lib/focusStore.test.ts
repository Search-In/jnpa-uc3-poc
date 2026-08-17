// The focus store is the contract three separately-deployed apps share, so its
// URL grammar and its loop-prevention are pinned here. The golden-thread values
// are the real corpus ones (see PoC/audit/corpus_thread_2026-08-16).

import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  detectVesselKey,
  focusFromParams,
  focusQueryString,
  focusStore,
  focusToParams,
  hasWindow,
  windowParams,
  isEmptyFocus,
  sameFocus,
} from "./focusStore";

// T1, the only complete import chain in the corpus.
const T1 = {
  vcn: "INNSA1NS0S0552",
  viaNo: "S0552",
  imoNo: "9523017",
  vesselName: "XIN HANG ZHOU",
  containerNo: "DPWU9011100",
  vehicleNo: "MH46H6948",
  igmNo: "1194313",
};

beforeEach(() => focusStore.clear());

describe("focus state", () => {
  it("starts empty and reports it", () => {
    expect(isEmptyFocus(focusStore.get())).toBe(true);
  });

  it("set() replaces, so focusing a vessel drops the previous container", () => {
    focusStore.set({ vcn: T1.vcn, containerNo: T1.containerNo });
    focusStore.set({ vcn: "INNSA1NS0S0633" });
    const f = focusStore.get();
    expect(f.vcn).toBe("INNSA1NS0S0633");
    expect(f.containerNo).toBeUndefined();
  });

  it("refine() narrows within the current focus", () => {
    focusStore.set({ vcn: T1.vcn, vesselName: T1.vesselName });
    focusStore.refine({ containerNo: T1.containerNo });
    const f = focusStore.get();
    expect(f.vcn).toBe(T1.vcn);
    expect(f.vesselName).toBe(T1.vesselName);
    expect(f.containerNo).toBe(T1.containerNo);
  });

  it("upper-cases identifiers but preserves vessel name and asOf casing", () => {
    focusStore.set({
      containerNo: " dpwu9011100 ",
      vesselName: "Xin Hang Zhou",
      asOf: "2026-06-12T00:00:00+05:30",
    });
    const f = focusStore.get();
    expect(f.containerNo).toBe("DPWU9011100");
    expect(f.vesselName).toBe("Xin Hang Zhou");
    expect(f.asOf).toBe("2026-06-12T00:00:00+05:30");
  });

  it("drops blank values rather than storing empty strings", () => {
    focusStore.set({ vcn: T1.vcn, containerNo: "   " });
    expect(focusStore.get().containerNo).toBeUndefined();
  });

  it("bumps the nonce so re-selecting the same entity still fires consumers", () => {
    const a = focusStore.set({ vcn: T1.vcn });
    const b = focusStore.set({ vcn: T1.vcn });
    expect(b.nonce).toBeGreaterThan(a.nonce);
    expect(sameFocus(a, b)).toBe(true);
  });

  it("notifies subscribers", () => {
    const seen = vi.fn();
    const off = focusStore.subscribe(seen);
    focusStore.set({ vcn: T1.vcn });
    expect(seen).toHaveBeenCalled();
    off();
  });
});

describe("remote hand-off", () => {
  it("publishes local changes to registered transports", () => {
    const sent = vi.fn();
    const off = focusStore.onPublish(sent);
    focusStore.set({ vcn: T1.vcn }, "UC-1");
    expect(sent).toHaveBeenCalledTimes(1);
    expect(sent.mock.calls[0][0].vcn).toBe(T1.vcn);
    off();
  });

  it("does NOT re-publish a remote focus — that would loop between apps", () => {
    const sent = vi.fn();
    const off = focusStore.onPublish(sent);
    focusStore.applyRemote({ vcn: T1.vcn, origin: "UC-2", nonce: 7 });
    expect(focusStore.get().vcn).toBe(T1.vcn);
    expect(sent).not.toHaveBeenCalled();
    off();
  });

  it("ignores a remote focus identical to the current one", () => {
    focusStore.set({ vcn: T1.vcn });
    const before = focusStore.get().nonce;
    focusStore.applyRemote({ vcn: T1.vcn, origin: "UC-2", nonce: 99 });
    expect(focusStore.get().nonce).toBe(before);
  });
});

describe("URL grammar", () => {
  it("round-trips every identity field", () => {
    const qs = focusToParams(T1);
    expect(focusFromParams(qs)).toEqual(T1);
  });

  it("emits the documented parameter names", () => {
    const qs = focusQueryString({ vcn: T1.vcn, viaNo: T1.viaNo, containerNo: T1.containerNo });
    expect(qs).toBe("?vcn=INNSA1NS0S0552&via=S0552&container=DPWU9011100");
  });

  it("omits unset fields and returns an empty string for an empty focus", () => {
    expect(focusQueryString({})).toBe("");
  });

  it("reads from a plain record as well as URLSearchParams", () => {
    expect(focusFromParams({ vcn: T1.vcn, container: T1.containerNo })).toEqual({
      vcn: T1.vcn,
      containerNo: T1.containerNo,
    });
  });
});

describe("vessel key recognition", () => {
  it("recognises a full VCN and derives the short VIA from it", () => {
    expect(detectVesselKey("INNSA1NS0S0552")).toEqual({ vcn: "INNSA1NS0S0552", viaNo: "S0552" });
  });

  it("recognises a bare VIA and a terminal-prefixed one", () => {
    expect(detectVesselKey("S0552")).toEqual({ viaNo: "S0552" });
    expect(detectVesselKey("NTPS0633")).toEqual({ viaNo: "S0633" });
    expect(detectVesselKey("APLS0595")).toEqual({ viaNo: "S0595" });
  });

  it("recognises the 2025 Q-series and the Feb-Apr R-series", () => {
    expect(detectVesselKey("Q2806")).toEqual({ viaNo: "Q2806" });
    expect(detectVesselKey("R3436")).toEqual({ viaNo: "R3436" });
  });

  it("only accepts an IMO when it is labelled as one", () => {
    expect(detectVesselKey("IMO 9523017")).toEqual({ imoNo: "9523017" });
    // A bare 7-digit number is ambiguous: 4339869 is an EIR number, not an IMO.
    // Claiming it here would break the existing gate-document detection.
    expect(detectVesselKey("4339869")).toBeNull();
    expect(detectVesselKey("9523017")).toBeNull();
  });

  it("does not claim containers or plates", () => {
    expect(detectVesselKey("DPWU9011100")).toBeNull();
    expect(detectVesselKey("MH46H6948")).toBeNull();
  });
});

// --- date window -----------------------------------------------------------
// The corpus is a set of disjoint time-slices, so a window that silently fails
// to apply is worse than no window: two panels then show different weeks and
// look comparable. These pin the wire names and the verbatim handling.
describe("date window", () => {
  it("round-trips through the URL under the API's own parameter names", () => {
    const qs = focusQueryString({ fromDate: "2026-06-06", toDate: "2026-06-12" });
    expect(qs).toBe("?from_date=2026-06-06&to_date=2026-06-12");
    expect(focusFromParams(new URLSearchParams(qs))).toEqual({
      fromDate: "2026-06-06",
      toDate: "2026-06-12",
    });
  });

  it("never upper-cases a date or the replay instant", () => {
    focusStore.set({ fromDate: "2026-06-06", asOf: "2026-06-12T00:00:00+05:30" });
    const f = focusStore.get();
    expect(f.fromDate).toBe("2026-06-06");
    expect(f.asOf).toBe("2026-06-12T00:00:00+05:30");
  });

  it("survives a refine() that narrows the entity", () => {
    focusStore.set({ vcn: T1.vcn, fromDate: "2026-06-06", toDate: "2026-06-12" });
    focusStore.refine({ containerNo: T1.containerNo });
    const f = focusStore.get();
    expect(f.fromDate).toBe("2026-06-06");
    expect(f.toDate).toBe("2026-06-12");
    expect(f.containerNo).toBe(T1.containerNo);
  });

  it("reports whether a window is set", () => {
    expect(hasWindow({})).toBe(false);
    expect(hasWindow({ fromDate: "2026-06-06" })).toBe(true);
    expect(hasWindow({ toDate: "2026-06-12" })).toBe(true);
  });

  it("emits request parameters an endpoint accepts verbatim", () => {
    expect(windowParams({ fromDate: "2026-06-06", toDate: "2026-06-12" }))
      .toEqual({ from_date: "2026-06-06", to_date: "2026-06-12" });
    expect(windowParams({})).toEqual({});
  });
});
