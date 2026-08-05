// Three Road Bottleneck Analytics (Feature 9) — ranks the worst 3 congestion
// segments from live traffic_snapshots (or metadata baseline), shows them as a
// ranked podium, a delay bar chart, and a persisted history table. Backed by
// /api/bottlenecks, /api/bottlenecks/snapshot (POST), /api/bottlenecks/history.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { TrafficCone, Camera, Clock, Gauge } from "lucide-react";
import { api } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/ui/dtccc";
import { Card } from "@/components/ui/card";
import { EmptyState, LoadingState } from "@/components/ui/misc";
import { STATUS } from "@/lib/tokens";
import { fmtDateTimeIST } from "@/lib/utils";

// jam_factor is 0..10 (HERE-style). Map to a severity colour: low = green,
// mid = amber, high = red.
function jamColour(jf: number): string {
  const j = Number(jf) || 0;
  if (j >= 7) return STATUS.critical;
  if (j >= 4) return STATUS.warning;
  return STATUS.ok;
}

const RANK_LABEL: Record<number, string> = { 1: "#1", 2: "#2", 3: "#3" };
const RANK_TONE: Record<number, string> = {
  1: STATUS.critical,
  2: STATUS.warning,
  3: STATUS.info,
};

function PodiumCard({ b }: { b: any }) {
  const jf = Number(b?.jam_factor) || 0;
  const jamPct = Math.max(0, Math.min(100, (jf / 10) * 100));
  const colour = jamColour(jf);
  const tone = RANK_TONE[b?.rank] ?? STATUS.info;
  const speed = Number(b?.speed_kmh) || 0;
  const freeFlow = Number(b?.free_flow_kmh) || 0;

  return (
    <Card className="flex flex-col gap-3 p-4">
      <div className="flex items-center gap-2">
        <span
          className="grid h-7 w-9 place-items-center rounded-md text-[13px] font-bold text-white"
          style={{ backgroundColor: tone }}
        >
          {RANK_LABEL[b?.rank] ?? `#${b?.rank}`}
        </span>
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold" title={b?.name}>
            {b?.name ?? b?.segment_id ?? "—"}
          </div>
          <div className="font-mono text-[10px] text-muted-foreground">{b?.segment_id}</div>
        </div>
      </div>

      {/* Avg delay — the headline number */}
      <div className="flex items-baseline gap-1.5">
        <Clock size={14} className="text-muted-foreground" />
        <span className="text-2xl font-bold tabular-nums" style={{ color: colour }}>
          {Number(b?.avg_delay_min ?? 0).toFixed(1)}
        </span>
        <span className="text-[11px] text-muted-foreground">min avg delay</span>
      </div>

      {/* Jam factor severity bar */}
      <div>
        <div className="mb-1 flex items-center justify-between text-[10px] text-muted-foreground">
          <span>Jam factor</span>
          <span className="font-mono tabular-nums" style={{ color: colour }}>
            {jf.toFixed(1)} / 10
          </span>
        </div>
        <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
          <div
            className="h-full rounded-full transition-all"
            style={{ width: `${jamPct}%`, backgroundColor: colour }}
          />
        </div>
      </div>

      {/* Current speed vs free-flow */}
      <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <Gauge size={13} />
        <span className="tabular-nums">
          <strong className="text-foreground">{speed.toFixed(0)}</strong> km/h
        </span>
        <span>/</span>
        <span className="tabular-nums">{freeFlow.toFixed(0)} km/h free-flow</span>
      </div>
    </Card>
  );
}

function DelayChart({ bottlenecks }: { bottlenecks: any[] }) {
  const max = Math.max(1, ...bottlenecks.map((b) => Number(b?.avg_delay_min) || 0));
  return (
    <div className="space-y-2">
      {bottlenecks.map((b) => {
        const delay = Number(b?.avg_delay_min) || 0;
        const pct = Math.max(2, (delay / max) * 100);
        const colour = jamColour(Number(b?.jam_factor) || 0);
        return (
          <div key={b?.segment_id ?? b?.rank} className="flex items-center gap-2 text-[12px]">
            <span className="w-32 shrink-0 truncate text-muted-foreground" title={b?.name}>
              {RANK_LABEL[b?.rank] ?? `#${b?.rank}`} {b?.name ?? b?.segment_id}
            </span>
            <div className="flex-1">
              <div
                className="flex h-5 items-center justify-end rounded pr-1.5 text-[10px] font-semibold text-white transition-all"
                style={{ width: `${pct}%`, backgroundColor: colour, minWidth: "2.5rem" }}
              >
                {delay.toFixed(1)}m
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default function RoadBottlenecks() {
  const qc = useQueryClient();

  const bottlenecksQ = useQuery({
    queryKey: ["bottlenecks", 3],
    queryFn: () => api.bottlenecks(3),
    refetchInterval: 10000,
  });
  const historyQ = useQuery({
    queryKey: ["bottleneck-history"],
    queryFn: () => api.bottleneckHistory(60),
  });

  const snapshot = useMutation({
    mutationFn: () => api.bottleneckSnapshot(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["bottleneck-history"] });
      qc.invalidateQueries({ queryKey: ["bottlenecks", 3] });
    },
  });

  const data = bottlenecksQ.data;
  const bottlenecks: any[] = data?.bottlenecks ?? [];
  const live = data?.source === "traffic_snapshots";
  const snapshots: any[] = historyQ.data?.snapshots ?? [];

  return (
    <PageContainer>
      <PageHeader
        title="Three Road Bottlenecks"
        subtitle="Worst congestion segments — ranked live from traffic snapshots"
        icon={TrafficCone}
      />

      <div className="space-y-3 px-4 py-3">
        {/* ---------------- Header: source badge + snapshot ---------------- */}
        <div className="flex flex-wrap items-center gap-3">
          {data && (
            <span
              className="rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide"
              style={{
                color: live ? STATUS.ok : STATUS.warning,
                backgroundColor: `${live ? STATUS.ok : STATUS.warning}1a`,
              }}
              title={
                live
                  ? "Ranked from live traffic_snapshots"
                  : "No live snapshots yet — showing metadata baseline"
              }
            >
              {live ? "Live" : "Baseline"} · {data?.source}
            </span>
          )}
          {data?.generated_at && (
            <span className="text-[11px] text-muted-foreground">
              Updated {fmtDateTimeIST(data.generated_at)}
            </span>
          )}
          <button
            disabled={snapshot.isPending}
            onClick={() => snapshot.mutate()}
            className="ml-auto flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            <Camera size={14} />
            {snapshot.isPending ? "Saving…" : "Take snapshot"}
          </button>
        </div>
        {snapshot.isError && (
          <div className="text-[11px]" style={{ color: STATUS.critical }}>
            {(snapshot.error as Error)?.message}
          </div>
        )}
        {snapshot.data && (
          <div className="text-[11px]" style={{ color: STATUS.ok }}>
            Snapshot saved · {snapshot.data?.persisted ?? snapshot.data?.bottlenecks?.length ?? 0}{" "}
            row(s) persisted
          </div>
        )}

        {/* ---------------- Podium ---------------- */}
        {bottlenecksQ.isLoading ? (
          <LoadingState />
        ) : !bottlenecks.length ? (
          <EmptyState>No bottlenecks available — start the gateway or take a snapshot.</EmptyState>
        ) : (
          <>
            <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
              {bottlenecks.map((b) => (
                <PodiumCard key={b?.segment_id ?? b?.rank} b={b} />
              ))}
            </div>

            {/* ---------------- Delay comparison chart ---------------- */}
            <Card className="p-4">
              <div className="mb-3 flex items-center gap-2">
                <Clock size={15} />
                <h3 className="text-sm font-semibold">Avg delay comparison</h3>
                <span className="text-[11px] text-muted-foreground">minutes</span>
              </div>
              <DelayChart bottlenecks={bottlenecks} />
            </Card>
          </>
        )}

        {/* ---------------- History ---------------- */}
        <Card className="p-4">
          <div className="mb-3 flex items-center gap-2">
            <h3 className="text-sm font-semibold">Snapshot history</h3>
            <span className="text-[11px] text-muted-foreground">
              ({historyQ.data?.count ?? snapshots.length})
            </span>
          </div>
          {historyQ.isLoading ? (
            <LoadingState />
          ) : !snapshots.length ? (
            <EmptyState>No history yet — take a snapshot to persist the current top-3.</EmptyState>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] border-collapse text-[12px]">
                <thead>
                  <tr className="text-left text-muted-foreground">
                    <th className="py-1 pr-3 font-medium">When</th>
                    <th className="py-1 pr-3 font-medium">Rank</th>
                    <th className="py-1 pr-3 font-medium">Segment</th>
                    <th className="py-1 pr-3 font-medium">Jam</th>
                    <th className="py-1 pr-3 font-medium">Speed</th>
                    <th className="py-1 pr-3 font-medium">Delay</th>
                  </tr>
                </thead>
                <tbody>
                  {snapshots.map((s, i) => (
                    <tr
                      key={`${s?.ts ?? ""}-${s?.segment_id ?? ""}-${i}`}
                      className="border-t border-border align-top"
                    >
                      <td className="py-1.5 pr-3 whitespace-nowrap text-muted-foreground">
                        {s?.ts ? fmtDateTimeIST(s.ts) : "—"}
                      </td>
                      <td className="py-1.5 pr-3 tabular-nums">{RANK_LABEL[s?.rank] ?? s?.rank}</td>
                      <td className="py-1.5 pr-3">
                        <span className="font-medium">{s?.name ?? s?.segment_id}</span>
                        <span className="ml-1 font-mono text-[10px] text-muted-foreground">
                          {s?.segment_id}
                        </span>
                      </td>
                      <td
                        className="py-1.5 pr-3 font-mono tabular-nums"
                        style={{ color: jamColour(Number(s?.jam_factor) || 0) }}
                      >
                        {(Number(s?.jam_factor) || 0).toFixed(1)}
                      </td>
                      <td className="py-1.5 pr-3 tabular-nums text-muted-foreground">
                        {(Number(s?.speed_kmh) || 0).toFixed(0)} km/h
                      </td>
                      <td className="py-1.5 pr-3 tabular-nums">
                        {(Number(s?.avg_delay_min) || 0).toFixed(1)} min
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </PageContainer>
  );
}
