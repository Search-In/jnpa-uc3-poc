// Tests for the Combined-incident-report view model.
//
// The rules that matter here are not formatting rules. This module sits between
// a language-model paragraph and an operator who will act on it, so the
// assertions are: nothing is invented, nothing is silently dropped, a section
// with no data does not render at all, and the original narrative survives
// byte-for-byte for the "view full narrative" disclosure.

import { describe, expect, it } from "vitest";
import {
  buildCombinedReport,
  classifySegment,
  extractRiskAndAction,
  normalizeNarrative,
  riskTone,
  splitNarrative,
  type SvReportSectionKey,
} from "./svCombinedReport";
import type { SvCombinedReport, SvIncident } from "./securevision";

const CAMERA = {
  securevision_code: "CAM-01",
  jnpa_camera_id: "CAM-NSICT-ENT",
  mapped: true,
  map_configured: true,
};

function incident(overrides: Partial<SvIncident>): SvIncident {
  return {
    source: "SECUREVISION",
    analysis_id: "A1",
    incident_code: null,
    incident_type: null,
    title: null,
    fired: true,
    status: "SUCCESS",
    validation_status: "PASSED",
    confidence: 0.9,
    confidence_pct: 90,
    ocr_confidence: null,
    ocr_confidence_pct: null,
    track_id: 1,
    clip_offset_s: 1.5,
    detected_at: null,
    camera: CAMERA,
    image_url: null,
    evidence: [],
    description: null,
    ai_generated: false,
    vision_provider: null,
    processing_time_ms: null,
    facts: {},
    ...overrides,
  };
}

/** The shape the production UI shows: one long, unbroken paragraph. */
const NARRATIVE =
  "Analysis of the clip captured at the terminal gate identified one trailer entering the lane. " +
  "The trailer plate MH46BM3672 was read successfully with high OCR confidence, while a second " +
  "plate was partially occluded and remains unreadable. Vehicle classification counted 4 trucks " +
  "and 1 car across the sampled frames. One person was detected inside the restricted machinery " +
  "zone and could not be matched against the enrolled gallery, so the identity is unverified. " +
  "The camera feed was operational throughout with no blur, obstruction or black screen, and no " +
  "tampering was detected. Risk Level: MEDIUM. Priority: High. Recommended Action: Dispatch a " +
  "supervisor to verify the unreadable plate and escort the unidentified person out of the " +
  "machinery zone.";

function report(overrides: Partial<SvCombinedReport> = {}): SvCombinedReport {
  return {
    source: "SECUREVISION",
    analysis_id: "A1",
    camera: CAMERA,
    incidents: [],
    combined_description: NARRATIVE,
    ai_generated: true,
    narrative_provenance: "AI_GENERATED",
    ...overrides,
  };
}

const FULL_INCIDENTS: SvIncident[] = [
  incident({
    incident_code: "i01",
    incident_type: "I-01",
    ocr_confidence: 0.93,
    plate: {
      plate: "MH46BM3672",
      plate_valid: true,
      vehicle_type: "truck",
      vehicle_color: "white",
      validation: "PASSED",
      ocr_confidence: 0.93,
    },
  }),
  incident({
    incident_code: "i02",
    incident_type: "I-02",
    counts: [
      { vehicle_class: "truck", count: 4 },
      { vehicle_class: "car", count: 1 },
    ],
    total_count: 5,
  }),
  incident({
    incident_code: "i07",
    incident_type: "I-07",
    facts: { person_status: "UNVERIFIED", zone: "Machinery Zone 1" },
  }),
  incident({
    incident_code: "i09",
    incident_type: "I-09",
    container: {
      number: "MSKU3881250",
      vendor_valid: true,
      jnpa_valid: true,
      agreement: "MATCH",
      container_detected: true,
      validation: "PASSED",
      plate: "MH46BM3672",
      plate_detected: true,
    },
  }),
  incident({
    incident_code: "i12",
    incident_type: "I-12",
    fired: false,
    facts: { blur: false, obstruction: false, black_screen: false, camera_status: "OPERATIONAL" },
  }),
];

/** Every fragment a section shows must exist, contiguously, in the narrative. */
function isVerbatim(fragment: string, narrative: string): boolean {
  return normalizeNarrative(narrative).includes(fragment);
}

describe("narrative splitting", () => {
  it("breaks one paragraph into scannable fragments without editing the words", () => {
    const segments = splitNarrative(NARRATIVE);
    expect(segments.length).toBeGreaterThan(4);
    for (const segment of segments) expect(isVerbatim(segment, NARRATIVE)).toBe(true);
  });

  it("handles bullets, numbering and markdown emphasis the model may emit", () => {
    const segments = splitNarrative("- **Plate:** MH12AB1234\n1. Camera is blurred\n");
    expect(segments).toEqual(["Plate: MH12AB1234", "Camera is blurred"]);
  });

  it("returns nothing for an absent narrative", () => {
    expect(splitNarrative(null)).toEqual([]);
    expect(splitNarrative("   ")).toEqual([]);
  });
});

describe("segment routing", () => {
  const cases: [string, SvReportSectionKey | "risk" | "other"][] = [
    ["The trailer plate MH46BM3672 was read successfully", "vehicle"],
    ["Vehicle classification counted 4 trucks", "vehicle"],
    ["One person was detected inside the restricted machinery zone", "person"],
    ["The camera feed was operational with no blur or obstruction", "camera"],
    ["Recommended Action: Dispatch a supervisor", "risk"],
    ["Analysis completed at 11:04", "other"],
  ];
  it.each(cases)("routes %j to the %s section", (segment, topic) => {
    expect(classifySegment(segment)).toBe(topic);
  });
});

describe("risk and action", () => {
  it("reads the stated risk, priority and action verbatim", () => {
    const { risk, priority, action } = extractRiskAndAction(NARRATIVE);
    expect(risk?.value).toBe("MEDIUM");
    expect(priority?.value).toBe("High");
    expect(action).toBe(
      "Dispatch a supervisor to verify the unreadable plate and escort the unidentified person out of the machinery zone.",
    );
    expect(isVerbatim(action as string, NARRATIVE)).toBe(true);
  });

  it("reads unlabelled phrasing too", () => {
    const r = extractRiskAndAction("Overall risk is low and this is a routine priority item.");
    expect(r.risk?.value).toBe("low");
    expect(r.priority?.value).toBe("routine");
  });

  it("returns nulls rather than a default when the narrative states none", () => {
    expect(extractRiskAndAction("A white truck passed the gate.")).toEqual({
      risk: null,
      priority: null,
      action: null,
    });
    expect(extractRiskAndAction(null)).toEqual({ risk: null, priority: null, action: null });
  });

  it("colours the value without changing it", () => {
    expect(riskTone("HIGH")).toBe("critical");
    expect(riskTone("Medium")).toBe("warn");
    expect(riskTone("low")).toBe("ok");
    expect(riskTone("banana")).toBe("neutral");
    expect(riskTone(null)).toBe("neutral");
  });
});

describe("buildCombinedReport", () => {
  const view = buildCombinedReport(report({ incidents: FULL_INCIDENTS }));

  it("replaces the single paragraph with structured sections", () => {
    expect(view.sections.map((s) => s.key)).toEqual(["vehicle", "person", "camera"]);
    for (const section of view.sections) {
      expect(section.facts.length + section.notes.length).toBeGreaterThan(0);
    }
    // The summary is a short opener, not the whole paragraph.
    expect(view.summary!.length).toBeLessThan(NARRATIVE.length / 2);
  });

  it("preserves the narrative exactly for the full-text disclosure", () => {
    expect(view.narrative).toBe(NARRATIVE);
    expect(view.aiGenerated).toBe(true);
  });

  it("renders vehicle findings from the structured detection fields", () => {
    const vehicle = view.sections.find((s) => s.key === "vehicle")!;
    const facts = Object.fromEntries(vehicle.facts.map((f) => [f.label, f.value]));
    expect(facts["Plate"]).toBe("MH46BM3672");
    expect(facts["Plate valid"]).toBe("Valid");
    expect(facts["OCR confidence"]).toBe("93%");
    expect(facts["Vehicles counted"]).toBe("5");
    expect(facts["Classification"]).toBe("truck ×4, car ×1");
    expect(facts["Container"]).toBe("MSKU3881250");
    expect(facts["ISO 6346 cross-check"]).toBe("Validation: MATCH");
  });

  it("reports an unreadable plate as unreadable instead of blank", () => {
    const v = buildCombinedReport(
      report({
        incidents: [
          incident({
            incident_code: "i01",
            plate: {
              plate: null,
              plate_valid: null,
              vehicle_type: null,
              vehicle_color: null,
              validation: null,
              ocr_confidence: null,
            },
          }),
        ],
      }),
    );
    const plate = v.sections.find((s) => s.key === "vehicle")!.facts[0];
    expect(plate).toMatchObject({ label: "Plate", value: "Not readable", tone: "warn" });
  });

  it("renders restricted-zone findings and never upgrades UNVERIFIED", () => {
    const person = view.sections.find((s) => s.key === "person")!;
    const facts = Object.fromEntries(person.facts.map((f) => [f.label, f.value]));
    expect(facts["Person detections"]).toBe("1");
    expect(facts["Unverified"]).toBe("1");
    expect(facts).not.toHaveProperty("Unauthorized");
    expect(facts["Zone"]).toBe("Machinery Zone 1");
    // No identity is asserted when the response carries none.
    expect(facts).not.toHaveProperty("Identified");
  });

  it("shows identity only where the response already carries it", () => {
    const v = buildCombinedReport(
      report({
        incidents: [
          incident({
            incident_code: "i07",
            facts: { person_status: "AUTHORIZED", person_name: "Rahul Sharma" },
          }),
        ],
      }),
    );
    const facts = Object.fromEntries(
      v.sections.find((s) => s.key === "person")!.facts.map((f) => [f.label, f.value]),
    );
    expect(facts["Identified"]).toBe("Rahul Sharma");
    expect(facts["Authorized"]).toBe("1");
  });

  it("renders camera-health fields that exist and omits ones that do not", () => {
    const camera = view.sections.find((s) => s.key === "camera")!;
    const facts = Object.fromEntries(camera.facts.map((f) => [f.label, f.value]));
    expect(facts["AI tamper check"]).toBe("No tamper condition");
    expect(facts["Blur"]).toBe("Not detected");
    expect(facts["Obstruction"]).toBe("Not detected");
    expect(facts["Black screen"]).toBe("Not detected");
    expect(facts["Camera status"]).toBe("OPERATIONAL");
    // Nothing was reported about fogging or exposure, so nothing is shown.
    expect(facts).not.toHaveProperty("Fogging");
    expect(facts).not.toHaveProperty("Exposure issue");
    expect(facts).not.toHaveProperty("Frozen frame");
  });

  it("carries a fired tamper state with its confidence", () => {
    const v = buildCombinedReport(
      report({
        incidents: [
          incident({
            incident_code: "i12",
            fired: true,
            tamper: { tamper_state: "BLACK_FRAME", analytic_confidence_pct: 96.5 },
          }),
        ],
      }),
    );
    const facts = v.sections.find((s) => s.key === "camera")!.facts;
    expect(facts[0]).toMatchObject({
      label: "AI tamper check",
      value: "BLACK_FRAME",
      tone: "critical",
    });
    expect(facts[1]).toMatchObject({ label: "Analytic confidence", value: "96.5%" });
  });

  it("surfaces risk and action when present", () => {
    expect(view.riskAction?.risk?.value).toBe("MEDIUM");
    expect(view.riskAction?.risk?.tone).toBe("warn");
    expect(view.riskAction?.priority?.value).toBe("High");
    expect(view.riskAction?.action).toContain("Dispatch a supervisor");
  });

  it("hides risk, sections and summary rather than inventing them", () => {
    const empty = buildCombinedReport(
      report({ incidents: [], combined_description: null, ai_generated: false }),
    );
    expect(empty.sections).toEqual([]);
    expect(empty.riskAction).toBeNull();
    expect(empty.summary).toBeNull();
    expect(empty.narrative).toBeNull();
    expect(empty.other).toEqual([]);
    expect(buildCombinedReport(undefined).sections).toEqual([]);
  });

  it("keeps a section that has only narrative findings, and drops the others", () => {
    const v = buildCombinedReport(
      report({
        incidents: [],
        combined_description:
          "Analysis of the uploaded clip completed across all sampled frames at the terminal gate. " +
          "The camera lens was fogged for most of the clip.",
      }),
    );
    expect(v.sections.map((s) => s.key)).toEqual(["camera"]);
  });

  it("fabricates nothing — every displayed fragment comes from the narrative", () => {
    const fragments = [...view.sections.flatMap((s) => s.notes), ...view.other, view.summary!];
    for (const fragment of fragments) expect(isVerbatim(fragment, NARRATIVE)).toBe(true);
    if (view.riskAction?.action) expect(isVerbatim(view.riskAction.action, NARRATIVE)).toBe(true);
  });

  it("drops no narrative fragment — each one is shown somewhere", () => {
    const shown = new Set([
      ...view.sections.flatMap((s) => s.notes),
      ...view.other,
      ...splitNarrative(view.summary),
    ]);
    for (const segment of splitNarrative(NARRATIVE)) {
      const stated =
        shown.has(segment) ||
        /risk|priority|recommended action/i.test(segment) ||
        Boolean(view.riskAction?.action && segment.includes(view.riskAction.action));
      expect(stated, `fragment not shown anywhere: ${segment}`).toBe(true);
    }
  });

  it("passes the incident rows through untouched for the detail table", () => {
    expect(view.incidents).toEqual(FULL_INCIDENTS);
  });
});
