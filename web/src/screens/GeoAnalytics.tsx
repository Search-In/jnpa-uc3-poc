// Geo Analytics — the merged geo-fencing experience (FINAL PHASE redesign).
// One professional screen combining the former Geo-fencing Manager (zone editor)
// and Geo-fence Events dashboard, with a live GIS map and six tabs:
//   Live Zones · Vehicles in Zone · Entry/Exit Timeline · Violations · AI Events · Heatmap
//
// Every row is RDS-backed via the DB-driven geo-fence engine (/api/geo/*,
// /api/ai/events) and /api/zones — query keys are UNCHANGED from the two source
// screens, so no backend/API changes. The zone editor is reused verbatim
// (GeofencingManager) to preserve all terra-draw editing + PUT-to-Postgres.

import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Shapes, MapPinned, LogIn, TriangleAlert, Cpu, Flame, Route, Gauge } from "lucide-react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "@/lib/api";
import CorridorHeatmapPanel from "@/components/panels/CorridorHeatmapPanel";
import { getAdapter } from "@/data";
import { Card } from "@/components/ui/card";
import { ArcgisMap } from "@/components/map/ArcgisMap";
import { useMapSettings } from "@/lib/mapSettings";
import { resolveIncidents } from "@/lib/incidents";
import GeofencingManager from "@/screens/GeofencingManager";
import RoadBottlenecks from "@/screens/RoadBottlenecks";
import {
  PageContainer,
  PageHeader,
  StatGrid,
  StatCard,
  SegmentedTabs,
  DataTable,
  StatusChip,
  Embedded,
  type Column,
} from "@/components/ui/dtccc";
import { STATUS } from "@/lib/tokens";
import { fmtDateTimeIST } from "@/lib/utils";
import type { AiEvent, GeofenceEvent, GeoVehicleInZone } from "@/lib/types";

type TabKey =
  | "zones"
  | "inside"
  | "events"
  | "violations"
  | "ai"
  | "heatmap"
  | "corridor"
  | "bottlenecks";

const TAB_KEYS: TabKey[] = [
  "zones",
  "inside",
  "events",
  "violations",
  "ai",
  "heatmap",
  "bottlenecks",
];

export default function GeoAnalytics({ defaultTab = "zones" }: { defaultTab?: TabKey }) {
  // `?tab=` wins over the route's defaultTab prop: Command Center and Demo
  // Console deep-link here (e.g. ?tab=bottlenecks) and previously the param was
  // ignored, dropping the operator on Live Zones instead.
  const [params, setParams] = useSearchParams();
  const urlTab = params.get("tab") as TabKey | null;
  const [tab, setTabState] = useState<TabKey>(
    urlTab && TAB_KEYS.includes(urlTab) ? urlTab : defaultTab,
  );
  useEffect(() => {
    if (urlTab && TAB_KEYS.includes(urlTab) && urlTab !== tab) setTabState(urlTab);
  }, [urlTab]); // eslint-disable-line react-hooks/exhaustive-deps
  const setTab = (k: TabKey) => {
    setTabState(k);
    const next = new URLSearchParams(params);
    next.set("tab", k);
    setParams(next, { replace: true });
  };

  // Page-level queries for the summary cards (shared keys with the tab bodies).
  const zonesQ = useQuery({ queryKey: ["geo-zones-active"], queryFn: () => api.geoZonesActive() });
  const insideQ = useQuery({
    queryKey: ["geo-inside"],
    queryFn: () => api.geoVehiclesInZones(),
  });
  const violQ = useQuery({ queryKey: ["geo-violations"], queryFn: () => api.geoViolations(200) });
  const aiQ = useQuery({ queryKey: ["ai-events"], queryFn: () => api.aiEvents(undefined, 200) });
  const eventsQ = useQuery({
    queryKey: ["geo-events"],
    queryFn: () => api.geoEvents(undefined, 200),
  });

  const updatedAt = Math.max(
    zonesQ.dataUpdatedAt || 0,
    insideQ.dataUpdatedAt || 0,
    violQ.dataUpdatedAt || 0,
  );
  const anyFetching = zonesQ.isFetching || insideQ.isFetching || violQ.isFetching || aiQ.isFetching;

  function refreshAll() {
    [zonesQ, insideQ, violQ, aiQ, eventsQ].forEach((q) => void q.refetch());
  }

  return (
    <PageContainer>
      <PageHeader
        icon={Shapes}
        title="Geo Analytics"
        subtitle="Zones · vehicles · entry/exit · violations · AI — DB-driven geo-fence engine"
        updatedAt={updatedAt}
        isFetching={anyFetching}
        onRefresh={refreshAll}
      />

      <div className="px-4 pt-3">
        <StatGrid className="lg:grid-cols-5">
          <StatCard
            icon={MapPinned}
            label="Active Zones"
            value={zonesQ.data?.zones?.length ?? "—"}
            tone="info"
            loading={zonesQ.isLoading}
          />
          <StatCard
            icon={MapPinned}
            label="Vehicles in Zone"
            value={insideQ.data?.vehicles?.length ?? "—"}
            tone="warn"
            loading={insideQ.isLoading}
          />
          <StatCard
            icon={LogIn}
            label="Entry/Exit Events"
            value={
              (eventsQ.data?.events ?? []).filter(
                (e) => e.event_type === "ENTER" || e.event_type === "EXIT",
              ).length
            }
            tone="info"
            loading={eventsQ.isLoading}
          />
          <StatCard
            icon={TriangleAlert}
            label="Violations"
            value={violQ.data?.violations?.length ?? "—"}
            tone={(violQ.data?.violations?.length ?? 0) > 0 ? "critical" : "ok"}
            loading={violQ.isLoading}
          />
          <StatCard
            icon={Cpu}
            label="AI Events"
            value={aiQ.data?.count ?? "—"}
            tone={(aiQ.data?.count ?? 0) > 0 ? "warn" : "ok"}
            loading={aiQ.isLoading}
          />
        </StatGrid>
      </div>

      <div className="px-4 py-3">
        <SegmentedTabs
          value={tab}
          onChange={setTab}
          className="mb-3"
          tabs={[
            { key: "zones", label: "Live Zones", icon: Shapes, count: zonesQ.data?.zones?.length },
            {
              key: "inside",
              label: "Vehicles in Zone",
              icon: MapPinned,
              count: insideQ.data?.vehicles?.length,
            },
            { key: "events", label: "Entry / Exit Timeline", icon: LogIn },
            {
              key: "violations",
              label: "Violations",
              icon: TriangleAlert,
              count: violQ.data?.violations?.length,
            },
            { key: "ai", label: "AI Events", icon: Cpu, count: aiQ.data?.events?.length },
            { key: "heatmap", label: "Heatmap", icon: Flame },
            // UC3-020 T-01: corridor congestion sits beside the violation
            // heatmap on the screen that already owns the map and its layers.
            { key: "corridor", label: "Corridor Congestion", icon: Route },
            { key: "bottlenecks", label: "Bottlenecks", icon: Gauge },
          ]}
        />

        {tab === "zones" && (
          <Card className="h-[600px] overflow-hidden">
            {/* Reused verbatim — all terra-draw editing + PUT /api/zones preserved. */}
            <GeofencingManager />
          </Card>
        )}
        {tab === "inside" && (
          <Card className="overflow-hidden">
            <InsideTable
              rows={insideQ.data?.vehicles ?? []}
              status={insideQ}
              onRetry={() => insideQ.refetch()}
            />
          </Card>
        )}
        {tab === "events" && (
          <Card className="overflow-hidden">
            <EventsTimeline
              status={eventsQ}
              rows={eventsQ.data?.events ?? []}
              onRetry={() => eventsQ.refetch()}
            />
          </Card>
        )}
        {tab === "violations" && (
          <Card className="overflow-hidden">
            <ViolationsTable
              rows={violQ.data?.violations ?? []}
              status={violQ}
              onRetry={() => violQ.refetch()}
            />
          </Card>
        )}
        {tab === "ai" && (
          <Card className="overflow-hidden">
            <AiTable rows={aiQ.data?.events ?? []} status={aiQ} onRetry={() => aiQ.refetch()} />
          </Card>
        )}
        {tab === "corridor" && <CorridorHeatmapPanel />}
        {tab === "heatmap" && (
          <HeatmapTab
            violations={violQ.data?.violations ?? []}
            aiEvents={aiQ.data?.events ?? []}
            events={eventsQ.data?.events ?? []}
          />
        )}
        {tab === "bottlenecks" && (
          <Embedded>
            <RoadBottlenecks />
          </Embedded>
        )}
      </div>
    </PageContainer>
  );
}

// --- Vehicles in Zone --------------------------------------------------------

function InsideTable({
  rows,
  status,
  onRetry,
}: {
  rows: GeoVehicleInZone[];
  status: any;
  onRetry: () => void;
}) {
  // Per-occupancy trigger state, keyed by vehicle+zone+entry_time so a genuine
  // re-entry (new entry_time) is triggerable again — mirroring the server's
  // dedup key exactly, so the button never promises what the API would refuse.
  const occKey = (v: GeoVehicleInZone) => `${v.vehicle_id}-${v.zone_id}-${v.entry_time}`;
  const [busy, setBusy] = useState<string | null>(null);
  const [done, setDone] = useState<Record<string, boolean>>({});
  const [failed, setFailed] = useState<Record<string, string>>({});

  async function onTrigger(v: GeoVehicleInZone) {
    const key = occKey(v);
    setBusy(key);
    setFailed((f) => ({ ...f, [key]: "" }));
    try {
      // `created: false` = already triggered for this occupancy; still a success
      // from the operator's point of view, so it settles into the same state.
      await api.geoNotifyZone(v.vehicle_id, v.zone_id, v.entry_time);
      setDone((d) => ({ ...d, [key]: true }));
    } catch (e: any) {
      // 409 = the vehicle left the zone, or re-entered since this row was drawn.
      const msg = String(e?.message ?? "");
      setFailed((f) => ({
        ...f,
        [key]: msg.includes("409")
          ? "Vehicle is no longer in this occupancy — refresh"
          : "Trigger failed",
      }));
    } finally {
      setBusy(null);
    }
  }

  const columns: Column<GeoVehicleInZone>[] = [
    { key: "vehicle", header: "Vehicle", className: "font-mono", render: (v) => v.vehicle_id },
    { key: "zone", header: "Zone", render: (v) => v.zone_id },
    {
      key: "entry",
      header: "Entered",
      className: "text-muted-foreground",
      render: (v) => (v.entry_time ? fmtDateTimeIST(v.entry_time) : "—"),
    },
    {
      key: "dwell",
      header: "Dwell",
      align: "right",
      className: "tabular-nums",
      render: (v) => `${Math.round(v.dwell_s / 60)}m`,
    },
    {
      key: "state",
      header: "State",
      render: (v) => (
        <StatusChip label={v.violated ? "VIOLATION" : "OK"} tone={v.violated ? "critical" : "ok"} />
      ),
    },
    {
      key: "trigger",
      header: "Action",
      align: "right",
      render: (v) => {
        const key = occKey(v);
        const isBusy = busy === key;
        const isDone = !!done[key];
        const err = failed[key];
        if (isDone) return <StatusChip label="Notified" tone="ok" />;
        return (
          <div className="flex items-center justify-end gap-2">
            {err && <span className="text-[11px] text-severity-critical">{err}</span>}
            <button
              type="button"
              onClick={() => void onTrigger(v)}
              disabled={isBusy}
              className="rounded border border-border px-2 py-0.5 text-[11px] font-medium text-foreground transition hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
            >
              {isBusy ? "Triggering…" : err ? "Retry" : "Trigger"}
            </button>
          </div>
        );
      },
    },
  ];
  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(v) => `${v.vehicle_id}-${v.zone_id}`}
      status={status}
      onRetry={onRetry}
      emptyLabel="No vehicles currently inside any zone."
      search={(v, q) => `${v.vehicle_id} ${v.zone_id}`.toLowerCase().includes(q)}
      searchPlaceholder="Search vehicle / zone…"
      pageSize={12}
    />
  );
}

// --- Entry / Exit Timeline ---------------------------------------------------

function EventsTimeline({
  rows,
  status,
  onRetry,
}: {
  rows: GeofenceEvent[];
  status: any;
  onRetry: () => void;
}) {
  // Same rows as before (ENTER/EXIT only, newest first) — only the presentation
  // changes: a scannable table instead of a vertical timeline. An EXIT row
  // already carries entry_time, exit_time and dwell_seconds from the engine, so
  // every column below reads a field the API already returns.
  const filtered = useMemo(
    () =>
      rows
        .filter((e) => e.event_type === "ENTER" || e.event_type === "EXIT")
        .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at)),
    [rows],
  );

  const columns: Column<GeofenceEvent>[] = [
    {
      key: "vehicle",
      header: "Vehicle",
      className: "font-mono",
      render: (e) => e.vehicle_id ?? "—",
    },
    { key: "zone", header: "Zone", render: (e) => e.zone_id ?? "—" },
    {
      key: "entry",
      header: "Entry",
      className: "text-muted-foreground",
      render: (e) => fmtDateTimeIST(e.entry_time ?? e.created_at),
    },
    {
      key: "exit",
      header: "Exit",
      className: "text-muted-foreground",
      render: (e) => (e.exit_time ? fmtDateTimeIST(e.exit_time) : "—"),
    },
    {
      key: "duration",
      header: "Duration",
      align: "right",
      className: "tabular-nums",
      render: (e) => (e.dwell_seconds != null ? `${Math.round(e.dwell_seconds / 60)}m` : "—"),
    },
    {
      key: "status",
      header: "Status",
      // An EXIT row has closed the visit; an ENTER row is still open.
      render: (e) =>
        e.exit_time || e.event_type === "EXIT" ? (
          <StatusChip label="COMPLETED" tone="ok" />
        ) : (
          <StatusChip label="ACTIVE" tone="warn" />
        ),
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={filtered}
      rowKey={(e) => String(e.id)}
      status={status}
      onRetry={onRetry}
      emptyLabel="No entry/exit events yet."
      search={(e, q) => `${e.vehicle_id ?? ""} ${e.zone_id ?? ""}`.toLowerCase().includes(q)}
      searchPlaceholder="Search vehicle / zone…"
      pageSize={12}
    />
  );
}

// --- Violations --------------------------------------------------------------

function ViolationsTable({
  rows,
  status,
  onRetry,
}: {
  rows: GeofenceEvent[];
  status: any;
  onRetry: () => void;
}) {
  const columns: Column<GeofenceEvent>[] = [
    {
      key: "type",
      header: "Violation",
      render: (e) => <StatusChip label={e.violation_type ?? "—"} tone="critical" />,
    },
    {
      key: "vehicle",
      header: "Vehicle",
      className: "font-mono",
      render: (e) => e.vehicle_id ?? "—",
    },
    { key: "driver", header: "Driver", render: (e) => e.driver_id ?? "—" },
    { key: "zone", header: "Zone", render: (e) => e.zone_id ?? "—" },
    {
      key: "dwell",
      header: "Dwell",
      align: "right",
      className: "tabular-nums",
      render: (e) => (e.dwell_seconds != null ? `${Math.round(e.dwell_seconds / 60)}m` : "—"),
    },
    {
      key: "when",
      header: "When",
      className: "text-muted-foreground",
      render: (e) => fmtDateTimeIST(e.created_at),
    },
  ];
  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(e) => String(e.id)}
      status={status}
      onRetry={onRetry}
      emptyLabel="No geo-fence violations in RDS."
      search={(e, q) =>
        `${e.violation_type ?? ""} ${e.vehicle_id ?? ""} ${e.zone_id ?? ""}`
          .toLowerCase()
          .includes(q)
      }
      searchPlaceholder="Search violations…"
      pageSize={12}
    />
  );
}

// --- AI Events ---------------------------------------------------------------

function AiTable({ rows, status, onRetry }: { rows: AiEvent[]; status: any; onRetry: () => void }) {
  const columns: Column<AiEvent>[] = [
    {
      key: "type",
      header: "AI Event",
      render: (e) => <StatusChip label={e.event_type} tone="warn" />,
    },
    {
      key: "vehicle",
      header: "Vehicle",
      className: "font-mono",
      render: (e) => e.vehicle_id ?? "—",
    },
    { key: "driver", header: "Driver", render: (e) => e.driver_id ?? "—" },
    {
      key: "location",
      header: "Location",
      className: "text-muted-foreground",
      render: (e) => summariseLocation(e.location),
    },
    {
      key: "when",
      header: "When",
      className: "text-muted-foreground",
      render: (e) => fmtDateTimeIST(e.created_at),
    },
  ];
  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(e) => String(e.id)}
      status={status}
      onRetry={onRetry}
      emptyLabel="No AI events in RDS."
      search={(e, q) => `${e.event_type} ${e.vehicle_id ?? ""}`.toLowerCase().includes(q)}
      searchPlaceholder="Search AI events…"
      pageSize={12}
    />
  );
}

function summariseLocation(loc: Record<string, unknown>): string {
  if (!loc || typeof loc !== "object") return "—";
  const lat = (loc as any).lat ?? (loc as any).latitude;
  const lon = (loc as any).lon ?? (loc as any).lng ?? (loc as any).longitude;
  if (lat != null && lon != null) return `${Number(lat).toFixed(3)}, ${Number(lon).toFixed(3)}`;
  const z = (loc as any).zone_id ?? (loc as any).gate_id;
  return z ? String(z) : "—";
}

// --- Heatmap -----------------------------------------------------------------

function HeatmapTab({
  violations,
  aiEvents,
  events,
}: {
  violations: GeofenceEvent[];
  aiEvents: AiEvent[];
  events: GeofenceEvent[];
}) {
  const { basemap } = useMapSettings();
  const corridorQ = useQuery({
    queryKey: ["corridor"],
    queryFn: () => getAdapter().corridor(),
    staleTime: Infinity,
  });
  const snapsQ = useQuery({
    queryKey: ["snapshots"],
    queryFn: () => getAdapter().trafficSnapshots(),
  });
  const zonesQ = useQuery({ queryKey: ["zones"], queryFn: () => getAdapter().zones() });
  const trucksQ = useQuery({
    queryKey: ["trucks", "live-map"],
    queryFn: () => getAdapter().trucks(undefined, 500),
  });

  // Geolocate the RDS-backed violations / AI / entry-exit rows into weighted
  // heatmap points (zone centroid or last-known vehicle position when a row
  // carries no explicit lat/lon). This is the Esri HeatmapRenderer's data source.
  const incidents = useMemo(
    () =>
      resolveIncidents({
        violations,
        aiEvents,
        events,
        zones: zonesQ.data,
        trucks: trucksQ.data,
      }),
    [violations, aiEvents, events, zonesQ.data, trucksQ.data],
  );

  // Violations by zone (density) for the accompanying chart.
  const byZone = useMemo(() => {
    const m = new Map<string, number>();
    for (const v of violations) {
      const z = v.zone_id ?? "—";
      m.set(z, (m.get(z) ?? 0) + 1);
    }
    return Array.from(m.entries())
      .map(([name, count]) => ({ name: name.slice(0, 14), count }))
      .sort((a, b) => b.count - a.count);
  }, [violations]);

  return (
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
      <Card className="relative h-[520px] overflow-hidden p-0 lg:col-span-2">
        <ArcgisMap
          basemap={basemap}
          corridor={corridorQ.data}
          snapshots={snapsQ.data}
          zones={zonesQ.data}
          trucks={trucksQ.data}
          incidents={incidents}
        />
      </Card>
      <Card className="p-3">
        <h2 className="mb-2 text-sm font-semibold text-foreground">Violations by Zone</h2>
        {byZone.length === 0 ? (
          <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
            No violations to plot.
          </div>
        ) : (
          <div className="h-[460px]">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={byZone}
                layout="vertical"
                margin={{ top: 4, right: 12, left: 4, bottom: 4 }}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(215 20% 90%)" horizontal={false} />
                <XAxis type="number" tick={{ fontSize: 10 }} allowDecimals={false} />
                <YAxis type="category" dataKey="name" tick={{ fontSize: 10 }} width={90} />
                <Tooltip contentStyle={{ fontSize: 12, borderRadius: 8 }} />
                <Bar dataKey="count" fill={STATUS.critical} radius={[0, 3, 3, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </Card>
    </div>
  );
}
