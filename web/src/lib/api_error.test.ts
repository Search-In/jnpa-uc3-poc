// apiError() unwraps the gateway's `{detail: {error, detail, ...}}` refusal from
// the flattened Error message http() throws, so the Job Assign panel can render
// the precise reason (e.g. "pdp expired") instead of a raw status string.
//
// The fixtures below are VERBATIM responses captured from the running gateway
// against the live schema, so the parser is tested against reality rather than
// against an assumed shape.

import { describe, expect, it } from "vitest";

import { DEFAULT_TIMEOUT_MS, UPLOAD_TIMEOUT_MS, apiError, isTimeoutError } from "./api";

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

// The audit measured /api/kpi at 81s against RDS. `fetch` has no default
// timeout, so the panel hung forever with no error state. http() now aborts at
// DEFAULT_TIMEOUT_MS and shapes the failure like any other API error.
describe("request timeout", () => {
  /** Exactly what http() throws when AbortSignal.timeout fires. */
  function timedOut(path = "/api/kpi", ms = DEFAULT_TIMEOUT_MS): Error {
    return new Error(
      `408 Request Timeout — ${JSON.stringify({
        detail: {
          error: "ETIMEDOUT",
          detail: `The server did not respond within ${Math.round(ms / 1000)}s.`,
          path,
        },
      })}`,
    );
  }

  it("has a bounded default budget, and a longer one for transfers", () => {
    expect(DEFAULT_TIMEOUT_MS).toBeGreaterThan(0);
    expect(DEFAULT_TIMEOUT_MS).toBeLessThanOrEqual(30_000);
    expect(UPLOAD_TIMEOUT_MS).toBeGreaterThan(DEFAULT_TIMEOUT_MS);
  });

  it("parses a timeout into the same shape as any other API error", () => {
    const e = apiError(timedOut());
    expect(e.status).toBe(408);
    expect(e.code).toBe("ETIMEDOUT");
    expect(e.timedOut).toBe(true);
    expect(e.detail).toContain("did not respond");
    expect(e.extra.path).toBe("/api/kpi");
  });

  it("isTimeoutError distinguishes a slow server from a refusal", () => {
    expect(isTimeoutError(timedOut())).toBe(true);
    expect(isTimeoutError(thrown(403, "Forbidden", { detail: "role not permitted" }))).toBe(false);
    expect(isTimeoutError(thrown(400, "Bad Request", { detail: { error: "pdp_expired" } }))).toBe(
      false,
    );
    expect(isTimeoutError(null)).toBe(false);
  });

  it("does not mark ordinary errors as timeouts", () => {
    expect(apiError(thrown(500, "Server Error", { detail: "boom" })).timedOut).toBe(false);
    expect(apiError(new Error("boom")).timedOut).toBe(false);
  });
});
