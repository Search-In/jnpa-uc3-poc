// UC3-027 — CPP per-terminal metered release (flow F-06), as a PANEL.
//
// Parking Management already owns plaza occupancy: its "facilities" tab renders
// capacity / occupied / available / utilisation from the same RDS slot state. A
// separate CPP board would have duplicated that table and split plaza work
// across two routes, so this adds ONLY what did not exist anywhere — the metered
// release and the dwell distribution — as a tab on the screen that already owns
// the plaza. It deliberately renders no zone/occupancy table of its own.
//
// The demonstrable claim is that only the CONGESTED terminal's release slows.
// The panel makes that checkable rather than asserted:
//
//  * each row shows the gate queue it read, the clearing rate it measured and
//    the release rate it derived, so the arithmetic is on screen;
//  * the METERED / UNIFORM toggle runs the same inputs through the do-nothing
//    comparison (UI-111), where one port-wide rate is applied to everybody and
//    the congested terminal visibly stops being protected;
//  * the driver advice is the sentence generated from those same numbers
//    (UI-156), not separately worded text that could drift from them.
//
// The release plan is SIMULATED — no CPP occupancy sensor feed exists in the
// corpus — and every row says so.
import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { AlertTriangle, Clock, MessageSquare } from "lucide-react";

import { api } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { DataTable, StatusChip, type Column, type Tone } from "@/components/ui/dtccc";
import type { CppReleasePlan } from "@/lib/types";

const CONGESTION_TONE: Record<string, Tone> = {
  LOW: "ok",
  MEDIUM: "warn",
  HIGH: "critical",
};

export default function CppMeteredReleasePanel() {
  const [mode, setMode] = useState<"METERED" | "UNIFORM">("METERED");

  // Dwell + the last persisted plan come from the board read; a recompute is a
  // POST because it persists the run as an audit trail, so it is an explicit
  // mutation rather than a poll.
  const boardQ = useQuery({
    queryKey: ["cpp-board"],
    queryFn: () => api.cppBoard(),
    refetchInterval: 10_000,
  });
  const recomputeM = useMutation({
    mutationFn: (m: "METERED" | "UNIFORM") => api.cppRecompute(m, true),
  });

  const board = boardQ.data;
  // Prefer a freshly recomputed plan; fall back to the last persisted one.
  const plans: CppReleasePlan[] = recomputeM.data?.plans ?? board?.release_plans ?? [];

  const planColumns: Column<CppReleasePlan>[] = [
    {
      key: "terminal_code",
      header: "Terminal",
      render: (p) => <span className="font-medium">{p.terminal_code}</span>,
    },
    {
      key: "gate_queue_vehicles",
      header: "Gate queue",
      align: "right",
      render: (p) => (
        <span className="inline-flex items-center gap-1.5">
          <span className="tabular-nums">{p.gate_queue_vehicles}</span>
          <StatusChip
            label={p.congestion_level}
            tone={CONGESTION_TONE[p.congestion_level] ?? "neutral"}
          />
        </span>
      ),
    },
    {
      key: "clearing_rate_vph",
      header: "Clearing",
      align: "right",
      render: (p) => `${p.clearing_rate_vph} veh/h`,
    },
    {
      key: "release_rate_vph",
      header: "Release rate",
      align: "right",
      render: (p) => {
        const throttled = p.release_rate_vph < p.clearing_rate_vph;
        return (
          <span className={throttled ? "font-semibold text-severity-warning" : "tabular-nums"}>
            {p.release_rate_vph} veh/h
            {throttled && <span className="ml-1 text-[10px]">throttled</span>}
          </span>
        );
      },
    },
    {
      key: "hold_minutes",
      header: "Hold",
      align: "right",
      render: (p) => (p.hold_minutes > 0 ? `${p.hold_minutes} min` : "—"),
    },
  ];

  const maxBucket = Math.max(...(board?.dwell_histogram ?? []).map((b) => b.trucks), 1);

  return (
    <div className="flex min-w-0 flex-col gap-3">
      <Card className="min-w-0 p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="min-w-0">
            <h3 className="text-sm font-semibold">Release-rate control</h3>
            <p className="mt-0.5 text-[11px] leading-snug text-muted-foreground">
              Recomputes each terminal&apos;s release rate from its own counted gate queue. Switch
              to UNIFORM for the do-nothing comparison: one port-wide rate for everybody.
            </p>
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <div className="inline-flex overflow-hidden rounded-md border border-border">
              {(["METERED", "UNIFORM"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => {
                    setMode(m);
                    recomputeM.mutate(m);
                  }}
                  className={
                    "px-3 py-1.5 text-[12px] font-medium " +
                    (mode === m
                      ? "bg-primary text-primary-foreground"
                      : "bg-background hover:bg-muted")
                  }
                >
                  {m}
                </button>
              ))}
            </div>
            <button
              type="button"
              onClick={() => recomputeM.mutate(mode)}
              disabled={recomputeM.isPending}
              className="rounded-md border border-border bg-muted px-3 py-1.5 text-[12px] font-medium hover:bg-muted/70 disabled:opacity-50"
            >
              {recomputeM.isPending ? "Recomputing…" : "Recompute now"}
            </button>
          </div>
        </div>
        {recomputeM.isError && (
          <p className="mt-2 flex items-start gap-1.5 text-[11px] text-severity-critical">
            <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden />
            {(recomputeM.error as Error).message}
          </p>
        )}
        {recomputeM.data && (
          <p className="mt-2 text-[11px] leading-snug text-muted-foreground">
            {recomputeM.data.note} Recompute budget {recomputeM.data.recompute_budget_seconds}s.
          </p>
        )}
      </Card>

      <DataTable<CppReleasePlan>
        columns={planColumns}
        rows={plans}
        rowKey={(p) => `${p.mode}-${p.terminal_code}`}
        status={{
          isLoading: boardQ.isLoading || recomputeM.isPending,
          isError: boardQ.isError,
          error: boardQ.error,
        }}
        onRetry={() => void boardQ.refetch()}
        emptyLabel="No release plan yet — recompute to generate one."
      />

      {plans.length > 0 && (
        <Card className="min-w-0 p-3">
          <h3 className="flex flex-wrap items-center gap-1.5 text-sm font-semibold">
            <MessageSquare className="h-4 w-4 shrink-0" aria-hidden />
            Driver advice
            <StatusChip label="SIMULATED" tone="warn" />
          </h3>
          <p className="mt-1 text-[11px] text-muted-foreground">
            Generated from the same numbers as the table above and shown verbatim in the driver PWA.
          </p>
          <ul className="mt-2 flex flex-col gap-1.5">
            {plans.map((p) => (
              <li
                key={p.terminal_code}
                className="min-w-0 rounded-md border border-border bg-muted/30 p-2"
              >
                <span className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                  {p.terminal_code}
                </span>
                <p className="mt-0.5 break-words text-[12px] leading-snug">{p.advice_text}</p>
              </li>
            ))}
          </ul>
          {/* Guarded: this app has no error boundary, so one undefined here would
              white-screen the whole Parking screen. */}
          {plans[0].method && (
            <p className="mt-2 text-[10px] leading-snug text-muted-foreground">
              Hold time = {String(plans[0].method.hold_minutes_formula)}, target{" "}
              {String(plans[0].method.queue_target_vehicles)} vehicles (KPI 6). Release rate ={" "}
              {String(plans[0].method.release_rate_formula)}.
            </p>
          )}
        </Card>
      )}

      <Card className="min-w-0 p-3">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold">
          <Clock className="h-4 w-4 shrink-0" aria-hidden />
          Dwell distribution
        </h3>
        {board?.dwell_status === "NO_DATA" ? (
          <p className="mt-2 text-[12px] text-muted-foreground">
            No completed parking transactions to build a histogram from. Nothing is shown rather
            than an invented distribution.
          </p>
        ) : (
          <ul className="mt-2 flex flex-col gap-1.5">
            {(board?.dwell_histogram ?? []).map((b) => (
              <li key={b.bucket} className="min-w-0">
                <div className="flex items-baseline justify-between gap-2 text-[11px]">
                  <span className="text-muted-foreground">
                    {b.min_minutes !== null && b.max_minutes !== null
                      ? `${Math.round(b.min_minutes)}–${Math.round(b.max_minutes)} min`
                      : `bucket ${b.bucket}`}
                  </span>
                  <span className="font-semibold tabular-nums">{b.trucks}</span>
                </div>
                <div className="mt-0.5 h-2 w-full overflow-hidden rounded bg-muted">
                  <div
                    className="h-full bg-primary"
                    style={{ width: `${(b.trucks / maxBucket) * 100}%` }}
                  />
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {board && (
        <p className="text-[11px] leading-snug text-muted-foreground">
          Amenity status: <span className="font-medium">{board.amenities.status}</span> —{" "}
          {board.amenities.note}
        </p>
      )}
    </div>
  );
}
