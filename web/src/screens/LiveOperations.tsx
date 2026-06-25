import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Info } from "lucide-react";
import type MapView from "@arcgis/core/views/MapView";
import { getAdapter } from "@/data";
import { useTourStore } from "@/whatif/useTourStore";
import { getScript } from "@/whatif/scenarioScripts";
import type { Gate, TrafficSnapshot } from "@/lib/types";
import { ArcgisMap } from "@/components/map/ArcgisMap";
import { Card, CardContent } from "@/components/ui/card";
import { ThroughputChart } from "@/components/ThroughputChart";
import { KpiStrip } from "@/components/panels/KpiStrip";
import { CarbonTile } from "@/components/panels/CarbonTile";
import { EmptyContainerBoard } from "@/components/panels/EmptyContainerBoard";
import { TasWidget } from "@/components/panels/TasWidget";
import { ParkingBoard } from "@/components/panels/ParkingBoard";
import { AutoLeoPanel } from "@/components/panels/AutoLeoPanel";
import { DecisionPathBadge } from "@/components/DecisionPathBadge";
import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/misc";
import { severityColour } from "@/lib/palette";
import { MAP_TOKENS, STATUS } from "@/lib/tokens";
import { useClickOutside } from "@/hooks/useClickOutside";
import { useAlertFocus } from "@/lib/alertFocus";
import { useMapSettings } from "@/lib/mapSettings";

export default function LiveOperations() {
  const [view, setView] = useState<MapView | null>(null);
  // Operator-chosen basemap (header → Map settings); defaults to satellite.
  const { basemap } = useMapSettings();
  // The incident the header notification drawer asked us to focus, rendered as a
  // halo on the map. Cleared when no alert is focused.
  const focus = useAlertFocus();
  const [focusPoint, setFocusPoint] = useState<{ lat: number; lon: number } | null>(null);

  // Map spotlight follows the guided What-If tour, but ONLY when the current step
  // is a map-related business event (target.kind === "map"). DOM steps hand the
  // map an empty set, so the map is never highlighted as a generic default.
  const tour = useTourStore();
  const spotlight = useMemo(() => {
    if (!tour.scenarioId) return [];
    const t = getScript(tour.scenarioId)?.steps[tour.stepIndex]?.target;
    return t?.kind === "map" ? (t.mapAssets ?? []) : [];
  }, [tour.scenarioId, tour.stepIndex]);

  // When an alert is clicked in the header drawer, pan/zoom to it and ring it.
  // Re-runs on every focus (nonce) and once the view becomes ready.
  useEffect(() => {
    const a = focus.alert;
    const lat = a?.payload?.lat as number | undefined;
    const lon = a?.payload?.lon as number | undefined;
    if (!a || typeof lat !== "number" || typeof lon !== "number") {
      setFocusPoint(null);
      return;
    }
    setFocusPoint({ lat, lon });
    if (view) {
      void view
        .goTo({ center: [lon, lat], zoom: 15 }, { duration: 700, easing: "ease-in-out" })
        .catch(() => {});
    }
  }, [focus, view]);

  // All data now flows through the typed adapter (never the gateway directly).
  // Adapter methods return UNWRAPPED data (Gate[], TrafficSnapshot[], …).
  const corridorQ = useQuery({
    queryKey: ["corridor"],
    queryFn: () => getAdapter().corridor(),
    staleTime: Infinity,
  });
  const gatesQ = useQuery({ queryKey: ["gates"], queryFn: () => getAdapter().gates() });
  const snapsQ = useQuery({
    queryKey: ["snapshots"],
    queryFn: () => getAdapter().trafficSnapshots(),
  });
  const zonesQ = useQuery({ queryKey: ["zones"], queryFn: () => getAdapter().zones() });
  const trucksQ = useQuery({
    queryKey: ["trucks", "live-map"],
    queryFn: () => getAdapter().trucks(undefined, 500),
  });
  const queuedQ = useQuery({
    queryKey: ["trucks", "AT_GATE_QUEUE"],
    queryFn: () => getAdapter().trucks("AT_GATE_QUEUE", 500),
  });
  const parkingQ = useQuery({
    queryKey: ["parking-availability"],
    queryFn: () => getAdapter().parkingAvailability(),
  });
  // Prediction carries a decision_path → surfaced as a LIVE/SYNTHETIC badge.
  const predictQ = useQuery({
    queryKey: ["traffic-predict"],
    queryFn: () => getAdapter().trafficPredict(),
  });

  const gates: Gate[] = gatesQ.data ?? [];
  const snapshots: TrafficSnapshot[] = snapsQ.data ?? [];

  const queueByGate = new Map<string, number>();
  for (const t of queuedQ.data ?? []) {
    if (t.gate_id) queueByGate.set(t.gate_id, (queueByGate.get(t.gate_id) ?? 0) + 1);
  }

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      {/* KPI strip from the adapter (label/value/target/Δ%/sparkline). */}
      <div className="border-b border-border px-3 py-2.5">
        <div className="mb-2 flex items-center gap-2">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Corridor KPIs
          </h2>
          <DecisionPathBadge path={predictQ.data?.decision_path} />
        </div>
        <KpiStrip />
      </div>

      {/* Gate throughput + queue tiles. */}
      <div className="grid grid-cols-2 gap-2.5 border-b border-border px-3 py-2.5 md:grid-cols-5">
        {gates.map((g) => (
          <Card key={g.id}>
            <CardContent className="flex flex-col gap-1 py-3">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-muted-foreground">
                  {g.id.replace("G-", "")}
                </span>
                <Badge
                  colour={severityColour(g.utilisation && g.utilisation >= 1 ? "critical" : "ok")}
                >
                  {Math.round((g.utilisation ?? 0) * 100)}%
                </Badge>
              </div>
              <div className="text-xl font-semibold tabular-nums">
                {g.throughput_60min}
                <span className="ml-1 text-xs font-normal text-muted-foreground">
                  /{g.target_vph} vph
                </span>
              </div>
              <div className="text-[11px] text-muted-foreground">
                queue {queueByGate.get(g.id) ?? 0} · target {g.target_vph}/h
              </div>
            </CardContent>
          </Card>
        ))}
        <Card className="col-span-2 md:col-span-1">
          <CardContent className="flex h-full flex-col py-2">
            <span className="mb-1 text-[11px] font-medium text-muted-foreground">
              Throughput · trend
            </span>
            <div className="min-h-[64px] flex-1">
              <ThroughputChart />
            </div>
          </CardContent>
        </Card>
        {gatesQ.isLoading && (
          <div className="col-span-full flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner /> loading gate KPIs…
          </div>
        )}
      </div>

      {/* Full-bleed map — the primary visual element. Alerts now live in the
          header notification drawer; the map keeps its floating Layers (in-map)
          and Legend widgets. */}
      <div className="relative min-h-[540px] flex-1">
        <ArcgisMap
          basemap={basemap}
          corridor={corridorQ.data}
          gates={gates}
          zones={zonesQ.data}
          snapshots={snapshots}
          trucks={trucksQ.data}
          parkingFacilities={parkingQ.data}
          highlights={spotlight}
          focusPoint={focusPoint}
          onViewReady={setView}
        />
        <FloatingLegend />
      </div>

      {/* Appendix-C capability tiles (DTCCC view) + TAS appointment board. */}
      <div className="grid grid-cols-1 gap-2.5 border-t border-border px-3 py-2.5 lg:grid-cols-3">
        <CarbonTile />
        <ParkingBoard />
        <EmptyContainerBoard />
        <TasWidget />
      </div>
      <div className="grid grid-cols-1 gap-2.5 border-t border-border px-3 py-2.5 lg:grid-cols-2">
        <AutoLeoPanel />
      </div>
    </div>
  );
}

// Floating, expandable Legend (GIS-4): an icon at the bottom-left opens the
// legend card; an outside click (or the icon again) closes it. Contents
// unchanged from the previous always-on legend.
function FloatingLegend() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useClickOutside(ref, () => setOpen(false), open);
  const items = [
    { c: STATUS.ok, l: "free flow / on-target" },
    { c: STATUS.warning, l: "moderate" },
    { c: STATUS.critical, l: "congested / over-cap" },
    { c: MAP_TOKENS.truckFill, l: "trucks (1:50)" },
  ];
  return (
    <div ref={ref} className="absolute bottom-3 left-3 z-10">
      {open && (
        <div className="mb-2 rounded-md border border-border bg-card/95 p-2 text-[11px] shadow-lg backdrop-blur">
          <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Legend
          </div>
          {items.map((i) => (
            <div key={i.l} className="flex items-center gap-2">
              <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: i.c }} />
              {i.l}
            </div>
          ))}
        </div>
      )}
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-label="Toggle legend"
        aria-expanded={open}
        title="Legend"
        className="flex h-9 w-9 items-center justify-center rounded-md border border-border bg-card/90 text-foreground shadow-md backdrop-blur transition hover:bg-muted"
      >
        <Info className="h-4 w-4" />
      </button>
    </div>
  );
}
