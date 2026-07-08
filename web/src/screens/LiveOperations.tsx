import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Info, Radio, RefreshCw, Truck as TruckIcon, X } from "lucide-react";
import type MapView from "@arcgis/core/views/MapView";
import { getAdapter } from "@/data";
import { api } from "@/lib/api";
import { useTourStore } from "@/whatif/useTourStore";
import { getScript } from "@/whatif/scenarioScripts";
import { useSimStore } from "@/sim/useSimStore";
import type { Gate, TrafficSnapshot, TruckDevice, VehicleIntel } from "@/lib/types";
import { ArcgisMap } from "@/components/map/ArcgisMap";
import { Card, CardContent } from "@/components/ui/card";
import { ThroughputChart } from "@/components/ThroughputChart";
import { KpiStrip } from "@/components/panels/KpiStrip";
import { CarbonTile } from "@/components/panels/CarbonTile";
import { EmptyContainerBoard } from "@/components/panels/EmptyContainerBoard";
import { TasWidget } from "@/components/panels/TasWidget";
import { ParkingBoard } from "@/components/panels/ParkingBoard";
import { AutoLeoPanel, CustomsFeedPanel } from "@/components/panels/AutoLeoPanel";
import { DecisionPathBadge } from "@/components/DecisionPathBadge";
import { Badge } from "@/components/ui/badge";
import { Spinner, ErrorState, LoadingState, EmptyState } from "@/components/ui/misc";
import { PageContainer, PageHeader, SearchInput, FilterSelect, StatusChip, type Tone } from "@/components/ui/dtccc";
import { useSocket } from "@/hooks/SocketContext";
import { severityColour } from "@/lib/palette";
import { MAP_TOKENS, STATUS } from "@/lib/tokens";
import { useClickOutside } from "@/hooks/useClickOutside";
import { useAlertFocus } from "@/lib/alertFocus";
import { useMapSettings } from "@/lib/mapSettings";
import { fmtEta } from "@/lib/utils";

function stateTone(state: string): Tone {
  if (state === "AT_GATE_QUEUE") return "warn";
  if (state === "MOVING" || state.startsWith("EN_ROUTE") || state === "ENROUTE") return "ok";
  return "info";
}
function humanizeState(s: string): string {
  return s
    .toLowerCase()
    .split(/[_\s]+/)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

export default function LiveOperations() {
  const { t } = useTranslation();
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
      // Smooth pan + zoom-in to the selected queue/feed item (spec: zoom 16).
      void view
        .goTo({ center: [lon, lat], zoom: 16 }, { duration: 800, easing: "ease-in-out" })
        .catch(() => {});
    }
  }, [focus, view]);

  // All data now flows through the typed adapter (never the gateway directly).
  const corridorQ = useQuery({
    queryKey: ["corridor"],
    queryFn: () => getAdapter().corridor(),
    staleTime: Infinity,
  });
  const gatesQ = useQuery({
    queryKey: ["gates"],
    queryFn: () => getAdapter().gates(),
    refetchInterval: 10_000,
  });
  const snapsQ = useQuery({
    queryKey: ["snapshots"],
    queryFn: () => getAdapter().trafficSnapshots(),
    refetchInterval: 8_000,
  });
  const zonesQ = useQuery({ queryKey: ["zones"], queryFn: () => getAdapter().zones() });
  const trucksQ = useQuery({
    queryKey: ["trucks", "live-map"],
    queryFn: () => getAdapter().trucks(undefined, 500),
    refetchInterval: 5_000,
  });
  const queuedQ = useQuery({
    queryKey: ["trucks", "AT_GATE_QUEUE"],
    queryFn: () => getAdapter().trucks("AT_GATE_QUEUE", 500),
    refetchInterval: 6_000,
  });
  const parkingQ = useQuery({
    queryKey: ["parking-availability"],
    queryFn: () => getAdapter().parkingAvailability(),
    refetchInterval: 10_000,
  });
  // Prediction carries a decision_path → surfaced as a LIVE/SYNTHETIC badge.
  const predictQ = useQuery({
    queryKey: ["traffic-predict"],
    queryFn: () => getAdapter().trafficPredict(),
    refetchInterval: 15_000,
  });

  // --- WebSocket → cache bridge (real-time primary) ------------------------
  const qc = useQueryClient();
  const { status: wsStatus, subscribe } = useSocket();
  const lastInvalidatedRef = useRef<Record<string, number>>({});
  const invalidateThrottled = useCallback(
    (key: unknown[], tag: string) => {
      const now = Date.now();
      if (now - (lastInvalidatedRef.current[tag] ?? 0) < 2_000) return;
      lastInvalidatedRef.current[tag] = now;
      void qc.invalidateQueries({ queryKey: key });
    },
    [qc],
  );
  useEffect(() => {
    const unsub = subscribe((frame) => {
      if (frame.type === "truck_position") {
        invalidateThrottled(["trucks"], "trucks");
      } else if (frame.type === "traffic") {
        invalidateThrottled(["snapshots"], "snapshots");
      }
    });
    return unsub;
  }, [subscribe, invalidateThrottled]);

  const lastUpdated = Math.max(
    trucksQ.dataUpdatedAt || 0,
    snapsQ.dataUpdatedAt || 0,
    gatesQ.dataUpdatedAt || 0,
  );
  const anyFetching = trucksQ.isFetching || snapsQ.isFetching || gatesQ.isFetching;

  const gates: Gate[] = gatesQ.data ?? [];
  const snapshots: TrafficSnapshot[] = snapsQ.data ?? [];

  const queueByGate = new Map<string, number>();
  for (const t of queuedQ.data ?? []) {
    if (t.gate_id) queueByGate.set(t.gate_id, (queueByGate.get(t.gate_id) ?? 0) + 1);
  }

  // --- Simulator-driven map highlighting -----------------------------------
  const sim = useSimStore();
  const corridor = corridorQ.data;

  const simHighlights = useMemo(() => {
    const drivenGates = Object.entries(sim.gates)
      .filter(([, g]) => (g.queueLength ?? 0) > 0)
      .map(([id]) => id);
    const drivenSegs = Object.entries(sim.segments)
      .filter(([, s]) => (s.jamFactor ?? 0) > 0)
      .map(([id]) => id);
    return [...new Set([...sim.highlights, ...drivenGates, ...drivenSegs])];
  }, [sim.highlights, sim.gates, sim.segments]);

  const highlightLabels = useMemo(() => {
    const labels: Record<string, string> = {};
    for (const [id, g] of Object.entries(sim.gates)) {
      if ((g.queueLength ?? 0) > 0) labels[id] = `${id.replace("G-", "")} • ${g.queueLength}`;
    }
    for (const [id, s] of Object.entries(sim.segments)) {
      if ((s.jamFactor ?? 0) > 0) labels[id] = `${id} • jam ${s.jamFactor!.toFixed(1)}`;
    }
    return labels;
  }, [sim.gates, sim.segments]);

  const simFocusPoint = useMemo(() => {
    const id = sim.lastTouched;
    if (!id) return null;
    const g = gates.find((x) => x.id === id);
    if (g) return { lat: g.lat, lon: g.lon };
    const seg = corridor?.segments.find((s) => s.id === id);
    if (seg) {
      const a = (seg.start[0] + seg.end[0]) / 2;
      const b = (seg.start[1] + seg.end[1]) / 2;
      const lat = Math.abs(a) <= 30 ? a : b;
      const lon = Math.abs(a) <= 30 ? b : a;
      return { lat, lon };
    }
    return null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sim.lastTouched, sim.lastTouchedNonce, gates, corridor]);

  // --- Filter rail + vehicle selection (Traffic Operations split layout) ----
  const [fState, setFState] = useState("all");
  const [fGate, setFGate] = useState("all");
  const [vsearch, setVsearch] = useState("");
  const [selected, setSelected] = useState<TruckDevice | null>(null);

  const allTrucks = trucksQ.data ?? [];
  const stateOptions = useMemo(
    () => ["all", ...Array.from(new Set(allTrucks.map((v) => v.state)))],
    [allTrucks],
  );
  const gateOptions = useMemo(
    () => ["all", ...Array.from(new Set(allTrucks.map((v) => v.gate_id).filter((g): g is string => !!g)))],
    [allTrucks],
  );
  const filteredTrucks = useMemo(() => {
    const q = vsearch.trim().toLowerCase();
    return allTrucks.filter(
      (v) =>
        (fState === "all" || v.state === fState) &&
        (fGate === "all" || v.gate_id === fGate) &&
        (!q || `${v.plate ?? ""} ${v.device_id}`.toLowerCase().includes(q)),
    );
  }, [allTrucks, fState, fGate, vsearch]);

  // Detail lookup for the selected vehicle (RDS: Vahan/violations/challans).
  const intelQ = useQuery({
    queryKey: ["vehicle-intel", selected?.plate],
    queryFn: () => api.vehicleIntel(selected!.plate!),
    enabled: !!selected?.plate,
  });

  const selectedFocus = selected ? { lat: selected.position.lat, lon: selected.position.lon } : null;

  // Selecting a vehicle pans/zooms the map to it.
  useEffect(() => {
    if (selected && view) {
      void view
        .goTo({ center: [selected.position.lon, selected.position.lat], zoom: 15 }, { duration: 700, easing: "ease-in-out" })
        .catch(() => {});
    }
  }, [selected, view]);

  const mapHighlights = useMemo(() => [...spotlight, ...simHighlights], [spotlight, simHighlights]);
  const effectiveFocus = focusPoint ?? selectedFocus ?? simFocusPoint;

  return (
    <PageContainer>
      <PageHeader
        icon={TruckIcon}
        title={t("navGroup.traffic")}
        subtitle={t("app.corridor")}
        updatedAt={lastUpdated}
        isFetching={anyFetching}
        onRefresh={() => qc.invalidateQueries()}
        actions={
          <div className="flex items-center gap-2">
            <DecisionPathBadge path={predictQ.data?.decision_path} />
            <RealtimePill wsOpen={wsStatus === "open"} fetching={anyFetching} />
          </div>
        }
      />

      {/* KPI strip */}
      <div className="border-b border-border px-4 py-2.5">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {t("liveOps.corridorKpis")}
        </h2>
        <KpiStrip />
      </div>

      {/* Gate throughput + queue tiles. */}
      <div className="grid grid-cols-2 gap-2.5 border-b border-border px-4 py-2.5 md:grid-cols-5">
        {gates.map((g) => (
          <Card key={g.id}>
            <CardContent className="flex flex-col gap-1 py-3">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-muted-foreground">{g.id.replace("G-", "")}</span>
                <Badge colour={severityColour(g.utilisation && g.utilisation >= 1 ? "critical" : "ok")}>
                  {Math.round((g.utilisation ?? 0) * 100)}%
                </Badge>
              </div>
              <div className="text-xl font-semibold tabular-nums">
                {g.throughput_60min}
                <span className="ml-1 text-xs font-normal text-muted-foreground">
                  /{g.target_vph} {t("kpiUnit.vph")}
                </span>
              </div>
              <div className="text-[11px] text-muted-foreground">
                {t("liveOps.queue")} {queueByGate.get(g.id) ?? 0} · {t("liveOps.target")} {g.target_vph}/h
              </div>
            </CardContent>
          </Card>
        ))}
        <Card className="col-span-2 md:col-span-1">
          <CardContent className="flex h-full flex-col py-2">
            <span className="mb-1 text-[11px] font-medium text-muted-foreground">{t("liveOps.throughputTrend")}</span>
            <div className="min-h-[64px] flex-1">
              <ThroughputChart />
            </div>
          </CardContent>
        </Card>
        {gatesQ.isLoading && (
          <div className="col-span-full flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner /> {t("liveOps.loadingGateKpis")}
          </div>
        )}
        {gatesQ.isError && (
          <div className="col-span-full">
            <ErrorState onRetry={() => gatesQ.refetch()} detail={(gatesQ.error as Error)?.message} />
          </div>
        )}
      </div>

      {/* Split layout: filters + vehicle list | large live map. */}
      <div className="grid grid-cols-1 gap-3 px-4 py-3 lg:grid-cols-[300px_1fr]">
        <VehicleRail
          trucks={filteredTrucks}
          total={allTrucks.length}
          isLoading={trucksQ.isLoading}
          isError={trucksQ.isError}
          onRetry={() => trucksQ.refetch()}
          fState={fState}
          setFState={setFState}
          fGate={fGate}
          setFGate={setFGate}
          search={vsearch}
          setSearch={setVsearch}
          stateOptions={stateOptions}
          gateOptions={gateOptions}
          selected={selected}
          onSelect={setSelected}
        />
        <div className="relative min-h-[520px] overflow-hidden rounded-lg border border-border">
          <ArcgisMap
            basemap={basemap}
            corridor={corridorQ.data}
            gates={gates}
            zones={zonesQ.data}
            snapshots={snapshots}
            trucks={filteredTrucks}
            parkingFacilities={parkingQ.data}
            highlights={mapHighlights}
            highlightLabels={highlightLabels}
            focusPoint={effectiveFocus}
            onViewReady={setView}
          />
          <FloatingLegend />
        </div>
      </div>

      {/* Selected-vehicle detail (Trip / ETA / Driver / History / Violations). */}
      {selected && (
        <div className="px-4 pb-3">
          <VehicleDetail truck={selected} intel={intelQ.data} status={intelQ} onClose={() => setSelected(null)} />
        </div>
      )}

      {/* Appendix-C capability tiles (DTCCC view). */}
      <div className="grid grid-cols-1 gap-2.5 border-t border-border px-4 py-2.5 md:grid-cols-2 lg:grid-cols-3">
        <CarbonTile />
        <ParkingBoard />
        <EmptyContainerBoard />
      </div>

      {/* Operations row — TAS, Auto-LEO gate-out queue, Customs alert feed. */}
      <div className="grid grid-cols-1 items-stretch gap-2.5 border-t border-border px-4 py-2.5 md:grid-cols-2 lg:grid-cols-3">
        <TasWidget />
        <AutoLeoPanel />
        <CustomsFeedPanel />
      </div>
    </PageContainer>
  );
}

// --- Filter rail + vehicle list ---------------------------------------------

function VehicleRail({
  trucks,
  total,
  isLoading,
  isError,
  onRetry,
  fState,
  setFState,
  fGate,
  setFGate,
  search,
  setSearch,
  stateOptions,
  gateOptions,
  selected,
  onSelect,
}: {
  trucks: TruckDevice[];
  total: number;
  isLoading: boolean;
  isError: boolean;
  onRetry: () => void;
  fState: string;
  setFState: (v: string) => void;
  fGate: string;
  setFGate: (v: string) => void;
  search: string;
  setSearch: (v: string) => void;
  stateOptions: string[];
  gateOptions: string[];
  selected: TruckDevice | null;
  onSelect: (v: TruckDevice) => void;
}) {
  return (
    <Card className="flex max-h-[520px] flex-col overflow-hidden">
      <div className="space-y-2 border-b border-border p-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-foreground">Vehicles</h2>
          <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-bold tabular-nums text-muted-foreground">
            {trucks.length}/{total}
          </span>
        </div>
        <SearchInput value={search} onChange={setSearch} placeholder="Plate or device…" />
        <div className="grid grid-cols-2 gap-2">
          <FilterSelect
            value={fState}
            onChange={setFState}
            label="Status"
            options={stateOptions.map((s) => ({ value: s, label: s === "all" ? "All status" : humanizeState(s) }))}
          />
          <FilterSelect
            value={fGate}
            onChange={setFGate}
            label="Gate"
            options={gateOptions.map((g) => ({ value: g, label: g === "all" ? "All gates" : g.replace("G-", "") }))}
          />
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {isLoading ? (
          <LoadingState />
        ) : isError ? (
          <ErrorState onRetry={onRetry} />
        ) : trucks.length === 0 ? (
          <EmptyState>No vehicles match these filters.</EmptyState>
        ) : (
          <ul className="divide-y divide-border">
            {trucks.slice(0, 200).map((v) => {
              const active = selected?.device_id === v.device_id;
              return (
                <li key={v.device_id}>
                  <button
                    type="button"
                    onClick={() => onSelect(v)}
                    className={`flex w-full items-center justify-between gap-2 px-3 py-2 text-left transition-colors ${
                      active ? "bg-primary/10" : "hover:bg-muted/50"
                    }`}
                  >
                    <div className="min-w-0">
                      <div className="truncate font-mono text-[13px] font-medium text-foreground">{v.plate ?? v.device_id}</div>
                      <div className="truncate text-[11px] text-muted-foreground">{v.gate_id ?? v.segment_id ?? "—"}</div>
                    </div>
                    <div className="flex shrink-0 flex-col items-end gap-1">
                      <StatusChip label={humanizeState(v.state)} tone={stateTone(v.state)} />
                      <span className="text-[11px] tabular-nums text-muted-foreground">{fmtEta(v.eta_s)}</span>
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </Card>
  );
}

// --- Selected-vehicle detail -------------------------------------------------

function VehicleDetail({
  truck,
  intel,
  status,
  onClose,
}: {
  truck: TruckDevice;
  intel?: VehicleIntel;
  status: { isLoading: boolean; isError: boolean };
  onClose: () => void;
}) {
  const rc = (intel?.rc ?? {}) as Record<string, any>;
  const owner = rc.owner_name ?? rc.owner ?? rc.ownerName;
  const rtoClass = rc.vehicle_class ?? rc.vehicleClass ?? rc.class;
  const violations = intel?.violations ?? [];
  const challans = intel?.challans ?? [];
  const tracking = intel?.tracking ?? [];

  return (
    <Card className="overflow-hidden">
      <div className="flex items-center justify-between gap-2 border-b border-border bg-muted/40 px-4 py-2.5">
        <div className="flex items-center gap-2">
          <TruckIcon className="h-4 w-4 text-primary" />
          <span className="font-mono text-sm font-semibold text-foreground">{truck.plate ?? truck.device_id}</span>
          <StatusChip label={humanizeState(truck.state)} tone={stateTone(truck.state)} />
        </div>
        <button type="button" onClick={onClose} aria-label="Close" className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground">
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="grid grid-cols-2 gap-x-6 gap-y-4 p-4 md:grid-cols-4">
        <DetailGroup title="Trip">
          <DetailRow label="Gate" value={truck.gate_id ?? "—"} />
          <DetailRow label="Segment" value={truck.segment_id ?? "—"} />
          <DetailRow label="Remaining" value={`${truck.remaining_km.toFixed(1)} km`} />
          <DetailRow label="Speed" value={`${Math.round(truck.speed_kmh)} km/h`} />
        </DetailGroup>
        <DetailGroup title="ETA">
          <DetailRow label="To gate" value={fmtEta(truck.eta_s)} />
          <DetailRow label="Heading" value={`${Math.round(truck.heading)}°`} />
          <DetailRow label="Position" value={`${truck.position.lat.toFixed(3)}, ${truck.position.lon.toFixed(3)}`} />
        </DetailGroup>
        <DetailGroup title="Driver / RC">
          {status.isLoading ? (
            <span className="text-xs text-muted-foreground">Loading…</span>
          ) : (
            <>
              <DetailRow label="Owner" value={owner ?? "—"} />
              <DetailRow label="Class" value={rtoClass ?? "—"} />
              <DetailRow label="RTO" value={rc.rto ?? rc.rto_name ?? "—"} />
            </>
          )}
        </DetailGroup>
        <DetailGroup title="Enforcement">
          <DetailRow label="Violations" value={String(violations.length)} tone={violations.length ? "warn" : "ok"} />
          <DetailRow label="Challans" value={String(challans.length)} tone={challans.length ? "warn" : "ok"} />
          <DetailRow label="Track points" value={String(tracking.length)} />
        </DetailGroup>
      </div>

      {violations.length > 0 && (
        <div className="border-t border-border px-4 py-3">
          <h4 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Recent Violations</h4>
          <div className="flex flex-wrap gap-1.5">
            {violations.slice(0, 12).map((v, i) => (
              <StatusChip key={i} label={String((v as any).kind ?? (v as any).type ?? (v as any).violation ?? "violation")} tone="warn" />
            ))}
          </div>
        </div>
      )}
      {status.isError && (
        <div className="border-t border-border px-4 py-2 text-xs text-muted-foreground">
          Vehicle intelligence unavailable for this plate.
        </div>
      )}
    </Card>
  );
}

function DetailGroup({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h4 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h4>
      <div className="space-y-1">{children}</div>
    </div>
  );
}

function DetailRow({ label, value, tone }: { label: string; value: React.ReactNode; tone?: Tone }) {
  return (
    <div className="flex items-baseline justify-between gap-2 text-[13px]">
      <span className="text-muted-foreground">{label}</span>
      <span className="truncate font-medium" style={tone ? { color: STATUS[tone === "ok" ? "ok" : tone === "warn" ? "warning" : tone === "critical" ? "critical" : "info"] } : undefined}>
        {value}
      </span>
    </div>
  );
}

// Real-time connectivity pill.
function RealtimePill({ wsOpen, fetching }: { wsOpen: boolean; fetching?: boolean }) {
  const { t } = useTranslation();
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border border-border px-2 py-0.5 text-[10px] font-medium"
      title={wsOpen ? t("liveOps.rtLiveHint") : t("liveOps.rtPollHint")}
    >
      {wsOpen ? (
        <Radio className="h-3 w-3 text-emerald-500" />
      ) : (
        <RefreshCw className={`h-3 w-3 text-amber-500 ${fetching ? "animate-spin" : ""}`} />
      )}
      {wsOpen ? t("liveOps.rtLive") : t("liveOps.rtPolling")}
    </span>
  );
}

// Floating, expandable Legend (GIS-4).
function FloatingLegend() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useClickOutside(ref, () => setOpen(false), open);
  const items = [
    { c: STATUS.ok, l: t("map.legendItem.freeFlow") },
    { c: STATUS.warning, l: t("map.legendItem.moderate") },
    { c: STATUS.critical, l: t("map.legendItem.congested") },
    { c: MAP_TOKENS.truckFill, l: t("map.legendItem.trucks") },
  ];
  return (
    <div ref={ref} className="absolute bottom-3 left-3 z-10">
      {open && (
        <div className="mb-2 rounded-md border border-border bg-card/95 p-2 text-[11px] shadow-lg backdrop-blur">
          <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{t("map.legend")}</div>
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
        aria-label={t("map.toggleLegend")}
        aria-expanded={open}
        title={t("map.legend")}
        className="flex h-9 w-9 items-center justify-center rounded-md border border-border bg-card/90 text-foreground shadow-md backdrop-blur transition hover:bg-muted"
      >
        <Info className="h-4 w-4" />
      </button>
    </div>
  );
}
