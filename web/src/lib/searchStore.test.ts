// Adding "vessel" to the omnibox reorders detectEntity, and the rules overlap:
// a bare VIA like "S0552" also satisfies the loose plate pattern, and a 7-digit
// EIR number looks exactly like an IMO. These cases pin that the new rule does
// not steal keys the existing screens depend on.

import { describe, expect, it } from "vitest";
import { detectEntity } from "./searchStore";

describe("detectEntity — vessel keys", () => {
  it("recognises a full VCN", () => {
    expect(detectEntity("INNSA1NS0S0552")).toBe("vessel");
    expect(detectEntity("INNSA1GT0S0554")).toBe("vessel");
  });

  it("recognises a bare and a terminal-prefixed VIA", () => {
    expect(detectEntity("S0552")).toBe("vessel");
    expect(detectEntity("NTPS0633")).toBe("vessel");
    expect(detectEntity("APLS0595")).toBe("vessel");
  });

  it("recognises an explicitly labelled IMO", () => {
    expect(detectEntity("IMO 9523017")).toBe("vessel");
  });
});

describe("detectEntity — no regression on the existing entities", () => {
  it("still recognises ISO 6346 containers", () => {
    expect(detectEntity("DPWU9011100")).toBe("container");
    expect(detectEntity("MEDU1777575")).toBe("container");
    expect(detectEntity("NYKU4768188")).toBe("container");
  });

  it("still recognises truck plates", () => {
    expect(detectEntity("MH46H6948")).toBe("vehicle");
    expect(detectEntity("MH43BX1488")).toBe("vehicle");
    expect(detectEntity("MH43CK1959")).toBe("vehicle");
  });

  it("still recognises a driving licence", () => {
    expect(detectEntity("UP6420140008203")).toBe("driver");
  });

  it("still routes bare numeric gate-document keys to gateDoc", () => {
    // A 7-digit EIR number must NOT be mistaken for a 7-digit IMO.
    expect(detectEntity("4339869")).toBe("gateDoc");
    expect(detectEntity("16497850")).toBe("gateDoc"); // Form 13 e-gate no
    expect(detectEntity("230283")).toBe("gateDoc"); // PIN
    expect(detectEntity("9523017")).toBe("gateDoc"); // unlabelled: ambiguous, unchanged
  });

  it("still recognises alerts and cases", () => {
    expect(detectEntity("AL-1234")).toBe("alert");
    expect(detectEntity("CHALLAN99")).toBe("case");
  });
});
