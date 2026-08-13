// Performance & Daily Reports — the Overview's five states.
//
// Regression cover for "Performance & Daily Reports is not loading data": every
// KPI card showed "—" and both charts were blank, with no way to tell which of
// five very different situations was in play. The live cause is provenance:
// the console defaults to the LIVE source, and on the production RDS the
// API-ingested perf rows carry report dates with NULL figures (the JNPA daily
// feed publishes vessels-on-berth and yard inventory only), while the
// manually-imported rows carry the figures.
//
// The fix must never fabricate: no zeros substituted for nulls, and no silent
// switch to the other source.

import { describe, expect, it } from "vitest";

import {
  alternativeHint,
  derivePerfOverviewState,
  hasAnyMetric,
  hasAnySeriesValue,
  type PerfStatsResponse,
} from "./perfOverview";

const COVERAGE = [
  {
    data_origin: "API",
    reports: 24,
    metric_reports: 0,
    date_from: "2026-07-11",
    date_to: "2026-08-07",
  },
  {
    data_origin: "MANUAL",
    reports: 54,
    metric_reports: 54,
    date_from: "2026-02-01",
    date_to: "2026-05-26",
  },
];

/** The live LIVE-mode payload: report dates present, every figure NULL. */
const LIVE_NO_FIGURES: PerfStatsResponse = {
  days: 1,
  latest_kpi: {
    report_date: "2026-08-07",
    metrics: {
      total_teus: null,
      total_tonnes: null,
      vessel_calls: null,
      yard_occupancy_pct: null,
      gate_total_teus: null,
    },
    deltas: {},
  },
  daily: [
    {
      day: "2026-08-07",
      total_teus: null,
      gate_in_teus: null,
      gate_out_teus: null,
      yard_occupancy_pct: null,
    },
  ],
  data_origin: "API",
  coverage: COVERAGE,
};

const POPULATED: PerfStatsResponse = {
  days: 1,
  latest_kpi: {
    report_date: "2026-05-26",
    metrics: { total_teus: 33603, yard_occupancy_pct: 61.06 },
    deltas: { total_teus: 19454 },
  },
  daily: [
    {
      day: "2026-05-26",
      total_teus: 33603,
      gate_in_teus: 9089,
      gate_out_teus: 9881,
      yard_occupancy_pct: 61.06,
    },
  ],
  data_origin: "MANUAL",
  coverage: COVERAGE,
};

describe("derivePerfOverviewState", () => {
  it("renders populated data", () => {
    const s = derivePerfOverviewState({ isLoading: false, isError: false, data: POPULATED });
    expect(s.status).toBe("ok");
    expect(s.hasMetrics).toBe(true);
    expect(s.message).toBeNull();
  });

  it("names the LIVE no-figures case instead of showing bare em-dashes", () => {
    const s = derivePerfOverviewState({ isLoading: false, isError: false, data: LIVE_NO_FIGURES });
    expect(s.status).toBe("no-metrics");
    expect(s.hasMetrics).toBe(false);
    expect(s.message).toContain("No LIVE report figures are available");
    // It points at where the figures ARE — without switching to them.
    expect(s.alternative?.data_origin).toBe("MANUAL");
    expect(alternativeHint(s.alternative)).toContain("54 manually imported reports are available");
  });

  it("does not offer an alternative that has no figures either", () => {
    const s = derivePerfOverviewState({
      isLoading: false,
      isError: false,
      data: {
        ...LIVE_NO_FIGURES,
        data_origin: "MANUAL",
        coverage: [{ ...COVERAGE[0], metric_reports: 0 }],
      },
    });
    expect(s.alternative).toBeNull();
    expect(alternativeHint(s.alternative)).toBeNull();
  });

  it("distinguishes no reports at all from reports without figures", () => {
    const s = derivePerfOverviewState({
      isLoading: false,
      isError: false,
      data: { days: 0, latest_kpi: null, daily: [], data_origin: "API", coverage: COVERAGE },
    });
    expect(s.status).toBe("no-reports");
    expect(s.message).toContain("No LIVE report data is available for this period");
  });

  it("treats a measured ZERO as data, not as missing", () => {
    const zeroed: PerfStatsResponse = {
      days: 1,
      latest_kpi: { report_date: "2026-05-26", metrics: { total_teus: 0 }, deltas: {} },
      daily: [
        {
          day: "2026-05-26",
          total_teus: 0,
          gate_in_teus: 0,
          gate_out_teus: 0,
          yard_occupancy_pct: 0,
        },
      ],
      data_origin: "MANUAL",
      coverage: COVERAGE,
    };
    const s = derivePerfOverviewState({ isLoading: false, isError: false, data: zeroed });
    expect(s.status).toBe("ok");
    expect(s.hasMetrics).toBe(true);
  });

  it("surfaces a request failure as an error, never as an empty period", () => {
    const s = derivePerfOverviewState({ isLoading: false, isError: true, data: undefined });
    expect(s.status).toBe("error");
    expect(s.message).toContain("request failure");
  });

  it("separates loading from every other state", () => {
    const s = derivePerfOverviewState({ isLoading: true, isError: false, data: undefined });
    expect(s.status).toBe("loading");
    expect(s.message).toBeNull();
  });

  it("keeps working against a gateway that sends no coverage block", () => {
    const s = derivePerfOverviewState({
      isLoading: false,
      isError: false,
      data: { days: 1, latest_kpi: POPULATED.latest_kpi, daily: POPULATED.daily },
    });
    expect(s.status).toBe("ok");
    expect(s.alternative).toBeNull();
  });
});

describe("value predicates", () => {
  it("counts zero as a value and null as absent", () => {
    expect(hasAnyMetric({ a: 0 })).toBe(true);
    expect(hasAnyMetric({ a: null, b: null })).toBe(false);
    expect(hasAnyMetric(undefined)).toBe(false);
    expect(
      hasAnySeriesValue([
        {
          day: "d",
          total_teus: 0,
          gate_in_teus: null,
          gate_out_teus: null,
          yard_occupancy_pct: null,
        },
      ]),
    ).toBe(true);
    expect(
      hasAnySeriesValue([
        {
          day: "d",
          total_teus: null,
          gate_in_teus: null,
          gate_out_teus: null,
          yard_occupancy_pct: null,
        },
      ]),
    ).toBe(false);
  });
});
