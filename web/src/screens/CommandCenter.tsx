// Command Center — the DTCCC landing page (FINAL PHASE redesign).
//
// Single operator-facing overview that composes EXISTING RDS-backed adapter
// calls (no backend change): a 10-tile KPI header, the large live GIS map, and a
// bottom dashboard of Top-20 active vehicles, Top-10 critical alerts and gate /
// parking / customs summaries. Everything is capped to a "top N" with a View-all
// link into the dedicated screen, per the RDS-presentation rules.

import { useMemo } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Truck,
  Users,
  Container,
  ArrowLeftRight,
  Timer,
  SquareParking,
  ShieldAlert,
  ScanEye,
  FileWarning,
  Leaf,
  RefreshCw,
  ChevronRight,
  AlertTriangle,
  TrafficCone,
  Gauge,
  Snowflake,
  Repeat,
  Camera,
} from "lucide-react";
import { getAdapter } from "@/data";
import { api } from "@/lib/api";
import { ArcgisMap } from "@/components/map/ArcgisMap";
import { Card } from "@/components/ui/card";
import { LastUpdated, LoadingState, ErrorState, EmptyState } from "@/components/ui/misc";
import { AutoRefreshControl } from "@/components/ui/AutoRefreshControl";
import { useMapSettings } from "@/lib/mapSettings";
import { severityColour, severityRank } from "@/lib/palette";
import { STATUS } from "@/lib/tokens";
import { relativeAge } from "@/lib/utils";
import { cn } from "@/lib/utils";
import type { Alert, Gate, TruckDevice } from "@/lib/types";

type Tone = "info" | "ok" | "warn" | "critical";
const TONE_COLOUR: Record<Tone, string> = {
  info: STATUS.info,
  ok: STATUS.ok,
  warn: STATUS.warning,
  critical: STATUS.critical,
};

export default function CommandCenter() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const { basemap } = useMapSettings();

  // --- Data (all via the typed adapter; RDS-backed in live mode) -----------
  const corridorQ = useQuery({
    queryKey: ["corridor"],
    queryFn: () => getAdapter().corridor(),
    staleTime: Infinity,
  });
  const gatesQ = useQuery({
    queryKey: ["gates"],
    queryFn: () => getAdapter().gates(),
  });
  const zonesQ = useQuery({ queryKey: ["zones"], queryFn: () => getAdapter().zones() });
  const snapsQ = useQuery({
    queryKey: ["snapshots"],
    queryFn: () => getAdapter().trafficSnapshots(),
  });
  const trucksQ = useQuery({
    queryKey: ["trucks", "live-map"],
    queryFn: () => getAdapter().trucks(undefined, 500),
  });
  const queuedQ = useQuery({
    queryKey: ["trucks", "AT_GATE_QUEUE"],
    queryFn: () => getAdapter().trucks("AT_GATE_QUEUE", 500),
  });
  const parkingAvailQ = useQuery({
    queryKey: ["parking-availability"],
    queryFn: () => getAdapter().parkingAvailability(),
  });
  // Customs Flags + AI Incidents read the SAME endpoints the Customs / Reports /
  // Geo screens use, so these KPIs match those screens exactly. Alerts share the
  // ["alerts-seed"] cache with the header bell and Alerts Center at ONE limit so
  // the count never diverges between them.
  const customsQ = useQuery({
    queryKey: ["customs-history"],
    queryFn: () => api.customsHistory(200),
  });
  const aiQ = useQuery({
    queryKey: ["ai-events"],
    queryFn: () => api.aiEvents(undefined, 200),
  });
  const alertsQ = useQuery({
    queryKey: ["alerts-seed"],
    queryFn: () => getAdapter().alerts({ limit: 100 }),
  });
  const carbonQ = useQuery({ queryKey: ["carbon"], queryFn: () => getAdapter().carbonRollup() });
  const leoQ = useQuery({
    queryKey: ["leo-queue"],
    queryFn: () => getAdapter().leoQueue(),
  });
  const enrollQ = useQuery({
    queryKey: ["enrollments"],
    queryFn: () => getAdapter().enrollments(),
  });
  const violationsQ = useQuery({
    queryKey: ["police-report"],
    queryFn: () => getAdapter().policeReport(),
  });

  // --- UC-III KPI cards (additive) — reuse the existing api methods only ----
  const accidentQ = useQuery({
    queryKey: ["cc-accident-dashboard"],
    queryFn: () => api.accidentDashboard(),
  });
  const bottlenecksQ = useQuery({
    queryKey: ["cc-bottlenecks", 3],
    queryFn: () => api.bottlenecks(3),
  });
  const trtQ = useQuery({ queryKey: ["cc-trt-summary"], queryFn: () => api.trtSummary() });
  const reeferQ = useQuery({
    queryKey: ["cc-reefer-availability"],
    queryFn: () => api.reeferAvailability(),
  });
  const doubleTripQ = useQuery({
    queryKey: ["cc-double-trip-stats"],
    queryFn: () => api.doubleTripStatistics(),
  });
  const cameraQ = useQuery({
    queryKey: ["cc-camera-dashboard"],
    queryFn: () => api.cameraDashboard(),
  });

  const trucks = trucksQ.data ?? [];
  const gates = gatesQ.data ?? [];
  const alerts = alertsQ.data ?? [];
  const enrollments = enrollQ.data ?? [];

  // plate → driver name, from the enrollment register (real RDS link).
  const driverByPlate = useMemo(() => {
    const m = new Map<string, string>();
    for (const e of enrollments)
      if (e.vehicle_no && e.name) m.set(e.vehicle_no.toUpperCase(), e.name);
    return m;
  }, [enrollments]);

  const avgEtaMin = useMemo(() => {
    const etas = trucks
      .map((v) => v.eta_s)
      .filter((s): s is number => typeof s === "number" && s > 0);
    if (etas.length === 0) return null;
    return Math.round(etas.reduce((a, b) => a + b, 0) / etas.length / 60);
  }, [trucks]);

  const activeDrivers = useMemo(() => {
    const enrolled = enrollments.filter((e) => e.status === "ACTIVE").length;
    const distinctPlates = new Set(trucks.map((v) => v.plate).filter(Boolean)).size;
    return Math.max(enrolled, distinctPlates);
  }, [enrollments, trucks]);

  const aiIncidents = aiQ.data?.count ?? 0;
  const customsFlags = customsQ.data?.alerts?.length ?? 0;
  // Parking totals summed from the facility list — robust regardless of the
  // /api/parking/summary field naming, and identical to the Parking screen's
  // facility table totals.
  const facilities = parkingAvailQ.data ?? [];
  const parkingTotals = useMemo(
    () => ({
      capacity: facilities.reduce((s, f) => s + (f.capacity ?? 0), 0),
      occupied: facilities.reduce((s, f) => s + (f.occupied ?? 0), 0),
      available: facilities.reduce((s, f) => s + (f.available ?? 0), 0),
      full: facilities.filter((f) => f.status === "FULL").length,
    }),
    [facilities],
  );
  const carbon = carbonQ.data;

  // "Today's Violations" = incidents dated today (IST) — matches Reports' "Today's Cases".
  const todaysViolations = useMemo(() => {
    const list = violationsQ.data ?? [];
    const today = new Date().toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata" });
    return list.filter(
      (i) => new Date(i.ts).toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata" }) === today,
    ).length;
  }, [violationsQ.data]);

  const lastUpdated = Math.max(
    trucksQ.dataUpdatedAt || 0,
    gatesQ.dataUpdatedAt || 0,
    snapsQ.dataUpdatedAt || 0,
    alertsQ.dataUpdatedAt || 0,
  );
  const anyFetching = trucksQ.isFetching || gatesQ.isFetching || alertsQ.isFetching;

  const kpis: { key: string; icon: typeof Truck; value: string; tone: Tone; loading: boolean }[] = [
    {
      key: "activeVehicles",
      icon: Truck,
      value: fmt(trucks.length),
      tone: "info",
      loading: trucksQ.isLoading,
    },
    {
      key: "activeDrivers",
      icon: Users,
      value: fmt(activeDrivers),
      tone: "info",
      loading: enrollQ.isLoading && trucksQ.isLoading,
    },
    {
      key: "activeContainers",
      icon: Container,
      value: fmt(leoQ.data?.length),
      tone: "info",
      loading: leoQ.isLoading,
    },
    {
      key: "gateQueue",
      icon: ArrowLeftRight,
      value: fmt(queuedQ.data?.length),
      tone: (queuedQ.data?.length ?? 0) > 40 ? "warn" : "info",
      loading: queuedQ.isLoading,
    },
    {
      key: "avgEta",
      icon: Timer,
      value: avgEtaMin != null ? `${avgEtaMin} min` : "—",
      tone: "info",
      loading: trucksQ.isLoading,
    },
    {
      key: "parkingAvailable",
      icon: SquareParking,
      value: fmt(parkingTotals.available),
      tone: "ok",
      loading: parkingAvailQ.isLoading,
    },
    {
      key: "customsFlags",
      icon: ShieldAlert,
      value: fmt(customsFlags),
      tone: customsFlags > 0 ? "warn" : "ok",
      loading: customsQ.isLoading,
    },
    {
      key: "aiIncidents",
      icon: ScanEye,
      value: fmt(aiIncidents),
      tone: aiIncidents > 0 ? "warn" : "ok",
      loading: aiQ.isLoading,
    },
    {
      key: "todaysViolations",
      icon: FileWarning,
      value: fmt(todaysViolations),
      tone: todaysViolations > 0 ? "warn" : "ok",
      loading: violationsQ.isLoading,
    },
    {
      key: "carbon",
      icon: Leaf,
      value: carbon ? fmtTonnes(carbon.total_kg) : "—",
      tone: "ok",
      loading: carbonQ.isLoading,
    },
  ];

  // UC-III cards derive defensively from `any` payloads so they never crash and
  // degrade to 0 / "—" on error or while loading.
  const accOpen = Number(accidentQ.data?.open ?? 0);
  const accTotal = Number(accidentQ.data?.total ?? 0);

  const bottleneckList: any[] = bottlenecksQ.data?.bottlenecks ?? [];
  const topJam = bottleneckList[0];
  const topJamLabel = topJam ? (topJam.name ?? topJam.segment_id ?? "—") : null;

  const trtMin = trtQ.data?.avg_trt_min != null ? Math.round(Number(trtQ.data.avg_trt_min)) : null;
  const trtLive = trtQ.data?.source === "live";

  const reeferTotals = reeferQ.data?.totals ?? {};
  const reeferAvail = Number(reeferTotals.available ?? 0);
  const reeferTotal = Number(reeferTotals.total ?? 0);
  const reeferFreePct =
    reeferTotals.free_pct != null ? Math.round(Number(reeferTotals.free_pct)) : null;

  const dtCycles = Number(doubleTripQ.data?.double_trip_cycles ?? 0);
  const dtTotal = Number(doubleTripQ.data?.total_cycles ?? 0);

  const camValid = Number(cameraQ.data?.container_reads?.valid ?? 0);
  const camTrailer = Number(cameraQ.data?.trailer_reads ?? 0);

  const uc3Cards: {
    key: string;
    icon: typeof Truck;
    value: string;
    label: string;
    tone: Tone;
    loading: boolean;
    to: string;
  }[] = [
    {
      key: "activeAccidents",
      icon: AlertTriangle,
      value: fmt(accOpen),
      label: `Active Accidents · ${fmt(accTotal)} total`,
      tone: accOpen > 0 ? "critical" : "ok",
      loading: accidentQ.isLoading,
      to: "/alerts?tab=accidents",
    },
    {
      key: "activeBottlenecks",
      icon: TrafficCone,
      value: fmt(bottleneckList.length),
      label: topJamLabel ? `Bottlenecks · ${topJamLabel}` : "Active Bottlenecks",
      tone: bottleneckList.length > 0 ? "warn" : "ok",
      loading: bottlenecksQ.isLoading,
      to: "/geofencing?tab=bottlenecks",
    },
    {
      key: "ecyTrt",
      icon: Gauge,
      value: trtMin != null ? `${trtMin} min` : "—",
      label: `ECY TRT · ${trtLive ? "Live" : "Baseline"}`,
      tone: "info",
      loading: trtQ.isLoading,
      to: "/live?tab=trt",
    },
    {
      key: "reefer",
      icon: Snowflake,
      value: `${fmt(reeferAvail)} / ${fmt(reeferTotal)}`,
      label: reeferFreePct != null ? `Reefer · ${reeferFreePct}% free` : "Reefer Available",
      tone: "ok",
      loading: reeferQ.isLoading,
      to: "/parking?tab=reefer",
    },
    {
      key: "doubleTrip",
      icon: Repeat,
      value: `${fmt(dtCycles)} / ${fmt(dtTotal)}`,
      label: "Double-Trip Cycles",
      tone: "info",
      loading: doubleTripQ.isLoading,
      to: "/live?tab=double-trip",
    },
    {
      key: "cameraAi",
      icon: Camera,
      value: fmt(camValid),
      label: `Camera AI · ${fmt(camTrailer)} trailer`,
      tone: "info",
      loading: cameraQ.isLoading,
      to: "/gate-customs",
    },
  ];

  // Top-20 active vehicles: queued first, then soonest ETA.
  const topVehicles = useMemo(() => {
    return [...trucks]
      .sort((a, b) => {
        const qa = a.state === "AT_GATE_QUEUE" ? 0 : 1;
        const qb = b.state === "AT_GATE_QUEUE" ? 0 : 1;
        if (qa !== qb) return qa - qb;
        return (a.eta_s ?? Infinity) - (b.eta_s ?? Infinity);
      })
      .slice(0, 20);
  }, [trucks]);

  // Top-10 critical alerts by severity rank (highest first).
  const topAlerts = useMemo(
    () =>
      [...alerts].sort((a, b) => severityRank(b.severity) - severityRank(a.severity)).slice(0, 10),
    [alerts],
  );

  function refreshAll() {
    void qc.invalidateQueries();
  }

  return (
    <div className="flex h-full flex-col overflow-y-auto bg-background">
      {/* Page header ---------------------------------------------------- */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border bg-card px-4 py-3">
        <div>
          <h1 className="text-lg font-bold tracking-tight text-foreground">
            {t("commandCenter.title")}
          </h1>
          <p className="text-xs text-muted-foreground">{t("commandCenter.subtitle")}</p>
        </div>
        <div className="ml-auto flex items-center gap-3">
          <LastUpdated at={lastUpdated || undefined} isFetching={anyFetching} />
          <AutoRefreshControl />
          <button
            type="button"
            onClick={refreshAll}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-muted"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", anyFetching && "animate-spin")} />
            {t("commandCenter.refresh")}
          </button>
        </div>
      </div>

      {/* Needs Attention — Phase-9 priority band. Reuses the queries already
          fetched above (no new API calls); surfaces only what an operator must
          act on now, most-critical first, each deep-linking to its screen. */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border bg-muted/30 px-4 py-2">
        <span className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          <AlertTriangle className="h-3.5 w-3.5 text-amber-600" /> Needs Attention
        </span>
        {(() => {
          const items: { label: string; crit: boolean; to: string }[] = [];
          const openAcc = (accidentQ.data as any)?.open ?? 0;
          if (openAcc > 0)
            items.push({
              label: `${openAcc} open accident${openAcc > 1 ? "s" : ""}`,
              crit: true,
              to: "/alerts?tab=accidents",
            });
          const crit = ((alertsQ.data as any)?.alerts ?? []).filter(
            (a: any) => a?.severity === "critical",
          ).length;
          if (crit > 0)
            items.push({
              label: `${crit} critical alert${crit > 1 ? "s" : ""}`,
              crit: true,
              to: "/alerts",
            });
          const q = (queuedQ.data as any)?.count ?? (queuedQ.data as any)?.devices?.length ?? 0;
          if (q >= 15) items.push({ label: `Gate queue ${q}`, crit: false, to: "/live" });
          const bn = (bottlenecksQ.data as any)?.bottlenecks?.[0];
          if (bn && (bn.jam_factor ?? 0) >= 6)
            items.push({
              label: `Bottleneck: ${bn.name ?? bn.segment_id}`,
              crit: false,
              to: "/geofencing",
            });
          const trt = (trtQ.data as any)?.avg_trt_min ?? 0;
          if (trt >= 120)
            items.push({ label: `TRT ${Math.round(trt)} min`, crit: false, to: "/live?tab=trt" });
          if (items.length === 0)
            return (
              <span className="rounded-full border border-emerald-300 bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700">
                All clear
              </span>
            );
          return items.map((it, i) => (
            <Link
              key={i}
              to={it.to}
              className={
                "rounded-full border px-2.5 py-0.5 text-xs font-medium transition-opacity hover:opacity-80 " +
                (it.crit
                  ? "border-red-300 bg-red-50 text-red-700"
                  : "border-amber-300 bg-amber-50 text-amber-700")
              }
            >
              {it.label}
            </Link>
          ));
        })()}
      </div>

      {/* KPI tiles ------------------------------------------------------- */}
      <div className="grid grid-cols-2 gap-2.5 px-4 py-3 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-5">
        {kpis.map((k) => (
          <KpiTile
            key={k.key}
            icon={k.icon}
            label={t(`commandCenter.kpi.${k.key}`)}
            value={k.value}
            tone={k.tone}
            loading={k.loading}
          />
        ))}
        {/* UC-III KPI cards (additive) — each links into its host screen. */}
        {uc3Cards.map((c) => (
          <Link key={c.key} to={c.to} className="block transition-opacity hover:opacity-90">
            <KpiTile
              icon={c.icon}
              label={c.label}
              value={c.value}
              tone={c.tone}
              loading={c.loading}
            />
          </Link>
        ))}
      </div>

      {/* Large live map ------------------------------------------------- */}
      <div className="px-4 pb-3">
        <Card className="relative h-[46vh] min-h-[360px] overflow-hidden p-0">
          <ArcgisMap
            basemap={basemap}
            corridor={corridorQ.data}
            gates={gates}
            zones={zonesQ.data}
            snapshots={snapsQ.data}
            trucks={trucks}
            parkingFacilities={parkingAvailQ.data}
          />
          {/* Never a blank card: overlay while the corridor geometry loads or if it fails. */}
          {(corridorQ.isLoading || corridorQ.isError) && (
            <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center bg-background/70 backdrop-blur-sm">
              {corridorQ.isError ? (
                <div className="pointer-events-auto">
                  <ErrorState
                    onRetry={() => corridorQ.refetch()}
                    detail={(corridorQ.error as Error)?.message}
                  />
                </div>
              ) : (
                <LoadingState
                  label={t("commandCenter.mapLoading", "Waiting for live corridor data…")}
                />
              )}
            </div>
          )}
        </Card>
      </div>

      {/* Bottom dashboard: vehicles + alerts ---------------------------- */}
      <div className="grid grid-cols-1 gap-3 px-4 pb-3 lg:grid-cols-3">
        <SectionCard
          className="lg:col-span-2"
          title={t("commandCenter.topVehicles")}
          count={trucks.length}
          to="/live"
          viewAll={t("commandCenter.viewAll")}
        >
          <VehiclesTable
            vehicles={topVehicles}
            driverByPlate={driverByPlate}
            loading={trucksQ.isLoading}
            error={trucksQ.isError}
            onRetry={() => trucksQ.refetch()}
            lastUpdated={trucksQ.dataUpdatedAt}
          />
        </SectionCard>

        <SectionCard
          title={t("commandCenter.topAlerts")}
          count={alerts.length}
          to="/alerts"
          viewAll={t("commandCenter.viewAll")}
        >
          <AlertsList
            alerts={topAlerts}
            loading={alertsQ.isLoading}
            error={alertsQ.isError}
            onRetry={() => alertsQ.refetch()}
          />
        </SectionCard>
      </div>

      {/* Summaries: gate / parking / customs ---------------------------- */}
      <div className="grid grid-cols-1 gap-3 px-4 pb-6 md:grid-cols-3">
        <GateSummary gates={gates} queued={queuedQ.data?.length ?? 0} loading={gatesQ.isLoading} />
        <ParkingSummaryCard
          capacity={parkingTotals.capacity}
          occupied={parkingTotals.occupied}
          available={parkingTotals.available}
          full={parkingTotals.full}
          loading={parkingAvailQ.isLoading}
        />
        <CustomsSummary leo={leoQ.data ?? []} flags={customsFlags} loading={leoQ.isLoading} />
      </div>
    </div>
  );
}

// --- KPI tile ----------------------------------------------------------------

function KpiTile({
  icon: Icon,
  label,
  value,
  tone,
  loading,
}: {
  icon: typeof Truck;
  label: string;
  value: string;
  tone: Tone;
  loading: boolean;
}) {
  const colour = TONE_COLOUR[tone];
  return (
    <Card className="flex items-center gap-3 p-3">
      <span
        className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg"
        style={{ backgroundColor: `${colour}1a`, color: colour }}
      >
        <Icon className="h-5 w-5" strokeWidth={2} />
      </span>
      <div className="min-w-0">
        <div className="text-xl font-bold tabular-nums leading-none text-foreground">
          {loading ? <span className="text-muted-foreground">…</span> : value}
        </div>
        <div className="mt-1 truncate text-[11px] font-medium text-muted-foreground" title={label}>
          {label}
        </div>
      </div>
    </Card>
  );
}

// --- Reusable section card (header + View all + body) ------------------------

function SectionCard({
  title,
  count,
  to,
  viewAll,
  className,
  children,
}: {
  title: string;
  count?: number;
  to: string;
  viewAll: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <Card className={cn("flex min-h-0 flex-col overflow-hidden", className)}>
      <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold tracking-tight text-foreground">{title}</h2>
          {typeof count === "number" && (
            <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-bold tabular-nums text-muted-foreground">
              {fmt(count)}
            </span>
          )}
        </div>
        <Link
          to={to}
          className="inline-flex items-center gap-0.5 text-[11px] font-semibold text-primary transition-colors hover:underline"
        >
          {viewAll}
          <ChevronRight className="h-3.5 w-3.5" />
        </Link>
      </div>
      <div className="min-h-0 flex-1">{children}</div>
    </Card>
  );
}

// --- Vehicles table ----------------------------------------------------------

function VehiclesTable({
  vehicles,
  driverByPlate,
  loading,
  error,
  onRetry,
  lastUpdated,
}: {
  vehicles: TruckDevice[];
  driverByPlate: Map<string, string>;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
  lastUpdated?: number;
}) {
  const { t } = useTranslation();
  if (loading) return <LoadingState />;
  if (error) return <ErrorState onRetry={onRetry} />;
  if (vehicles.length === 0) return <EmptyState>{t("commandCenter.noVehicles")}</EmptyState>;
  const age = lastUpdated ? relativeAge(new Date(lastUpdated).toISOString()) : "—";
  return (
    <div className="max-h-[320px] overflow-auto">
      <table className="w-full text-left text-[13px]">
        <thead className="sticky top-0 z-10 bg-muted/80 text-[11px] uppercase tracking-wide text-muted-foreground backdrop-blur">
          <tr>
            <th className="px-3 py-1.5 font-semibold">{t("commandCenter.col.vehicle")}</th>
            <th className="px-3 py-1.5 font-semibold">{t("commandCenter.col.driver")}</th>
            <th className="px-3 py-1.5 font-semibold">{t("commandCenter.col.location")}</th>
            <th className="px-3 py-1.5 text-right font-semibold">{t("commandCenter.col.eta")}</th>
            <th className="px-3 py-1.5 font-semibold">{t("commandCenter.col.status")}</th>
            <th className="px-3 py-1.5 font-semibold">{t("commandCenter.col.updated")}</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {vehicles.map((v) => {
            const plate = v.plate ?? v.device_id;
            const driver = (v.plate && driverByPlate.get(v.plate.toUpperCase())) || "—";
            const loc =
              v.gate_id ??
              v.segment_id ??
              `${v.position.lat.toFixed(3)}, ${v.position.lon.toFixed(3)}`;
            return (
              <tr key={v.device_id} className="hover:bg-muted/40">
                <td className="px-3 py-1.5 font-mono font-medium text-foreground">{plate}</td>
                <td className="px-3 py-1.5 text-muted-foreground">{driver}</td>
                <td className="px-3 py-1.5 text-muted-foreground">{loc}</td>
                <td className="px-3 py-1.5 text-right tabular-nums">
                  {v.eta_s != null ? `${Math.round(v.eta_s / 60)}m` : "—"}
                </td>
                <td className="px-3 py-1.5">
                  <StateChip state={v.state} />
                </td>
                <td className="px-3 py-1.5 text-[11px] text-muted-foreground">{age}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function StateChip({ state }: { state: string }) {
  const tone: Tone =
    state === "AT_GATE_QUEUE" ? "warn" : state === "MOVING" || state === "ENROUTE" ? "ok" : "info";
  const colour = TONE_COLOUR[tone];
  return (
    <span
      className="inline-flex items-center rounded-full px-2 py-0.5 text-[10.5px] font-semibold"
      style={{ backgroundColor: `${colour}1f`, color: colour }}
    >
      {humanize(state)}
    </span>
  );
}

// --- Alerts list -------------------------------------------------------------

function AlertsList({
  alerts,
  loading,
  error,
  onRetry,
}: {
  alerts: Alert[];
  loading: boolean;
  error: boolean;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  if (loading) return <LoadingState />;
  if (error) return <ErrorState onRetry={onRetry} />;
  if (alerts.length === 0) return <EmptyState>{t("notifications.empty")}</EmptyState>;
  return (
    <ul className="max-h-[320px] divide-y divide-border overflow-auto">
      {alerts.map((a, i) => {
        const sev = severityColour(a.severity);
        const loc = a.gate_id ?? (a.payload?.zone_id as string) ?? "—";
        return (
          <li key={`${a.id}-${i}`} className="flex items-start gap-2.5 px-3 py-2 hover:bg-muted/40">
            <span
              className="mt-1.5 h-2 w-2 shrink-0 rounded-full"
              style={{ backgroundColor: sev }}
              aria-hidden
            />
            <div className="min-w-0 flex-1">
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-[13px] font-semibold text-foreground">
                  {t(`alertKind.${a.kind}`, { defaultValue: humanize(a.kind) })}
                </span>
                <span className="shrink-0 text-[11px] text-muted-foreground">
                  {relativeAge(a.ts)}
                </span>
              </div>
              <div className="truncate text-[11px] text-muted-foreground">
                {a.plate ? `${a.plate} · ` : ""}
                {loc}
              </div>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

// --- Summary cards -----------------------------------------------------------

function SummaryShell({
  title,
  to,
  children,
}: {
  title: string;
  to: string;
  children: React.ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <Card className="flex flex-col p-3">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-sm font-semibold tracking-tight text-foreground">{title}</h2>
        <Link to={to} className="text-[11px] font-semibold text-primary hover:underline">
          {t("commandCenter.viewAll")}
        </Link>
      </div>
      {children}
    </Card>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: Tone }) {
  return (
    <div className="rounded-md bg-muted/50 px-2.5 py-2">
      <div
        className="text-lg font-bold tabular-nums leading-none"
        style={tone ? { color: TONE_COLOUR[tone] } : undefined}
      >
        {value}
      </div>
      <div className="mt-1 text-[10.5px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
    </div>
  );
}

function GateSummary({
  gates,
  queued,
  loading,
}: {
  gates: Gate[];
  queued: number;
  loading: boolean;
}) {
  const { t } = useTranslation();
  const throughput = gates.reduce((s, g) => s + (g.throughput_60min ?? 0), 0);
  const avgUtil = gates.length
    ? Math.round((gates.reduce((s, g) => s + (g.utilisation ?? 0), 0) / gates.length) * 100)
    : 0;
  return (
    <SummaryShell title={t("commandCenter.gateSummary")} to="/gate-customs">
      {loading ? (
        <LoadingState />
      ) : (
        <div className="grid grid-cols-3 gap-2">
          <Stat label={t("commandCenter.gateThroughput")} value={fmt(throughput)} tone="info" />
          <Stat
            label={t("commandCenter.gateQueueShort")}
            value={fmt(queued)}
            tone={queued > 40 ? "warn" : "ok"}
          />
          <Stat
            label={t("commandCenter.gateUtil")}
            value={`${avgUtil}%`}
            tone={avgUtil >= 100 ? "critical" : "info"}
          />
        </div>
      )}
    </SummaryShell>
  );
}

function ParkingSummaryCard({
  capacity,
  occupied,
  available,
  full,
  loading,
}: {
  capacity?: number;
  occupied?: number;
  available?: number;
  full?: number;
  loading: boolean;
}) {
  const { t } = useTranslation();
  const pct = capacity && occupied != null ? Math.round((occupied / capacity) * 100) : 0;
  return (
    <SummaryShell title={t("commandCenter.parkingSummary")} to="/parking">
      {loading ? (
        <LoadingState />
      ) : (
        <>
          <div className="grid grid-cols-3 gap-2">
            <Stat label={t("panels.parking.capacity")} value={fmt(capacity)} tone="info" />
            <Stat label={t("panels.parking.available")} value={fmt(available)} tone="ok" />
            <Stat
              label={t("commandCenter.parkingFull")}
              value={fmt(full)}
              tone={(full ?? 0) > 0 ? "warn" : "ok"}
            />
          </div>
          <div className="mt-2">
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
              <div
                className="h-full rounded-full"
                style={{
                  width: `${Math.min(pct, 100)}%`,
                  backgroundColor:
                    pct >= 90 ? STATUS.critical : pct >= 70 ? STATUS.warning : STATUS.ok,
                }}
              />
            </div>
            <div className="mt-1 text-[10.5px] text-muted-foreground">
              {pct}% {t("commandCenter.occupied")}
            </div>
          </div>
        </>
      )}
    </SummaryShell>
  );
}

function CustomsSummary({
  leo,
  flags,
  loading,
}: {
  leo: { leo_ready: boolean }[];
  flags: number;
  loading: boolean;
}) {
  const { t } = useTranslation();
  const ready = leo.filter((x) => x.leo_ready).length;
  const held = leo.length - ready;
  return (
    <SummaryShell title={t("commandCenter.customsSummary")} to="/gate-customs">
      {loading ? (
        <LoadingState />
      ) : (
        <div className="grid grid-cols-3 gap-2">
          <Stat label={t("commandCenter.leoReady")} value={fmt(ready)} tone="ok" />
          <Stat
            label={t("commandCenter.leoHeld")}
            value={fmt(held)}
            tone={held > 0 ? "warn" : "ok"}
          />
          <Stat
            label={t("commandCenter.customsFlagsShort")}
            value={fmt(flags)}
            tone={flags > 0 ? "critical" : "ok"}
          />
        </div>
      )}
    </SummaryShell>
  );
}

// --- helpers -----------------------------------------------------------------

function fmt(n?: number | null): string {
  if (n == null) return "—";
  return n.toLocaleString("en-IN");
}

function fmtTonnes(kg: number): string {
  if (kg >= 1000) return `${(kg / 1000).toFixed(1)} t`;
  return `${Math.round(kg)} kg`;
}

function humanize(s: string): string {
  return s
    .toLowerCase()
    .split(/[_\s]+/)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}
