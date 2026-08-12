import { describe, expect, it } from "vitest";

import { apiError } from "./api";
import { CUSTOMS_FLAGGED_DETAIL, CUSTOMS_FLAGGED_MESSAGE, customsBlock } from "./customs";

/** Build the ApiErrorInfo the panel sees from a real gateway 400 body. */
function reject(detail: Record<string, unknown>) {
  return apiError(new Error(`400 — ${JSON.stringify({ detail })}`));
}

/** The body POST /api/jobs actually returns for a flagged container. */
function customsReject(over: Record<string, unknown> = {}) {
  return reject({
    error: "customs_flagged",
    detail: CUSTOMS_FLAGGED_MESSAGE,
    reason: CUSTOMS_FLAGGED_MESSAGE,
    customs_status: "HELD",
    customs_note: null,
    container_number: "MRKU5014206",
    message: CUSTOMS_FLAGGED_DETAIL,
    ...over,
  });
}

describe("customsBlock", () => {
  it("stays out of the way when customs is PENDING (no rejection at all)", () => {
    // A PENDING container is assignable, so the panel never sees a customs
    // refusal — the only way a block appears is an explicit customs_flagged.
    expect(customsBlock(apiError(new Error("")))).toBeNull();
  });

  it("ignores refusals that are not about customs", () => {
    const err = reject({
      error: "vehicle_already_assigned",
      detail: "vehicle TRK-000001 already holds open job #11",
      job_id: 11,
    });
    expect(customsBlock(err)).toBeNull();
  });

  it("renders the reason and message the backend sent, not its own wording", () => {
    const block = customsBlock(
      customsReject({
        customs_status: "UNDER_INSPECTION",
        customs_note: "Density anomaly in upper tier",
        detail: `${CUSTOMS_FLAGGED_MESSAGE}: Density anomaly in upper tier`,
      }),
    );
    expect(block).toEqual({
      reason: CUSTOMS_FLAGGED_MESSAGE,
      message: CUSTOMS_FLAGGED_DETAIL,
      note: "Density anomaly in upper tier",
      status: "UNDER_INSPECTION",
      container: "MRKU5014206",
    });
  });

  it("blocks with the generic message and no note when none was recorded", () => {
    const block = customsBlock(customsReject());
    expect(block?.reason).toBe(CUSTOMS_FLAGGED_MESSAGE);
    expect(block?.message).toBe(CUSTOMS_FLAGGED_DETAIL);
    expect(block?.note).toBeNull(); // nothing invented
    expect(block?.status).toBe("HELD");
  });

  it("treats a blank remark as no note", () => {
    expect(customsBlock(customsReject({ customs_note: "   " }))?.note).toBeNull();
  });

  it("falls back to the local wording when the response carries none", () => {
    // A gateway older than the reason/message fields still blocks, and the
    // operator still reads why — the UI never shows an empty banner.
    const block = customsBlock(
      reject({ error: "customs_flagged", detail: CUSTOMS_FLAGGED_MESSAGE, customs_status: "HELD" }),
    );
    expect(block?.reason).toBe(CUSTOMS_FLAGGED_MESSAGE);
    expect(block?.message).toBe(CUSTOMS_FLAGGED_DETAIL);
  });

  it("pins the block to its container so a different one clears it", () => {
    expect(customsBlock(customsReject())?.container).toBe("MRKU5014206");
  });
});
