// Operator screens must not render internal/engineering wording.
//
// Regression cover for the Live Operations copy defect: the turnaround card
// printed the gateway's engineering notes verbatim ("PoC demonstration
// baseline … see docs/ASSUMPTIONS.md", "the plaza legs have no corpus events
// (gaps G6/G9) and are simulated", "The only REAL measured turnarounds in the
// corpus", "mean(truck_out_ts - truck_in_ts) over gate documents"), and the
// Vehicle Registry printed "Full entry: docs/ASSUMPTIONS.md".
//
// The wording is sanitised at the API boundary — asserted server-side in
// tests/test_operator_copy_boundary.py, which is the guard that matters for any
// client. This is the CLIENT half: no production screen may reintroduce the
// wording as a literal of its own.
//
// Code COMMENTS are exempt and are stripped before scanning: the engineering
// rationale belongs in the source, it just may not reach a screen. (This repo's
// vitest runs in a node environment with no DOM testing library, so the check is
// made against the rendered source rather than a mounted tree.)

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), "..");

/** Screens an operator uses on shift, where this wording must never appear. */
const OPERATOR_SCREENS = [
  "screens/LiveOperations.tsx",
  "screens/PerformanceReports.tsx",
  "screens/DriverAdvisory.tsx",
  "screens/VideoAnalytics.tsx",
  "screens/VehicleRegistry.tsx",
  "components/panels/DualTatCard.tsx",
  "components/panels/KpiStrip.tsx",
];

const BANNED = [
  "PoC demonstration baseline",
  "docs/ASSUMPTIONS.md",
  "ASSUMPTIONS.md",
  "corpus events",
  "G6/G9",
  "REAL measured turnarounds",
  "truck_out_ts",
  "truck_in_ts",
];

/** Drop // line comments and block comments — rationale is allowed in source. */
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

describe("operator screens carry no internal wording", () => {
  it.each(OPERATOR_SCREENS)("%s", (file) => {
    const code = stripComments(readFileSync(resolve(SRC, file), "utf8"));
    const found = BANNED.filter((phrase) => code.includes(phrase));
    expect(found, `${file} renders internal wording: ${found.join(", ")}`).toEqual([]);
  });

  it("the turnaround card no longer prints the internal spec id as a chip", () => {
    const code = stripComments(
      readFileSync(resolve(SRC, "components/panels/DualTatCard.tsx"), "utf8"),
    );
    // render_rule.ref stays in the payload as machine metadata; the card must
    // not print it.
    expect(code).not.toContain("render_rule.ref");
    // …but the card still renders the operator-facing method/baseline text the
    // API supplies: sanitised, not stripped.
    expect(code).toContain("arm.method");
    expect(code).toContain("arm.baseline_source");
  });
});

describe("the alerts drawer is user-owned state", () => {
  // Bug 5, half 1: the guided tour force-opened the Active Alerts sheet and
  // disabled its dismissal. TFC-2 ends on an alert step, so the sheet stayed
  // open over the whole console and swallowed the second run's clicks.
  const code = stripComments(
    readFileSync(resolve(SRC, "components/layout/HeaderActions.tsx"), "utf8"),
  );

  it("does not derive the drawer's open state from the tour", () => {
    expect(code).not.toContain("alertStepActive");
    expect(code).not.toContain("useTourStore");
    expect(code).not.toContain("getScript");
  });

  it("does not disable outside-click or Escape dismissal", () => {
    expect(code).not.toContain("outsideCloseDisabled");
    expect(code).not.toContain("escapeDisabled");
  });

  it("opens only from the user's own action", () => {
    expect(code).toContain("const expanded = open;");
  });
});
