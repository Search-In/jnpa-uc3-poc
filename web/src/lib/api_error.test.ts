// apiError() unwraps the gateway's `{detail: {error, detail, ...}}` refusal from
// the flattened Error message http() throws, so the Job Assign panel can render
// the precise reason (e.g. "pdp expired") instead of a raw status string.
//
// The fixtures below are VERBATIM responses captured from the running gateway
// against the live schema, so the parser is tested against reality rather than
// against an assumed shape.

import { describe, expect, it } from "vitest";

import { apiError } from "./api";

/** Reproduces exactly what http() throws for a non-2xx response. */
function thrown(status: number, statusText: string, body: unknown): Error {
  return new Error(`${status} ${statusText} — ${JSON.stringify(body)}`);
}

describe("apiError", () => {
  it("extracts the machine-readable code and message from a 400 refusal", () => {
    const err = thrown(400, "Bad Request", {
      detail: {
        error: "pdp_expired",
        detail: "PDP permit PDP2025/280144/3 expired on 2026-06-28",
        pdp_number: "PDP2025/280144/3",
        pdp_validity: "2026-06-28",
      },
    });
    const e = apiError(err);
    expect(e.status).toBe(400);
    expect(e.code).toBe("pdp_expired");
    expect(e.detail).toBe("PDP permit PDP2025/280144/3 expired on 2026-06-28");
    // the extra keys survive for callers that want to show specifics
    expect(e.extra.pdp_number).toBe("PDP2025/280144/3");
  });

  it("recognises a 403 so a permission problem is not shown as an outage", () => {
    const e = apiError(thrown(403, "Forbidden", { detail: "role not permitted" }));
    expect(e.status).toBe(403);
    expect(e.detail).toBe("role not permitted");
  });

  it("reports the other assignment refusals the panel must surface", () => {
    for (const code of ["container_not_found", "no_gate_document", "vehicle_not_active"]) {
      const e = apiError(thrown(400, "Bad Request", { detail: { error: code, detail: "why" } }));
      expect(e.code).toBe(code);
      expect(e.detail).toBe("why");
    }
  });

  it("degrades safely on a non-JSON body, a plain Error and a non-Error", () => {
    expect(apiError(new Error("500 Internal Server Error")).code).toBeNull();
    expect(apiError(new Error("boom")).detail).toBe("boom");
    expect(apiError(new Error("500 Server Error — not json")).status).toBe(500);
    expect(apiError("weird").detail).toBe("weird");
    // null is what TanStack Query holds when a query has NOT failed
    expect(apiError(null).status).toBeNull();
  });
});
