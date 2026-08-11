// T-01 corridor congestion heatmap (UC3-020), on the existing GeoAnalytics map.
//
// A tab on the screen that already owns the map, its layers and its legend — not
// a second map application. It reuses ArcgisMap and feeds the same Esri heat
// layer the violation heatmap uses, so there is one map implementation with two
// things to render on it.
//
// The rule this panel enforces on screen is the DATA_MODE flip. The slider runs
// -6 h … now … +2 h, and the banner changes at exactly the moment the slider
// crosses now: at or before it the segments are OBSERVED counts, after it they
// are a DERIVED extrapolation carrying a confidence band that widens with the
// horizon. Presenting a forecast as an observation is the failure the banner
// exists to prevent, so it is large, coloured, and always visible.
//
// Segment geometry is OSM-traced under assumption A4, not survey-grade, and the
// resolution disclaimer says so permanently rather than in a tooltip.
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Flame, Route } from "lucide-react";

import { api } from "@/lib/api";
import { cn, fmtDateTimeIST } from "@/lib/utils";
import { Card } from "@/components/ui/card";
import { StatusChip, type Tone } from "@/components/ui/dtccc";
import { ArcgisMap } from "@/components/map/ArcgisMap";
import type { IncidentPoint } from "@/lib/incidents";
import type { HeatmapSegment } from "@/lib/types";

const BAND_TONE: Record<string, Tone> = {
  FREE: "ok",
  BUSY: "info",
  HEAVY: "warn",
  SEVERE: "critical",
};

/** Legend, in the order an operator reads severity. */
const LEGEND: Array<{ band: string; label: string; from: string }> = [
  { band: "FREE", label: "Free flowing", from: "index < 0.50" },
  { band: "BUSY", label: "Busy", from: "0.50 – 0.75" },
  { band: "HEAVY", label: "Heavy", from: "0.75 – 0.90" },
  { band: "SEVERE", label: "Severe", from: "≥ 0.90" },
];

/**
 * Segments as heat points for the existing Esri heat layer.
 *
 * Weight is the jam probability, so the heat follows the thing the reroute
 * decision is actually made on rather than a separate visual scale that could
 * disagree with it.
 */
function toIncidents(segments: HeatmapSegment[]): IncidentPoint[] {
  return segments
    .filter((s) => s.jam_probability !== null)
    .map((s) => ({
      id: s.segment_code,
      kind: "ai" as const,
      lat: s.lat,
      lon: s.lon,
      weight: Math.max(0.05, s.jam_probability ?? 0),
      event_type: `congestion:${s.band ?? "UNKNOWN"}`,
      vehicle_id: null,
      zone_id: s.segment_code,
      severity:
        s.band === "SEVERE" || s.band === "HEAVY"
          ? ("HIGH" as const)
          : s.band === "BUSY"
            ? ("MEDIUM" as const)
            : ("LOW" as const),
      status: s.data_mode,
      created_at: new Date().toISOString(),
      located_by: "coords" as const,
    }));
}

export default function CorridorHeatmapPanel() {
  const [offset, setOffset] = useState(0);

  const q = useQuery({
    queryKey: ["corridor-heatmap", offset],
    queryFn: () => api.corridorHeatmap(offset),
    refetchInterval: 15 * 60 * 1000, // the 15-minute forecast cycle
  });

  const corridorQ = useQuery({
    queryKey: ["corridor-geometry"],
    queryFn: () => api.corridor(),
    staleTime: Infinity, // static geometry
  });

  const d = q.data;
  const derived = d?.data_mode === "DERIVED";
  const segments = d?.segments ?? [];
  const measured = segments.filter((s) => s.congestion_index !== null);

  return (
    <div className="flex min-w-0 flex-col gap-3">
      {/* ---- DATA_MODE banner: the flip is the point, so it is unmissable ---- */}
      <div
        className={cn(
          "flex min-w-0 flex-wrap items-center gap-2 rounded-lg border-2 p-2.5",
          derived
            ? "border-amber-500/60 bg-amber-500/10"
            : "border-emerald-500/50 bg-emerald-500/10",
        )}
        role="status"
        aria-live="polite"
      >
        <StatusChip label={d?.data_mode ?? "…"} tone={derived ? "warn" : "ok"} />
        <span className="text-[12px] font-medium">
          {derived
            ? `Forecast — confidence ${((d?.confidence ?? 0) * 100).toFixed(0)}%`
            : "Observed counts"}
        </span>
        {d && (
          <span className="text-[11px] text-muted-foreground">
            {fmtDateTimeIST(d.at)} · {d.offset_minutes >= 0 ? "+" : ""}
            {d.offset_minutes} min from now
          </span>
        )}
        {d?.clamped && <StatusChip label="CLAMPED TO WINDOW" tone="info" />}
      </div>

      {/* ---- time slider: -6 h … now … +2 h ---- */}
      <Card className="min-w-0 p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="flex items-center gap-1.5 text-sm font-semibold">
            <Flame className="h-4 w-4 shrink-0" aria-hidden />
            Corridor congestion — NH-348
          </h3>
          <div className="flex shrink-0 items-center gap-1.5">
            <button
              type="button"
              onClick={() => setOffset(0)}
              className="rounded-md border border-border bg-background px-2 py-1 text-[11px] font-medium hover:bg-muted"
            >
              Now
            </button>
          </div>
        </div>

        <label className="mt-2 block">
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
            Time slider ({d?.window.past_hours ?? 6}h past → +{d?.window.forecast_hours ?? 2}h
            forecast)
          </span>
          <input
            type="range"
            aria-label="Corridor heatmap time slider"
            min={d?.window.min_offset_minutes ?? -360}
            max={d?.window.max_offset_minutes ?? 120}
            step={15}
            value={offset}
            onChange={(e) => setOffset(Number(e.target.value))}
            className="mt-1 w-full accent-primary"
          />
          <span className="flex justify-between text-[10px] text-muted-foreground">
            <span>−6h</span>
            <span className="font-semibold text-foreground">now</span>
            <span>+2h</span>
          </span>
        </label>
      </Card>

      {/* ---- reroute recommendation ---- */}
      {d?.reroute.triggered && (
        <p className="flex min-w-0 items-start gap-1.5 rounded-lg border border-severity-critical/40 bg-severity-critical/10 p-2.5 text-[11px] leading-snug text-severity-critical">
          <Route className="mt-px h-4 w-4 shrink-0" aria-hidden />
          <span className="min-w-0">
            <span className="font-semibold">
              Pre-emptive reroute recommended ({d.reroute.action}).
            </span>{" "}
            Jam probability reached the {d.reroute.threshold} threshold on{" "}
            {d.reroute.segments.length} segment
            {d.reroute.segments.length === 1 ? "" : "s"}:{" "}
            <span className="font-mono">{d.reroute.segments.join(", ")}</span>.
          </span>
        </p>
      )}

      {/* ---- the map, reusing the existing ArcGIS surface + heat layer ---- */}
      <div className="min-w-0 overflow-hidden rounded-lg border border-border">
        <div className="h-[320px] w-full sm:h-[420px]">
          <ArcgisMap corridor={corridorQ.data} incidents={toIncidents(segments)} />
        </div>
      </div>

      {/* ---- legend ---- */}
      <Card className="min-w-0 p-3">
        <h4 className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          Legend — congestion index
        </h4>
        <ul className="mt-1.5 flex flex-wrap gap-2">
          {LEGEND.map((l) => (
            <li key={l.band} className="flex shrink-0 items-center gap-1">
              <StatusChip label={l.band} tone={BAND_TONE[l.band]} />
              <span className="text-[10px] text-muted-foreground">
                {l.label} · {l.from}
              </span>
            </li>
          ))}
        </ul>
        <p className="mt-1.5 text-[10px] leading-snug text-muted-foreground">
          {String(d?.method.congestion_index ?? "")} Capacity{" "}
          {String(d?.method.capacity_vph ?? "—")} veh/h ({String(d?.method.capacity_source ?? "")}).
        </p>
      </Card>

      {/* ---- per-segment readout ---- */}
      {q.isLoading && (
        <p className="text-[12px] text-muted-foreground" role="status">
          Loading corridor congestion…
        </p>
      )}
      {q.isError && (
        <p className="text-[12px] text-severity-critical" role="alert">
          Corridor heatmap unavailable: {(q.error as Error).message}
        </p>
      )}
      {!q.isLoading && !q.isError && segments.length === 0 && (
        <p className="text-[12px] text-muted-foreground">No corridor segments configured.</p>
      )}

      {segments.length > 0 && (
        <Card className="min-w-0 p-3">
          <h4 className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            Segments ({measured.length} of {segments.length} measured)
          </h4>
          <ul className="mt-1.5 flex flex-col gap-1">
            {segments.map((s) => (
              <li
                key={s.segment_code}
                className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 border-b border-border/50 py-1 text-[11px] last:border-0"
              >
                <span className="w-16 shrink-0 font-mono">{s.segment_code}</span>
                {s.congestion_index === null ? (
                  <span className="text-muted-foreground">not measured in this bucket</span>
                ) : (
                  <>
                    <StatusChip label={s.band ?? "—"} tone={BAND_TONE[s.band ?? "FREE"]} />
                    <span className="tabular-nums">idx {s.congestion_index}</span>
                    <span className="tabular-nums text-muted-foreground">
                      {s.flow_vph} veh/h · {s.speed_kph} km/h
                    </span>
                    <span className="tabular-nums">P {s.jam_probability}</span>
                    <StatusChip
                      label={s.observation}
                      tone={s.observation === "COUNTED" ? "ok" : "warn"}
                    />
                    {s.reroute_recommended && <StatusChip label="REROUTE" tone="critical" />}
                  </>
                )}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {/* ---- provenance + the permanent resolution disclaimer ---- */}
      {d && (
        <p className="flex min-w-0 items-start gap-1.5 text-[10px] leading-snug text-muted-foreground">
          <AlertTriangle className="mt-px h-3 w-3 shrink-0" aria-hidden />
          <span className="min-w-0">
            {d.provenance.note} {d.provenance.resolution_disclaimer}
          </span>
        </p>
      )}
    </div>
  );
}
