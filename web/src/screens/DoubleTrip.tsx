// TT Double-Trip Workflow (Feature 15) — Trip-1 → Return → Trip-2 → Statistics.
//
// Terminal Tractors (TTs) shuttle laden/empty legs between yard positions. A
// "cycle" bundles the sequential legs a single vehicle runs; a cycle that racks
// up two loaded trips before returning to base is a "double-trip". This screen
// surfaces the live vs baseline statistics, a per-vehicle leaderboard, the raw
// cycle list rendered as a horizontal leg-flow, and a form to kick off a trip.
//
// Backed by /api/double-trip/* via the api client (statistics, cycles, start,
// complete). Loosely typed (any) — the gateway payload shape is authoritative.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Repeat, Play, Trophy, ArrowRight } from "lucide-react";
import { api } from "@/lib/api";
import { PageContainer, PageHeader, StatusChip } from "@/components/ui/dtccc";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState, LoadingState } from "@/components/ui/misc";
import { STATUS } from "@/lib/tokens";

const anyApi = api as any;

/** Colour a per-leg / cycle status. */
function legTone(status: string): "ok" | "warn" | "info" | "neutral" {
  const s = (status || "").toUpperCase();
  if (s === "COMPLETED" || s === "COMPLETE" || s === "DONE") return "ok";
  if (s === "IN_PROGRESS" || s === "ACTIVE" || s === "STARTED") return "info";
  if (s === "CANCELLED" || s === "ABORTED" || s === "FAILED") return "warn";
  return "neutral";
}

function isInProgress(status: string): boolean {
  const s = (status || "").toUpperCase();
  return s === "IN_PROGRESS" || s === "ACTIVE" || s === "STARTED";
}

function StatCard({
  label,
  value,
  unit,
}: {
  label: string;
  value: React.ReactNode;
  unit?: string;
}) {
  return (
    <Card>
      <CardContent className="flex flex-col gap-1 py-3">
        <span className="truncate text-[11px] font-medium text-muted-foreground" title={label}>
          {label}
        </span>
        <div className="flex items-baseline gap-1">
          <span className="text-xl font-semibold tabular-nums">{value}</span>
          {unit ? <span className="text-[11px] text-muted-foreground">{unit}</span> : null}
        </div>
      </CardContent>
    </Card>
  );
}

/** A single leg tile in the horizontal cycle flow. */
function LegTile({
  leg,
  onComplete,
  completing,
}: {
  leg: any;
  onComplete: (tripId: any) => void;
  completing: boolean;
}) {
  const seq = leg?.trip_seq ?? "—";
  const isReturn = String(leg?.direction || "")
    .toUpperCase()
    .includes("RETURN");
  const label = seq === 1 ? "Trip 1" : isReturn ? "Return" : `Trip ${seq}`;
  return (
    <div className="min-w-[180px] flex-1 rounded-md border border-border bg-muted/30 p-2">
      <div className="mb-1 flex items-center gap-2">
        <span className="text-[11px] font-semibold uppercase tracking-wide">{label}</span>
        <div className="ml-auto">
          <StatusChip label={leg?.status ?? "—"} tone={legTone(leg?.status)} />
        </div>
      </div>
      <div className="text-[11px] text-muted-foreground">
        {leg?.direction ? (
          <span className="rounded bg-muted px-1 py-0.5 font-mono text-[10px]">
            {leg.direction}
          </span>
        ) : null}
      </div>
      <div className="mt-1 flex items-center gap-1 text-[12px]">
        <span className="font-medium">{leg?.origin ?? "?"}</span>
        <ArrowRight size={12} className="text-muted-foreground" />
        <span className="font-medium">{leg?.destination ?? "?"}</span>
      </div>
      {isInProgress(leg?.status) ? (
        <button
          disabled={completing}
          onClick={() => onComplete(leg?.trip_id ?? leg?.id)}
          className="mt-2 rounded-md bg-primary px-2 py-1 text-[11px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        >
          {completing ? "Completing…" : "Complete leg"}
        </button>
      ) : null}
    </div>
  );
}

function CycleCard({
  cycle,
  onComplete,
  completingId,
}: {
  cycle: any;
  onComplete: (tripId: any) => void;
  completingId: any;
}) {
  const legs: any[] = Array.isArray(cycle?.trips) ? cycle.trips : [];
  const isDouble = !!cycle?.is_double_trip;
  return (
    <div className="rounded-lg border border-border p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="font-mono text-[12px]">{cycle?.cycle_id ?? "—"}</span>
        <span className="rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
          {cycle?.vehicle_id ?? "—"}
        </span>
        <StatusChip
          label={isDouble ? "double-trip" : "single"}
          tone={isDouble ? "ok" : "neutral"}
        />
        <span className="text-[11px] text-muted-foreground">
          {cycle?.completed_count ?? 0}/{cycle?.trip_count ?? legs.length} legs
        </span>
        {cycle?.total_cycle_min != null ? (
          <span className="ml-auto text-[11px] tabular-nums text-muted-foreground">
            {cycle.total_cycle_min} min
          </span>
        ) : null}
      </div>
      {legs.length ? (
        <div className="flex flex-wrap items-stretch gap-2 md:flex-nowrap">
          {legs.map((leg, i) => (
            <div
              key={leg?.trip_id ?? leg?.trip_seq ?? i}
              className="flex flex-1 items-center gap-2"
            >
              <LegTile
                leg={leg}
                onComplete={onComplete}
                completing={completingId != null && completingId === (leg?.trip_id ?? leg?.id)}
              />
              {i < legs.length - 1 ? (
                <ArrowRight size={16} className="hidden shrink-0 text-muted-foreground md:block" />
              ) : null}
            </div>
          ))}
        </div>
      ) : (
        <EmptyState>No legs recorded for this cycle.</EmptyState>
      )}
    </div>
  );
}

export default function DoubleTrip() {
  const qc = useQueryClient();

  const statsQ = useQuery({
    queryKey: ["double-trip-stats"],
    queryFn: () => anyApi.doubleTripStatistics(),
    refetchInterval: 8000,
  });
  const cyclesQ = useQuery({
    queryKey: ["double-trip-cycles"],
    queryFn: () => anyApi.doubleTripCycles({ limit: 50 }),
  });

  const stats: any = statsQ.data ?? {};
  const cycles: any[] = Array.isArray(cyclesQ.data?.cycles) ? cyclesQ.data.cycles : [];
  const byVehicle: any[] = Array.isArray(stats?.by_vehicle) ? stats.by_vehicle : [];

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["double-trip-cycles"] });
    qc.invalidateQueries({ queryKey: ["double-trip-stats"] });
  };

  const complete = useMutation({
    mutationFn: (tripId: any) => anyApi.doubleTripComplete(tripId),
    onSuccess: invalidate,
  });

  // --- start-trip form ---
  const [vehicleId, setVehicleId] = useState("");
  const [origin, setOrigin] = useState("");
  const [destination, setDestination] = useState("");
  const [laden, setLaden] = useState(true);

  const start = useMutation({
    mutationFn: () =>
      anyApi.doubleTripStart({
        vehicle_id: vehicleId,
        origin,
        destination,
        laden,
      }),
    onSuccess: () => {
      setOrigin("");
      setDestination("");
      invalidate();
    },
  });

  const canStart = vehicleId.trim() && origin.trim() && destination.trim();
  const live = String(stats?.source || "").toLowerCase() === "live";
  const ratioPct =
    stats?.double_trip_ratio != null
      ? `${Math.round(Number(stats.double_trip_ratio) * 100)}%`
      : "—";

  return (
    <PageContainer>
      <PageHeader
        title="TT Double-Trip Workflow"
        subtitle="Trip 1 → Return → Trip 2 — terminal-tractor cycle analytics"
        icon={Repeat}
      />

      <div className="space-y-3 px-4 py-3">
        {/* -------------------- KPI strip -------------------- */}
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold">Statistics</h3>
          {stats?.source ? (
            <span
              className="rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide"
              style={{
                color: live ? STATUS.ok : STATUS.warning,
                backgroundColor: `${live ? STATUS.ok : STATUS.warning}1a`,
              }}
              title={live ? "Aggregated from live trip data" : "No trip data yet — baseline"}
            >
              {live ? "Live" : "Baseline"}
            </span>
          ) : null}
        </div>
        {statsQ.isLoading ? (
          <LoadingState />
        ) : (
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
            <StatCard label="Total cycles" value={stats?.total_cycles ?? "—"} />
            <StatCard label="Double-trip cycles" value={stats?.double_trip_cycles ?? "—"} />
            <StatCard label="Double-trip ratio" value={ratioPct} />
            <StatCard
              label="Avg trips / cycle"
              value={
                stats?.avg_trips_per_cycle != null
                  ? Number(stats.avg_trips_per_cycle).toFixed(2)
                  : "—"
              }
            />
            <StatCard
              label="Avg cycle time"
              value={stats?.avg_cycle_min != null ? Math.round(Number(stats.avg_cycle_min)) : "—"}
              unit="min"
            />
            <StatCard label="Trips today" value={stats?.trips_today ?? "—"} />
          </div>
        )}

        {/* -------------------- By-vehicle leaderboard -------------------- */}
        <Card className="p-4">
          <div className="mb-3 flex items-center gap-2">
            <Trophy size={15} />
            <h3 className="text-sm font-semibold">By vehicle</h3>
            <span className="text-[11px] text-muted-foreground">({byVehicle.length})</span>
          </div>
          {!byVehicle.length ? (
            <EmptyState>No per-vehicle data yet.</EmptyState>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[360px] border-collapse text-[12px]">
                <thead>
                  <tr className="text-left text-muted-foreground">
                    <th className="py-1 pr-3 font-medium">Vehicle</th>
                    <th className="py-1 pr-3 font-medium">Cycles</th>
                    <th className="py-1 pr-3 font-medium">Double-trips</th>
                  </tr>
                </thead>
                <tbody>
                  {byVehicle.map((v, i) => (
                    <tr key={(v?.vehicle_id ?? "") + i} className="border-t border-border">
                      <td className="py-1.5 pr-3 font-mono text-[11px]">{v?.vehicle_id ?? "—"}</td>
                      <td className="py-1.5 pr-3 tabular-nums">{v?.cycles ?? 0}</td>
                      <td className="py-1.5 pr-3 tabular-nums">{v?.double_trips ?? 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        {/* -------------------- Start-trip form -------------------- */}
        <Card className="p-4">
          <div className="mb-3 flex items-center gap-2">
            <Play size={15} />
            <h3 className="text-sm font-semibold">Start trip</h3>
          </div>
          <div className="flex flex-wrap items-end gap-2 text-sm">
            <label className="flex flex-col gap-0.5">
              <span className="text-[10px] text-muted-foreground">Vehicle ID</span>
              <input
                value={vehicleId}
                onChange={(e) => setVehicleId(e.target.value)}
                placeholder="TT-001"
                className="w-36 rounded-md border border-border bg-card px-2 py-1.5 outline-none"
              />
            </label>
            <label className="flex flex-col gap-0.5">
              <span className="text-[10px] text-muted-foreground">Origin</span>
              <input
                value={origin}
                onChange={(e) => setOrigin(e.target.value)}
                placeholder="Yard A"
                className="w-36 rounded-md border border-border bg-card px-2 py-1.5 outline-none"
              />
            </label>
            <label className="flex flex-col gap-0.5">
              <span className="text-[10px] text-muted-foreground">Destination</span>
              <input
                value={destination}
                onChange={(e) => setDestination(e.target.value)}
                placeholder="Berth 3"
                className="w-36 rounded-md border border-border bg-card px-2 py-1.5 outline-none"
              />
            </label>
            <label className="flex items-center gap-1.5 pb-1.5 text-[13px]">
              <input
                type="checkbox"
                checked={laden}
                onChange={(e) => setLaden(e.target.checked)}
                className="h-4 w-4"
              />
              Laden
            </label>
            <button
              disabled={!canStart || start.isPending}
              onClick={() => start.mutate()}
              className="rounded-md bg-primary px-3 py-1.5 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {start.isPending ? "Starting…" : "Start trip"}
            </button>
          </div>
          {start.isError && (
            <div className="mt-2 text-[11px]" style={{ color: STATUS.critical }}>
              {(start.error as Error)?.message}
            </div>
          )}
        </Card>

        {/* -------------------- Cycles list -------------------- */}
        <Card className="p-4">
          <div className="mb-3 flex items-center gap-2">
            <Repeat size={15} />
            <h3 className="text-sm font-semibold">Cycles</h3>
            <span className="text-[11px] text-muted-foreground">
              ({cyclesQ.data?.count ?? cycles.length})
            </span>
          </div>
          {cyclesQ.isLoading ? (
            <LoadingState />
          ) : !cycles.length ? (
            <EmptyState>No cycles yet — start a trip above.</EmptyState>
          ) : (
            <div className="space-y-3">
              {cycles.map((c, i) => (
                <CycleCard
                  key={c?.cycle_id ?? i}
                  cycle={c}
                  onComplete={(tripId) => complete.mutate(tripId)}
                  completingId={complete.isPending ? complete.variables : null}
                />
              ))}
            </div>
          )}
        </Card>
      </div>
    </PageContainer>
  );
}
