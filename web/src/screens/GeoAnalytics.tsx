// Geo Analytics — the merged geo-fencing experience (FINAL PHASE redesign).
// One professional screen combining the former Geo-fencing Manager (zone editor)
// and Geo-fence Events dashboard, with a live GIS map and six tabs:
//   Live Zones · Vehicles in Zone · Entry/Exit Timeline · Violations · AI Events · Heatmap
//
// Every row is RDS-backed via the DB-driven geo-fence engine (/api/geo/*,
// /api/ai/events) and /api/zones — query keys are UNCHANGED from the two source
// screens, so no backend/API changes. The zone editor is reused verbatim
// (GeofencingManager) to preserve all terra-draw editing + PUT-to-Postgres.

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Shapes, MapPinned, LogIn, TriangleAlert, Cpu, Flame, LogOut } from "lucide-react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "@/lib/api";
import { getAdapter } from "@/data";
import { Card } from "@/components/ui/card";
import { ArcgisMap } from "@/components/map/ArcgisMap";
import { useMapSettings } from "@/lib/mapSettings";
import GeofencingManager from "@/screens/GeofencingManager";
import {
  PageContainer,
  PageHeader,
  StatGrid,
  StatCard,
  SegmentedTabs,
  DataTable,
  StatusChip,
  type Column,
} from "@/components/ui/dtccc";
import { EmptyState, LoadingState, ErrorState } from "@/components/ui/misc";
import { STATUS } from "@/lib/tokens";
import { fmtDateTimeIST, relativeAge } from "@/lib/utils";
import type { AiEvent, GeofenceEvent, GeoVehicleInZone } from "@/lib/types";

type TabKey = "zones" | "inside" | "events" | "violations" | "ai" | "heatmap";

export default function GeoAnalytics({ defaultTab = "zones" }: { defaultTab?: TabKey }) {
  const [tab, setTab] = useState<TabKey>(defaultTab);

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
          <EventsTimeline
            status={eventsQ}
            rows={eventsQ.data?.events ?? []}
            onRetry={() => eventsQ.refetch()}
          />
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
        {tab === "heatmap" && <HeatmapTab violations={violQ.data?.violations ?? []} />}
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
  const [q, setQ] = useState("");
  const [limit, setLimit] = useState(20);
  const filtered = useMemo(() => {
    const list = rows.filter((e) => e.event_type === "ENTER" || e.event_type === "EXIT");
    const query = q.trim().toLowerCase();
    return (
      query
        ? list.filter((e) =>
            `${e.vehicle_id ?? ""} ${e.zone_id ?? ""}`.toLowerCase().includes(query),
          )
        : list
    ).sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
  }, [rows, q]);

  if (status.isLoading)
    return (
      <Card className="p-0">
        <LoadingState />
      </Card>
    );
  if (status.isError)
    return (
      <Card className="p-0">
        <ErrorState onRetry={onRetry} />
      </Card>
    );

  return (
    <Card className="overflow-hidden">
      <div className="border-b border-border p-3">
        <input
          type="search"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setLimit(20);
          }}
          placeholder="Search vehicle / zone…"
          className="h-9 w-full max-w-xs rounded-md border border-border bg-background px-3 text-[13px] outline-none focus:border-primary focus:ring-2 focus:ring-primary/20"
        />
      </div>
      {filtered.length === 0 ? (
        <EmptyState>No entry/exit events yet.</EmptyState>
      ) : (
        <>
          <ol className="relative space-y-0 p-4 pl-6">
            <span className="absolute left-[13px] top-4 bottom-4 w-px bg-border" aria-hidden />
            {filtered.slice(0, limit).map((e) => {
              const enter = e.event_type === "ENTER";
              return (
                <li key={e.id} className="relative flex gap-3 pb-4 last:pb-0">
                  <span
                    className="absolute -left-[11px] mt-1 flex h-4 w-4 items-center justify-center rounded-full ring-4 ring-card"
                    style={{ backgroundColor: enter ? STATUS.warning : STATUS.ok }}
                  >
                    {enter ? (
                      <LogIn className="h-2.5 w-2.5 text-white" />
                    ) : (
                      <LogOut className="h-2.5 w-2.5 text-white" />
                    )}
                  </span>
                  <div className="ml-4 flex flex-1 flex-wrap items-center gap-x-2 gap-y-0.5">
                    <StatusChip label={e.event_type} tone={enter ? "warn" : "ok"} />
                    <span className="font-mono text-[13px] font-medium text-foreground">
                      {e.vehicle_id}
                    </span>
                    <span className="text-[13px] text-muted-foreground">→ {e.zone_id}</span>
                    {e.dwell_seconds != null && (
                      <span className="text-[11px] text-muted-foreground">
                        · dwell {Math.round(e.dwell_seconds / 60)}m
                      </span>
                    )}
                    <span
                      className="ml-auto text-[11px] text-muted-foreground"
                      title={fmtDateTimeIST(e.created_at)}
                    >
                      {relativeAge(e.created_at)}
                    </span>
                  </div>
                </li>
              );
            })}
          </ol>
          {limit < filtered.length && (
            <div className="border-t border-border px-4 py-2 text-center">
              <button
                onClick={() => setLimit((l) => l + 20)}
                className="text-[12px] font-semibold text-primary hover:underline"
              >
                Load more ({filtered.length - limit} remaining)
              </button>
            </div>
          )}
        </>
      )}
    </Card>
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

function HeatmapTab({ violations }: { violations: GeofenceEvent[] }) {
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
