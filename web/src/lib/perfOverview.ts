// Performance & Daily Reports — Overview state derivation.
//
// The Overview board renders seven KPI cards and two charts from a single
// GET /api/performance/stats. That call is narrowed by the header data-source
// mode (LIVE ⇒ data_origin='API', DEMO ⇒ 'MANUAL'), and the tab used to render
// `num(metric)` straight into each card, where `num(null)` is "—".
//
// Five very different situations therefore printed the SAME em-dash:
//
//   1. the request is still in flight,
//   2. the request FAILED (the tab never read `isError` at all),
//   3. the selected provenance has report rows but no KPI VALUES — the live
//      situation for LIVE/API: the JNPA daily-report feed publishes
//      vessels-on-berth and yard inventory, so throughput, gate movements,
//      occupancy and pendency all arrive NULL,
//   4. there are no reports for the period at all,
//   5. a genuine, measured ZERO.
//
// An operator cannot act on a board that shows the same glyph for "loading",
// "broken" and "measured zero". This module names the five, from the response
// the API already returns plus the coverage block it now carries. It fabricates
// nothing: a missing figure stays missing, a zero stays zero.

/** Per-provenance report coverage, as reported by /api/performance/stats. */
export interface PerfCoverage {
  data_origin: string;
  /** Report dates held for this provenance. */
  reports: number;
  /** Of those, how many carry at least one headline KPI value. */
  metric_reports: number;
  date_from: string | null;
  date_to: string | null;
}

export interface PerfDailyPoint {
  day: string;
  total_teus: number | null;
  gate_in_teus: number | null;
  gate_out_teus: number | null;
  yard_occupancy_pct: number | null;
}

export interface PerfStatsResponse {
  days: number;
  latest_kpi: {
    report_date?: string;
    metrics?: Record<string, number | null>;
    deltas?: Record<string, number>;
  } | null;
  daily: PerfDailyPoint[];
  /** Applied provenance filter: "API" | "MANUAL" | null (unfiltered). */
  data_origin?: string | null;
  coverage?: PerfCoverage[];
}

export type PerfOverviewStatus =
  | "loading"
  /** The request failed — nothing below is a measurement. */
  | "error"
  /** No report at all for the selected source/period. */
  | "no-reports"
  /** Reports exist for this source but carry no headline figures. */
  | "no-metrics"
  /** Real figures (possibly zero) to render. */
  | "ok";

/** Header data-source mode ⇒ the data_origin the gateway filters on. */
export const ORIGIN_LABEL: Record<string, string> = {
  API: "LIVE",
  MANUAL: "DEMO",
};

export interface PerfOverviewState {
  status: PerfOverviewStatus;
  /** True only when KPI cards/charts should render values. */
  hasMetrics: boolean;
  /** Applied provenance ("API"/"MANUAL"), or null when unfiltered. */
  origin: string | null;
  /** Operator-facing headline for a non-ok state. */
  message: string | null;
  /**
   * Where figures DO exist, when the selected source has none — so the console
   * can point at the source switch instead of silently swapping the source
   * itself (which would misrepresent LIVE data as available).
   */
  alternative: PerfCoverage | null;
}

function originName(origin: string | null | undefined): string {
  if (!origin) return "the selected source";
  return ORIGIN_LABEL[origin] ?? origin;
}

/** True when a metrics map holds at least one real number (0 counts). */
export function hasAnyMetric(metrics: Record<string, number | null> | undefined): boolean {
  if (!metrics) return false;
  return Object.values(metrics).some((v) => typeof v === "number" && Number.isFinite(v));
}

/** True when the daily series holds at least one plottable point (0 counts). */
export function hasAnySeriesValue(daily: PerfDailyPoint[] | undefined): boolean {
  if (!daily?.length) return false;
  return daily.some(
    (d) =>
      typeof d.total_teus === "number" ||
      typeof d.gate_in_teus === "number" ||
      typeof d.gate_out_teus === "number" ||
      typeof d.yard_occupancy_pct === "number",
  );
}

export function derivePerfOverviewState(input: {
  isLoading: boolean;
  isError: boolean;
  data?: PerfStatsResponse | null;
}): PerfOverviewState {
  const { isLoading, isError, data } = input;
  if (isError) {
    return {
      status: "error",
      hasMetrics: false,
      origin: null,
      message:
        "Report data could not be loaded. This is a request failure, not an empty report period.",
      alternative: null,
    };
  }
  if (isLoading || !data) {
    return { status: "loading", hasMetrics: false, origin: null, message: null, alternative: null };
  }

  const origin = data.data_origin ?? null;
  const coverage = data.coverage ?? [];
  const mine = coverage.find((c) => c.data_origin === origin) ?? null;
  // The best other provenance to point at: the one that actually has figures.
  const alternative =
    coverage
      .filter((c) => c.data_origin !== origin && c.metric_reports > 0)
      .sort((a, b) => b.metric_reports - a.metric_reports)[0] ?? null;

  const metrics = data.latest_kpi?.metrics;
  const withFigures = hasAnyMetric(metrics) || hasAnySeriesValue(data.daily);
  if (withFigures) {
    return { status: "ok", hasMetrics: true, origin, message: null, alternative };
  }

  const noReports = (data.daily?.length ?? 0) === 0 && !data.latest_kpi;
  if (noReports) {
    return {
      status: "no-reports",
      hasMetrics: false,
      origin,
      message: `No ${originName(origin)} report data is available for this period.`,
      alternative,
    };
  }

  const held = mine
    ? ` ${mine.reports} ${originName(origin)} report${mine.reports === 1 ? "" : "s"} carry no headline figures.`
    : "";
  return {
    status: "no-metrics",
    hasMetrics: false,
    origin,
    message: `No ${originName(origin)} report figures are available for this period.${held}`,
    alternative,
  };
}

/** "54 manually imported reports are available." — pointer, never an auto-switch. */
export function alternativeHint(alt: PerfCoverage | null): string | null {
  if (!alt) return null;
  const label = alt.data_origin === "MANUAL" ? "manually imported" : "API-sourced";
  const plural = alt.metric_reports === 1 ? "report is" : "reports are";
  const range = alt.date_from && alt.date_to ? ` (${alt.date_from} → ${alt.date_to})` : "";
  return `${alt.metric_reports} ${label} ${plural} available${range}. Switch the data source in the header to view them.`;
}
