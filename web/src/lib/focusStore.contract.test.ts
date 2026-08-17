// The focus store is duplicated into four separately-deployed surfaces because
// they are four independent repos with no shared package. The URL grammar it
// encodes is a contract BETWEEN those origins, so a drift in one copy does not
// fail loudly — it silently breaks the cross-app hand-off, which is exactly the
// failure this whole feature exists to remove.
//
// This test is the guard: the copies must be byte-identical apart from the
// default `origin` each one stamps on a locally-raised focus.
//
// If a sibling repo is not checked out beside this one the comparison is
// skipped rather than failed — CI for this repo alone must not depend on the
// others being present.

import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const CANONICAL = resolve(__dirname, "focusStore.ts");
// jnpa-uc3-poc/web/src/lib -> up 4 = the PoC root holding all four repos.
const ROOT = resolve(__dirname, "..", "..", "..", "..");

const COPIES: { label: string; path: string; origin: string }[] = [
  { label: "UC-1 poc_1", path: resolve(ROOT, "poc_1/src/lib/focusStore.ts"), origin: "UC-1" },
  { label: "UC-2 PoC_2", path: resolve(ROOT, "PoC_2/apps/web/src/lib/focusStore.ts"), origin: "UC-2" },
  { label: "suite dtccc", path: resolve(ROOT, "suite/dtccc/src/focusStore.ts"), origin: "SUITE" },
];

/** Blank out every origin literal so only real logic differences survive. */
const neutralise = (s: string): string => s.replace(/"(UC-1|UC-2|UC-3|SUITE)"/g, '"@"');

describe("focusStore copies stay in step", () => {
  const canonical = readFileSync(CANONICAL, "utf8");

  for (const c of COPIES) {
    const present = existsSync(c.path);
    it.skipIf(!present)(`${c.label} matches the canonical copy modulo its origin`, () => {
      const copy = readFileSync(c.path, "utf8");
      expect(neutralise(copy)).toBe(neutralise(canonical));
    });

    it.skipIf(!present)(`${c.label} stamps origin ${c.origin} by default`, () => {
      const copy = readFileSync(c.path, "utf8");
      expect(copy).toContain(`EMPTY_FOCUS: PortFocus = { origin: "${c.origin}"`);
      expect(copy).toContain(`origin: FocusOrigin = "${c.origin}"`);
    });
  }

  it("the canonical copy stamps UC-3", () => {
    expect(canonical).toContain('EMPTY_FOCUS: PortFocus = { origin: "UC-3"');
  });

  it("the URL parameter names are the ones the other repos deep-link with", () => {
    // Changing any of these silently breaks every shared link and demo script.
    for (const name of ["vcn", "via", "imo", "vessel", "container", "vehicle", "igm", "asOf"]) {
      expect(canonical).toContain(`"${name}"`);
    }
  });
});
