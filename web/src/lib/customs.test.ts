import { describe, expect, it } from "vitest";

import { apiError } from "./api";
import { CUSTOMS_FLAGGED_MESSAGE, customsBlock } from "./customs";

/** Build the ApiErrorInfo the panel sees from a real gateway 400 body. */
function reject(detail: Record<string, unknown>) {
  return apiError(new Error(`400 — ${JSON.stringify({ detail })}`));
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

  it("blocks with the recorded note when customs left one", () => {
    const block = customsBlock(
      reject({
        error: "customs_flagged",
        detail: `${CUSTOMS_FLAGGED_MESSAGE}: Density anomaly in upper tier`,
        customs_status: "UNDER_INSPECTION",
        customs_note: "Density anomaly in upper tier",
        container_number: "MRKU5014206",
      }),
    );
    expect(block).toEqual({
      message: `Vehicle assignment blocked — ${CUSTOMS_FLAGGED_MESSAGE}`,
      note: "Density anomaly in upper tier",
      status: "UNDER_INSPECTION",
      container: "MRKU5014206",
    });
  });

  it("blocks with the generic message and no note when none was recorded", () => {
    const block = customsBlock(
      reject({
        error: "customs_flagged",
        detail: CUSTOMS_FLAGGED_MESSAGE,
        customs_status: "HELD",
        customs_note: null,
        container_number: "MRKU5014206",
      }),
    );
    expect(block?.message).toBe(`Vehicle assignment blocked — ${CUSTOMS_FLAGGED_MESSAGE}`);
    expect(block?.note).toBeNull(); // nothing invented
    expect(block?.status).toBe("HELD");
  });

  it("treats a blank remark as no note", () => {
    const block = customsBlock(
      reject({ error: "customs_flagged", detail: CUSTOMS_FLAGGED_MESSAGE, customs_note: "   " }),
    );
    expect(block?.note).toBeNull();
  });

  it("pins the block to its container so a different one clears it", () => {
    const block = customsBlock(
      reject({
        error: "customs_flagged",
        detail: CUSTOMS_FLAGGED_MESSAGE,
        container_number: "MRKU5014206",
      }),
    );
    expect(block?.container).toBe("MRKU5014206");
  });
});
