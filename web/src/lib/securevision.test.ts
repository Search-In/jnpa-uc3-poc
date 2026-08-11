// SecureVision presentation-helper tests.
//
// The assertions that matter here are the ones that encode product rules rather
// than formatting: UNVERIFIED must never render as an accusation, an unmapped
// camera must never be presented as a JNPA camera, and each vendor failure code
// must produce copy an operator can act on.

import { describe, expect, it } from "vitest";
import {
  cameraHint,
  cameraLabel,
  containerAgreementLabel,
  containerAgreementTone,
  fmtClipOffset,
  fmtConfidence,
  fmtDwell,
  incidentTitle,
  isAnalysisExpired,
  isNotConfigured,
  personVerdictHint,
  personVerdictLabel,
  personVerdictTone,
  svErrorMessage,
  tamperTone,
  validationTone,
  type SvCamera,
} from "./securevision";

/** Errors reach the UI as the string thrown by lib/api.ts's http(). */
function apiThrow(status: number, code: string, detail = "boom"): Error {
  return new Error(`${status} Error — ${JSON.stringify({ detail: { error: code, detail } })}`);
}

describe("person verdicts", () => {
  it("keeps the three states distinct", () => {
    expect(personVerdictLabel("AUTHORIZED")).toBe("Authorized");
    expect(personVerdictLabel("UNAUTHORIZED")).toBe("Unauthorized");
    expect(personVerdictLabel("UNVERIFIED")).toBe("Unverified");
  });

  it("never colours UNVERIFIED as a critical/unauthorized state", () => {
    expect(personVerdictTone("UNAUTHORIZED")).toBe("critical");
    expect(personVerdictTone("UNVERIFIED")).toBe("neutral");
    expect(personVerdictTone(null)).toBe("neutral");
    expect(personVerdictTone(undefined)).toBe("neutral");
  });

  it("explains that UNVERIFIED means undetermined, not disallowed", () => {
    const hint = personVerdictHint("UNVERIFIED").toLowerCase();
    expect(hint).toContain("not determined");
    expect(hint).not.toContain("unauthorized");
  });
});

describe("camera attribution", () => {
  const unmapped: SvCamera = {
    securevision_code: "CAM-01",
    jnpa_camera_id: null,
    mapped: false,
    map_configured: true,
  };
  const mapped: SvCamera = {
    securevision_code: "CAM-01",
    jnpa_camera_id: "CAM-NSICT-ENT",
    mapped: true,
    map_configured: true,
  };

  it("shows the JNPA camera only when a mapping exists", () => {
    expect(cameraLabel(mapped)).toBe("CAM-NSICT-ENT");
    expect(cameraLabel(unmapped)).toBe("CAM-01");
    expect(cameraHint(mapped)).toBeNull();
  });

  it("says the mapping is unavailable rather than guessing", () => {
    expect(cameraHint(unmapped)).toContain("Camera mapping unavailable");
    expect(cameraHint({ ...unmapped, map_configured: false })).toContain("SECUREVISION_CAMERA_MAP");
  });
});

describe("container cross-check", () => {
  it("labels agreement without hiding either verdict", () => {
    expect(containerAgreementLabel("MATCH")).toBe("Validation: MATCH");
    expect(containerAgreementLabel("REVIEW")).toBe("Validation: REVIEW");
    expect(containerAgreementLabel(null)).toBe("Validation: UNKNOWN");
    expect(containerAgreementTone("MATCH")).toBe("ok");
    expect(containerAgreementTone("REVIEW")).toBe("warn");
    expect(containerAgreementTone("UNKNOWN")).toBe("neutral");
  });
});

describe("formatting", () => {
  it("accepts 0-1 and 0-100 confidences", () => {
    expect(fmtConfidence(0.93)).toBe("93%");
    expect(fmtConfidence(96.5)).toBe("97%");
    expect(fmtConfidence(null)).toBe("—");
  });

  it("labels a clip offset so it cannot read as a wall-clock time", () => {
    expect(fmtClipOffset(4.2)).toBe("+4.2s into clip");
    expect(fmtClipOffset(null)).toBe("—");
  });

  it("formats dwell seconds", () => {
    expect(fmtDwell(8.4)).toBe("8.4 s");
    expect(fmtDwell(undefined)).toBe("—");
  });

  it("tones tamper and validation states", () => {
    expect(tamperTone("BLACK_FRAME")).toBe("critical");
    expect(tamperTone("OK")).toBe("ok");
    expect(tamperTone(null)).toBe("neutral");
    expect(validationTone("PASSED")).toBe("ok");
    expect(validationTone("FAILED")).toBe("critical");
    expect(validationTone(null)).toBe("neutral");
  });

  it("names each incident analyzer", () => {
    expect(incidentTitle("i01")).toContain("Plate");
    expect(incidentTitle("i02")).toContain("Count");
    expect(incidentTitle("i07")).toContain("Restricted");
    expect(incidentTitle("i09")).toContain("ISO 6346");
    expect(incidentTitle("i12")).toContain("Tamper");
    expect(incidentTitle("all")).toContain("Combined");
  });
});

describe("error handling", () => {
  it("detects the 409 that needs a re-run rather than a retry", () => {
    expect(isAnalysisExpired(apiThrow(409, "analysis_expired"))).toBe(true);
    expect(isAnalysisExpired(apiThrow(503, "securevision_unavailable"))).toBe(false);
  });

  it("detects an unconfigured integration", () => {
    expect(isNotConfigured(apiThrow(503, "securevision_not_configured"))).toBe(true);
    expect(isNotConfigured(apiThrow(504, "securevision_timeout"))).toBe(false);
  });

  it("turns each vendor failure into actionable copy, never a stack trace", () => {
    expect(svErrorMessage(apiThrow(409, "analysis_expired"))).toContain("re-run analysis");
    expect(svErrorMessage(apiThrow(409, "person_already_enrolled"))).toContain("already enrolled");
    expect(svErrorMessage(apiThrow(503, "face_model_unavailable"))).toContain("face model");
    expect(svErrorMessage(apiThrow(415, "not_a_video"))).toContain("not a recognised video");
    expect(svErrorMessage(apiThrow(400, "camera_mapping_unavailable"))).toContain("mapped");
    const unconfigured = svErrorMessage(apiThrow(503, "securevision_not_configured"));
    expect(unconfigured).toContain("not configured");
    // Whatever the failure, the vendor's own internals never surface.
    for (const code of [
      "securevision_timeout",
      "securevision_unavailable",
      "securevision_auth_failed",
    ]) {
      const msg = svErrorMessage(apiThrow(502, code));
      expect(msg).not.toContain("httpx");
      expect(msg).not.toContain("Traceback");
    }
  });
});
