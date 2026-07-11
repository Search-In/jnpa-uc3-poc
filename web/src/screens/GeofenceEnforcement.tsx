// Geo-fence Enforcement dashboard — active zones, vehicles currently inside
// zones, entry/exit events, violations (no-parking / restricted / dwell) and AI
// incidents. Every row is RDS-backed via the DB-driven geo-fence engine
// (/api/geo/*, /api/ai/events) — detection reads jnpa.geofence_zones live.
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ShieldAlert, MapPinned, LogIn, TriangleAlert, Cpu } from "lucide-react";
import { api } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { AsyncBoundary, EmptyState, LastUpdated } from "@/components/ui/misc";
import { cn, fmtDateTimeIST } from "@/lib/utils";

const OK = "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400";
const WARN = "bg-amber-500/15 text-amber-600 dark:text-amber-400";
const CRIT = "bg-red-500/15 text-red-600 dark:text-red-400";

type TabKey = "zones" | "inside" | "events" | "violations" | "ai";
const TABS: { key: TabKey; label: string }[] = [
  { key: "zones", label: "Active Zones" },
  { key: "inside", label: "Vehicles In Zone" },
  { key: "events", label: "Entry/Exit" },
  { key: "violations", label: "Violations" },
  { key: "ai", label: "AI Incidents" },
];

function evBadge(t: string | null) {
  if (t === "EXIT") return OK;
  if (t === "ENTER") return WARN;
  return CRIT;
}

function ZonesTab() {
  const q = useQuery({ queryKey: ["geo-zones-active"], queryFn: () => api.geoZonesActive() });
  const rows = q.data?.zones ?? [];
  return (
    <>
      <div className="mb-2 flex justify-end">
        <LastUpdated at={q.dataUpdatedAt} isFetching={q.isFetching && !q.isLoading} />
      </div>
      <AsyncBoundary
        status={q}
        isEmpty={!rows.length}
        onRetry={() => q.refetch()}
        empty={<EmptyState>No active zones.</EmptyState>}
      >
        <div className="mb-2 text-[11px] text-muted-foreground">
          Detection source: <span className="font-mono">{q.data?.source}</span> · {rows.length}{" "}
          enforced
        </div>
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-xs">
            <thead className="bg-muted/50 text-left text-muted-foreground">
              <tr>
                <th className="px-3 py-2">Zone</th>
                <th className="px-3 py-2">ID</th>
                <th className="px-3 py-2">Kind</th>
                <th className="px-3 py-2 text-right">Vertices</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((z) => (
                <tr key={z.id} className="border-t border-border/60">
                  <td className="px-3 py-1.5">{z.name}</td>
                  <td className="px-3 py-1.5 font-mono">{z.id}</td>
                  <td className="px-3 py-1.5">
                    <Badge className={cn("text-[10px]", z.kind === "restricted" ? CRIT : WARN)}>
                      {z.kind}
                    </Badge>
                  </td>
                  <td className="px-3 py-1.5 text-right tabular-nums">{z.points}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </AsyncBoundary>
    </>
  );
}

function InsideTab() {
  const q = useQuery({
    queryKey: ["geo-inside"],
    queryFn: () => api.geoVehiclesInZones(),
  });
  const rows = q.data?.vehicles ?? [];
  return (
    <>
      <div className="mb-2 flex justify-end">
        <LastUpdated at={q.dataUpdatedAt} isFetching={q.isFetching && !q.isLoading} />
      </div>
      <AsyncBoundary
        status={q}
        isEmpty={!rows.length}
        onRetry={() => q.refetch()}
        empty={<EmptyState>No vehicles currently inside any zone.</EmptyState>}
      >
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-xs">
            <thead className="bg-muted/50 text-left text-muted-foreground">
              <tr>
                <th className="px-3 py-2">Vehicle</th>
                <th className="px-3 py-2">Zone</th>
                <th className="px-3 py-2 text-right">Dwell</th>
                <th className="px-3 py-2">State</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((v, i) => (
                <tr key={`${v.vehicle_id}-${v.zone_id}-${i}`} className="border-t border-border/60">
                  <td className="px-3 py-1.5 font-mono">{v.vehicle_id}</td>
                  <td className="px-3 py-1.5">{v.zone_id}</td>
                  <td className="px-3 py-1.5 text-right tabular-nums">
                    {Math.round(v.dwell_s / 60)}m
                  </td>
                  <td className="px-3 py-1.5">
                    <Badge className={cn("text-[10px]", v.violated ? CRIT : OK)}>
                      {v.violated ? "VIOLATION" : "OK"}
                    </Badge>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </AsyncBoundary>
    </>
  );
}

function EventsTab() {
  const q = useQuery({ queryKey: ["geo-events"], queryFn: () => api.geoEvents(undefined, 200) });
  const rows = (q.data?.events ?? []).filter(
    (e) => e.event_type === "ENTER" || e.event_type === "EXIT",
  );
  return (
    <>
      <div className="mb-2 flex justify-end">
        <LastUpdated at={q.dataUpdatedAt} isFetching={q.isFetching && !q.isLoading} />
      </div>
      <AsyncBoundary
        status={q}
        isEmpty={!rows.length}
        onRetry={() => q.refetch()}
        empty={<EmptyState>No entry/exit events yet.</EmptyState>}
      >
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-xs">
            <thead className="bg-muted/50 text-left text-muted-foreground">
              <tr>
                <th className="px-3 py-2">Type</th>
                <th className="px-3 py-2">Vehicle</th>
                <th className="px-3 py-2">Zone</th>
                <th className="px-3 py-2 text-right">Dwell</th>
                <th className="px-3 py-2">When</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((e) => (
                <tr key={e.id} className="border-t border-border/60">
                  <td className="px-3 py-1.5">
                    <Badge className={cn("text-[10px]", evBadge(e.event_type))}>
                      {e.event_type}
                    </Badge>
                  </td>
                  <td className="px-3 py-1.5 font-mono">{e.vehicle_id}</td>
                  <td className="px-3 py-1.5">{e.zone_id}</td>
                  <td className="px-3 py-1.5 text-right tabular-nums">
                    {e.dwell_seconds != null ? `${Math.round(e.dwell_seconds / 60)}m` : "—"}
                  </td>
                  <td className="px-3 py-1.5 text-muted-foreground">
                    {fmtDateTimeIST(e.created_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </AsyncBoundary>
    </>
  );
}

function ViolationsTab() {
  const q = useQuery({ queryKey: ["geo-violations"], queryFn: () => api.geoViolations(200) });
  const rows = q.data?.violations ?? [];
  return (
    <>
      <div className="mb-2 flex justify-end">
        <LastUpdated at={q.dataUpdatedAt} isFetching={q.isFetching && !q.isLoading} />
      </div>
      <AsyncBoundary
        status={q}
        isEmpty={!rows.length}
        onRetry={() => q.refetch()}
        empty={<EmptyState>No geo-fence violations in RDS.</EmptyState>}
      >
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-xs">
            <thead className="bg-muted/50 text-left text-muted-foreground">
              <tr>
                <th className="px-3 py-2">Violation</th>
                <th className="px-3 py-2">Vehicle</th>
                <th className="px-3 py-2">Driver</th>
                <th className="px-3 py-2">Zone</th>
                <th className="px-3 py-2 text-right">Dwell</th>
                <th className="px-3 py-2">When</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((e) => (
                <tr key={e.id} className="border-t border-border/60">
                  <td className="px-3 py-1.5">
                    <Badge className={cn("text-[10px]", CRIT)}>{e.violation_type}</Badge>
                  </td>
                  <td className="px-3 py-1.5 font-mono">{e.vehicle_id}</td>
                  <td className="px-3 py-1.5">{e.driver_id ?? "—"}</td>
                  <td className="px-3 py-1.5">{e.zone_id}</td>
                  <td className="px-3 py-1.5 text-right tabular-nums">
                    {e.dwell_seconds != null ? `${Math.round(e.dwell_seconds / 60)}m` : "—"}
                  </td>
                  <td className="px-3 py-1.5 text-muted-foreground">
                    {fmtDateTimeIST(e.created_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </AsyncBoundary>
    </>
  );
}

function AiTab() {
  const q = useQuery({ queryKey: ["ai-events"], queryFn: () => api.aiEvents(undefined, 200) });
  const rows = q.data?.events ?? [];
  return (
    <>
      <div className="mb-2 flex justify-end">
        <LastUpdated at={q.dataUpdatedAt} isFetching={q.isFetching && !q.isLoading} />
      </div>
      <AsyncBoundary
        status={q}
        isEmpty={!rows.length}
        onRetry={() => q.refetch()}
        empty={<EmptyState>No AI incidents in RDS.</EmptyState>}
      >
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-xs">
            <thead className="bg-muted/50 text-left text-muted-foreground">
              <tr>
                <th className="px-3 py-2">AI Event</th>
                <th className="px-3 py-2">Vehicle</th>
                <th className="px-3 py-2">Location</th>
                <th className="px-3 py-2">When</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((e) => (
                <tr key={e.id} className="border-t border-border/60">
                  <td className="px-3 py-1.5">
                    <Badge className={cn("text-[10px]", WARN)}>{e.event_type}</Badge>
                  </td>
                  <td className="px-3 py-1.5 font-mono">{e.vehicle_id ?? "—"}</td>
                  <td className="px-3 py-1.5 text-muted-foreground">
                    {JSON.stringify(e.location)}
                  </td>
                  <td className="px-3 py-1.5 text-muted-foreground">
                    {fmtDateTimeIST(e.created_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </AsyncBoundary>
    </>
  );
}

export default function GeofenceEnforcement() {
  const [tab, setTab] = useState<TabKey>("zones");
  return (
    <div className="h-full overflow-y-auto p-4">
      <div className="mb-3 flex items-center gap-2">
        <ShieldAlert className="h-5 w-5 text-primary" aria-hidden />
        <h1 className="text-lg font-semibold">Geo-fence Enforcement</h1>
        <span className="text-xs text-muted-foreground">DB-driven · reads jnpa.geofence_zones</span>
      </div>
      <div className="mb-4 inline-flex flex-wrap rounded-md border border-border p-0.5">
        {TABS.map((tb) => (
          <button
            key={tb.key}
            onClick={() => setTab(tb.key)}
            className={cn(
              "rounded-md px-3 py-1.5 text-sm transition-colors",
              tab === tb.key ? "bg-primary text-primary-foreground" : "hover:bg-muted",
            )}
          >
            {tb.label}
          </button>
        ))}
      </div>
      <Card>
        <CardHeader className="flex-row items-center gap-2">
          {tab === "zones" ? (
            <MapPinned className="h-4 w-4 text-muted-foreground" />
          ) : tab === "inside" ? (
            <MapPinned className="h-4 w-4 text-muted-foreground" />
          ) : tab === "events" ? (
            <LogIn className="h-4 w-4 text-muted-foreground" />
          ) : tab === "violations" ? (
            <TriangleAlert className="h-4 w-4 text-muted-foreground" />
          ) : (
            <Cpu className="h-4 w-4 text-muted-foreground" />
          )}
          <CardTitle className="text-sm">{TABS.find((t) => t.key === tab)?.label}</CardTitle>
        </CardHeader>
        <CardContent>
          {tab === "zones" && <ZonesTab />}
          {tab === "inside" && <InsideTab />}
          {tab === "events" && <EventsTab />}
          {tab === "violations" && <ViolationsTab />}
          {tab === "ai" && <AiTab />}
        </CardContent>
      </Card>
    </div>
  );
}
