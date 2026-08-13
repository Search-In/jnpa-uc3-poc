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
  "PoC",
  "docs/ASSUMPTIONS.md",
  "ASSUMPTIONS.md",
  "corpus",
  "G6/G9",
  "REAL measured turnarounds",
  "GROUND-TRUTH",
  "Ground-truth",
  "Method:",
  "Baseline source:",
  "Internal reference baseline",
  "truck_out_ts",
  "truck_in_ts",
  "gate_document",
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

  // Live Operations shows operational figures only. The dual-TAT payload still
  // CARRIES the engineering metadata for other clients (reports, audit,
  // diagnostics) — the card simply must not read any of it.
  const dualTat = stripComments(
    readFileSync(resolve(SRC, "components/panels/DualTatCard.tsx"), "utf8"),
  );

  it.each([
    ["method", "arm.method"],
    ["baseline source", "arm.baseline_source"],
    ["render-rule note", "render_rule.note"],
    ["internal spec id", "render_rule.ref"],
    ["ground-truth markers", "ground_truth_markers"],
    ["ground-truth note", "ground_truth_note"],
    ["REAL provenance badge", "m.provenance"],
    ["source-document id", "source_document"],
    ["marker container id", "m.container_no"],
  ])("the turnaround card does not render the %s", (_label, token) => {
    expect(dualTat).not.toContain(token);
  });

  it("the turnaround card has no ground-truth section left at all", () => {
    expect(dualTat).not.toMatch(/[Gg]round-truth/);
    expect(dualTat).not.toContain("Method:");
    expect(dualTat).not.toContain("Baseline source:");
  });

  it("the turnaround card still renders the operational figures", () => {
    // The point of the removal is presentation, not amputation: name, value,
    // unit, target and baseline must all still be on screen for BOTH arms.
    expect(dualTat).toContain("arm.label");
    expect(dualTat).toContain("arm.definition");
    expect(dualTat).toContain("arm.unit");
    expect(dualTat).toContain("arm.target");
    expect(dualTat).toContain("arm.baseline ??");
    expect(dualTat).toContain("<Arm arm={d.pair.terminal}");
    expect(dualTat).toContain("<Arm arm={d.pair.driver}");
  });

  it("the KPI distribution panel renders no engineering explanation", () => {
    const code = stripComments(
      readFileSync(resolve(SRC, "components/panels/KpiDistributionPanel.tsx"), "utf8"),
    );
    expect(code).not.toContain("skew_warning");
    expect(code).not.toContain("q.data?.note");
    // …while the figures themselves are untouched.
    expect(code).toContain("d.target");
    expect(code).toContain("d.baseline");
    expect(code).toContain("d.p90");
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
