/**
 * The Evidence & Audit Explorer's honesty contract.
 *
 * Every case here corresponds to a way the screen could quietly overstate what
 * JNPA supplied. The values are the real ones measured against jnpa_schema_v3 on
 * 17-Aug-2026 (see audit/corpus_thread_2026-08-16/01_GOLDEN_THREAD_IMPORT.md).
 */
import { describe, expect, it } from "vitest";
import {
  describeVehicleAttribution,
  verdictLabel,
  verdictTone,
} from "@/lib/thread";
import type { ThreadVehicle } from "@/lib/types";

const vehicle = (o: Partial<ThreadVehicle>): ThreadVehicle => ({
  plate: "MH00A0000",
  provenance: null,
  assumption_ref: null,
  source_ref: null,
  transporter: null,
  driver_name: null,
  driver_licence: null,
  ...o,
});

describe("hop verdicts", () => {
  it('never lets "we could not look" read as "there is nothing there"', () => {
    // The distinction the whole screen rests on: ERROR is a fault in OUR query,
    // NOT_IN_CORPUS is a finding about JNPA's data. One bad column once made
    // every later hop report the wrong one of these.
    expect(verdictLabel("ERROR")).not.toBe(verdictLabel("NOT_IN_CORPUS"));
    expect(verdictTone("ERROR")).not.toBe(verdictTone("NOT_IN_CORPUS"));
  });

  it("tones only a genuine find as ok", () => {
    expect(verdictTone("FOUND")).toBe("ok");
    expect(verdictTone("NOT_IN_CORPUS")).toBe("neutral");
    expect(verdictTone("ERROR")).toBe("warn");
  });

  it("shows an unrecognised verdict verbatim instead of guessing", () => {
    expect(verdictLabel("SOMETHING_NEW")).toBe("SOMETHING_NEW");
    expect(verdictTone("SOMETHING_NEW")).not.toBe("ok");
  });
});

describe("truck attribution", () => {
  it("names the document when the transporter is evidenced", () => {
    // MH43BX1488 / BABALU KUMAR — three EIRs carry this bridge.
    const { label, tone } = describeVehicleAttribution(
      vehicle({
        plate: "MH43BX1488",
        provenance: "DOCUMENT_EVIDENCED",
        source_ref: "eir1_psa_bmct+eir3_gateway_maersk+eir4_gateway_one",
        transporter: "TRANSTAR HANDLING & WAREHOUSING CO",
        driver_name: "BABALU KUMAR",
      }),
    );
    expect(tone).toBe("ok");
    expect(label).toContain("eir1_psa_bmct");
  });

  it("marks an assumed transporter as assumed, and cites the assumption", () => {
    // MH46H6948 comes off the DPWU9011100 CODECO message, so the PLATE is real;
    // only the transporter behind it is inferred (A-G6). The chip must say so.
    const { label, tone } = describeVehicleAttribution(
      vehicle({
        plate: "MH46H6948",
        provenance: "SYNTHETIC",
        assumption_ref: "A-G6",
        transporter: "SHRI VAISHNAVI LOGISTICS SOLUTIONS",
      }),
    );
    expect(tone).toBe("warn");
    expect(label).toMatch(/assumed/i);
    expect(label).toContain("A-G6");
    // and it must never imply a document backs it
    expect(label).not.toMatch(/from .*document/i);
  });

  it("says unmapped when no transporter resolves at all", () => {
    // The common case: defect B1 — no vehicle-registration column exists in
    // 11-Transport Data, so most plates resolve to nothing.
    const { label, tone } = describeVehicleAttribution(vehicle({ plate: "MH43CK1959" }));
    expect(label).toBe("unmapped");
    expect(tone).toBe("warn");
  });

  it("does not treat a missing provenance as evidence", () => {
    const { tone } = describeVehicleAttribution(
      vehicle({ transporter: "SOME LOGISTICS", provenance: null }),
    );
    expect(tone).not.toBe("ok");
  });
});
