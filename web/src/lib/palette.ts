// Colour-blind-safe severity + flow palette (Okabe–Ito). Single source of truth
// shared by the map layers, chips, and charts so colours never drift between
// the legend and the data. All pairs meet WCAG AA against the dark canvas.

export const SEVERITY_COLOUR: Record<string, string> = {
  info: "#56B4E9",
  warning: "#E69F00",
  critical: "#D55E00",
  REPORT_TO_POLICE: "#D55E00",
  ok: "#009E73",
};

export function severityColour(sev?: string | null): string {
  if (!sev) return "#999999";
  return SEVERITY_COLOUR[sev] ?? "#999999";
}

// Severity rank for sorting "most severe first".
export function severityRank(sev?: string | null): number {
  switch (sev) {
    case "REPORT_TO_POLICE":
    case "critical":
      return 3;
    case "warning":
      return 2;
    case "info":
      return 1;
    default:
      return 0;
  }
}

// jam_factor is 0..10 (TrafficSnapshot); the spec colours the corridor by the
// 0..1 normalised band. We accept either and normalise.
export function jamColour(jamFactor: number): string {
  const j = jamFactor > 1 ? jamFactor / 10 : jamFactor;
  if (j >= 0.6) return "#D55E00"; // red
  if (j >= 0.3) return "#E69F00"; // amber
  return "#009E73"; // green
}

// Gate marker colour by throughput utilisation vs. target. Under-served (low
// utilisation) and over-saturated (>= 1.0, congestion) both read as problems.
export function gateColour(utilisation: number | null): string {
  if (utilisation == null) return "#999999";
  if (utilisation >= 1.0 || utilisation < 0.35) return "#D55E00";
  if (utilisation < 0.6) return "#E69F00";
  return "#009E73";
}

export function sourceStateColour(state?: string | null): string {
  switch (state) {
    case "LIVE":
      return "#009E73";
    case "DEGRADED":
      return "#E69F00";
    case "DOWN":
      return "#D55E00";
    default:
      return "#999999";
  }
}
