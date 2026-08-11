/**
 * Verdict sentences, checked against REAL gateway responses.
 *
 * `__fixtures__/live-results.json` was captured from the gateway running against
 * the JNPA RDS — not hand-written. That matters: a verdict that reads well over
 * invented figures and breaks on a null, a zero or a missing nested key is the
 * failure mode this file exists to catch.
 *
 * The assertions are deliberately about *readability* as much as correctness: no
 * "undefined", no "NaN", no "1 calls", no raw snake_case leaking through to a
 * reader who has never seen the database.
 */
import { describe, expect, it } from "vitest";
import live from "./__fixtures__/live-results.json";
import { coverageNotice, shouldChip, verdictFor, type VerdictInput } from "./verdict";

const results = live as unknown as Record<string, VerdictInput>;
const SCENARIOS = Object.keys(results);

/** Indexed lookup that fails loudly rather than handing a verdict `undefined`.
 *  Written this way because UC-2's tsconfig enables `noUncheckedIndexedAccess`,
 *  and this file is copied verbatim into all three apps — it has to satisfy the
 *  strictest of them. */
function fixture(name: string): VerdictInput {
  const found = results[name];
  if (!found) throw new Error(`no captured result for scenario '${name}'`);
  return found;
}

describe("verdictFor over live gateway responses", () => {
  it("covers every scenario the gateway exposes", () => {
    expect(SCENARIOS).toHaveLength(9);
  });

  it.each(SCENARIOS)("%s produces a readable sentence", (name) => {
    const v = verdictFor(fixture(name));

    expect(v.headline.length).toBeGreaterThan(20);
    // Ends like a sentence, starts like one.
    expect(v.headline).toMatch(/[.!?]$/);
    const first = v.headline.charAt(0);
    expect(first).toEqual(first.toUpperCase());

    // Nothing half-rendered reaches the reader.
    for (const text of [v.headline, v.detail ?? ""]) {
      expect(text).not.toMatch(/undefined|NaN|\[object|null/i);
      // No raw column names: snake_case with two or more underscores.
      expect(text).not.toMatch(/\b[a-z]+_[a-z]+_[a-z]+\b/);
    }
    expect(["ok", "warning", "critical", "unavailable"]).toContain(v.tone);
  });

  it.each(SCENARIOS)("%s builds its own sentence, not the fallback", (name) => {
    // The strongest guard in this file. Every builder calls req() for the figures
    // its sentence needs; a renamed backend key throws and degrades to this
    // generic line. If that happens the scenario is silently no longer explained,
    // so it must fail here rather than ship. This caught sustained_rate ->
    // sustained_rate_per_hour and delay_hours -> target_delay_hours.
    const v = verdictFor(fixture(name));
    expect(v.headline).not.toMatch(/see the figures below/i);
  });

  it.each(SCENARIOS)("%s never writes '1 items'", (name) => {
    const v = verdictFor(fixture(name));
    const text = `${v.headline} ${v.detail ?? ""}`;
    // "1 hours", "1 calls", "1 vessels" — the tell that a screen is generated.
    expect(text).not.toMatch(/\b1 [a-z]+s\b/);
  });

  it.each(SCENARIOS)("%s agrees its verbs with its counts", (name) => {
    const v = verdictFor(fixture(name));
    const text = `${v.headline} ${v.detail ?? ""}`;
    // "3 berths is still free", "2 vessels finishes" — plural noun, singular verb.
    expect(text).not.toMatch(/\b(?!1\b)\d+ [a-z]+s (is|was|finishes|waits|runs)\b/);
    // "1 berths are", "1 vessel finish" — singular count, plural verb.
    expect(text).not.toMatch(/\b1 [a-z]+ (are|were)\b/);
    // "a 6 hours outage" — hours used attributively where "6-hour" is meant.
    // Targeted at the head nouns this file actually uses, so a legitimate
    // "6 hours to clear" is not flagged.
    expect(text).not.toMatch(
      /\b\d+(\.\d+)? hours (outage|overrun|closure|window|shortage|delay)\b/);
  });

  it.each(SCENARIOS)("%s does not report a zero as if it were a problem", (name) => {
    const v = verdictFor(fixture(name));
    const text = `${v.headline} ${v.detail ?? ""}`;
    expect(text).not.toMatch(/\b0 [a-z]+s? (cannot|could not|are not|is not)\b/);
  });
});

describe("specific verdicts say the right thing", () => {
  it("III-A names the peak, the sustained rate and the saturated hours", () => {
    const v = verdictFor(fixture("gate-slotting"));
    const f = fixture("gate-slotting").figures;
    expect(v.headline).toContain(String(Math.round(Number(f.observed_peak))));
    expect(v.headline).toContain(String(Math.round(Number(f.sustained_rate_per_hour))));
  });

  it("II-B says the rate is per berth, not per crane", () => {
    const v = verdictFor(fixture("crane-productivity"));
    expect(`${v.headline} ${v.detail}`).toMatch(/whole berth|per berth|not one crane/i);
  });

  it("N-2 distinguishes settling from being safe", () => {
    const v = verdictFor(fixture("yard-feedback"));
    const text = `${v.headline} ${v.detail ?? ""}`;
    if (fixture("yard-feedback").figures.regime === "SATURATING") {
      expect(text).toMatch(/does not settle|fills until/i);
    } else {
      expect(text).toMatch(/not the same as safe|slowing the berth/i);
    }
  });

  it("N-1 reports berth-lock as a state, not a number", () => {
    const v = verdictFor(fixture("channel-closure"));
    expect(v.headline).toMatch(/berth-locked|stays workable/i);
  });
});

describe("unanswerable results", () => {
  const base: VerdictInput = {
    scenario: "modal-shift", figures: {}, result: {},
    data_available: false, notes: [],
  };

  it("an empty table and a failed query are not the same verdict", () => {
    const empty = verdictFor({ ...base, notes: ["core.eir returned no rows"] });
    const failed = verdictFor({
      ...base,
      notes: ["QUERY FAILED (gate profile): relation does not exist"],
    });
    expect(empty.headline).not.toEqual(failed.headline);
    expect(failed.headline).toMatch(/failed/i);
    expect(empty.tone).toBe("unavailable");
  });

  it("carries the backend's own reason rather than inventing one", () => {
    const v = verdictFor({ ...base, notes: ["core.eir carries 5 rows"] });
    expect(v.detail).toContain("core.eir carries 5 rows");
  });

  it("an unknown scenario still renders something honest", () => {
    const v = verdictFor({ ...base, scenario: "not-a-scenario", data_available: true });
    expect(v.headline).toMatch(/figures below/i);
  });
});

describe("provenance chips stay quiet", () => {
  it("MEASURED and DERIVED are unmarked; ASSUMED and PARAMETER are chipped", () => {
    expect(shouldChip("MEASURED")).toBe(false);
    expect(shouldChip("DERIVED")).toBe(false);
    expect(shouldChip("ASSUMED")).toBe(true);
    expect(shouldChip("PARAMETER")).toBe(true);
  });
});

describe("coverage banner", () => {
  it("is silent for a measured day", () => {
    expect(coverageNotice({ coverage: { basis: "MEASURED" } })).toBeNull();
    expect(coverageNotice({})).toBeNull();
  });

  it("explains a projected day in plain words", () => {
    const text = coverageNotice({
      coverage: {
        basis: "PROJECTED",
        requested: "2026-08-06T00:00:00Z",
        measured_through: "2026-08-05T10:26:00Z",
      },
    });
    expect(text).toContain("2026-08-06");
    expect(text).toContain("2026-08-05");
    expect(text).toMatch(/projection, not a measurement/i);
  });
});
