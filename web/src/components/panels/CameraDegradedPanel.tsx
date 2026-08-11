// UC3-023 — EC-6 camera-outage degraded mode, on the Gate & Lane Board.
//
// A tab on the board that already owns gate operations, not a second camera
// screen. The board is where the consequence lands: losing a camera cuts the
// gate's service rate, which is what makes the queue forecast worsen.
//
// Everything shown here comes from /api/gate-board/degraded-mode, which reads
// each camera's ACTUAL rung out of the ANPR cascade — including a rung forced
// through the fault console. There is no UI-only status in this file: if the
// panel says DOWN, the feed is down.
//
// Two things it will not do:
//
//  * **Call replayed frames live.** A cached feed is badged REPLAY. The whole
//    point of the ladder is that an evaluator can tell which frames are current.
//  * **Hide the confidence drop.** When the ANPR half of the join is gone the
//    gate confirms on RFID alone at a lower, recorded confidence. Showing the
//    old number would assert a certainty the gate no longer has, and every
//    downstream decision would inherit it.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Radio, RotateCcw, Video } from "lucide-react";

import { api } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { DataTable, StatusChip, type Column, type Tone } from "@/components/ui/dtccc";
import type { CameraDegradedMode } from "@/lib/types";

const RUNG_TONE: Record<string, Tone> = {
  LIVE: "ok",
  DEGRADED: "warn",
  NO_FEED: "critical",
};

const CARD_TONE: Record<string, Tone> = {
  LIVE: "ok",
  DEGRADED: "warn",
  DOWN: "critical",
};

/** REPLAY must never read as LIVE, so it gets its own tone. */
const FEED_TONE: Record<string, Tone> = {
  LIVE: "ok",
  REPLAY: "warn",
  "NO FEED": "critical",
};

export default function CameraDegradedPanel() {
  const qc = useQueryClient();

  const q = useQuery({
    queryKey: ["gate-degraded-mode"],
    queryFn: () => api.gateDegradedMode(),
    // The EC-6 contract is no_feed within 5 s, so the panel polls inside that
    // budget rather than on the board's slower 10 s cadence.
    refetchInterval: 5_000,
  });

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["gate-degraded-mode"] });
    void qc.invalidateQueries({ queryKey: ["gate-board"] });
  };

  const injectM = useMutation({
    mutationFn: (rung: string) => api.injectFault("camera", rung),
    onSuccess: invalidate,
  });
  const clearM = useMutation({
    mutationFn: () => api.clearFault("camera"),
    onSuccess: invalidate,
  });

  const d = q.data;

  const columns: Column<CameraDegradedMode>[] = [
    {
      key: "camera_id",
      header: "Camera",
      render: (c) => <span className="font-mono text-[12px]">{c.camera_id}</span>,
    },
    {
      key: "source_card",
      header: "Source card",
      render: (c) => (
        <StatusChip label={c.source_card} tone={CARD_TONE[c.source_card] ?? "neutral"} />
      ),
    },
    {
      key: "feed_label",
      header: "Feed",
      render: (c) => (
        <StatusChip label={c.feed_label} tone={FEED_TONE[c.feed_label] ?? "neutral"} />
      ),
    },
    {
      key: "confirmation_mode",
      header: "Confirmation",
      render: (c) => (
        <span className="inline-flex flex-wrap items-center gap-1">
          <span className="text-[11px] font-medium">
            {c.confirmation_mode === "RFID_ONLY" ? "RFID only" : "ANPR + RFID"}
          </span>
          {c.manual_verify_lane && <StatusChip label="MANUAL VERIFY" tone="warn" />}
        </span>
      ),
    },
    {
      key: "confidence",
      header: "Confidence",
      align: "right",
      render: (c) => (
        <span
          className={
            c.rung === "LIVE" ? "tabular-nums" : "font-semibold tabular-nums text-severity-warning"
          }
          title={c.confidence_basis}
        >
          {c.confidence.toFixed(2)}
        </span>
      ),
    },
    {
      key: "frame_age_s",
      header: "Frame age",
      align: "right",
      render: (c) => (c.frame_age_s === null ? "—" : `${c.frame_age_s}s`),
    },
    {
      key: "fault_injected",
      header: "Source",
      render: (c) =>
        c.fault_injected ? (
          <StatusChip label="FAULT INJECTED" tone="info" />
        ) : (
          <span className="text-[10px] text-muted-foreground">real cascade</span>
        ),
    },
  ];

  return (
    <div className="flex min-w-0 flex-col gap-3">
      <Card className="min-w-0 p-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <h3 className="flex flex-wrap items-center gap-1.5 text-sm font-semibold">
              <Video className="h-4 w-4 shrink-0" aria-hidden />
              Camera feed health
              {d && (
                <StatusChip label={d.overall_rung} tone={RUNG_TONE[d.overall_rung] ?? "neutral"} />
              )}
            </h3>
            <p className="mt-0.5 text-[11px] leading-snug text-muted-foreground">
              {d?.timing_contract.note ??
                "Frames stop → no_feed within 5 s; the source card reads DOWN within 60 s."}
            </p>
          </div>

          {/* The drill uses the project's existing fault console, not a new one. */}
          <div className="flex shrink-0 flex-wrap items-center gap-1.5">
            <span className="text-[10px] text-muted-foreground">Fault drill:</span>
            {(["LIVE", "CACHED", "SYNTHETIC"] as const).map((r) => (
              <button
                key={r}
                type="button"
                onClick={() => injectM.mutate(r)}
                disabled={injectM.isPending}
                className="rounded-md border border-border bg-background px-2 py-1 text-[11px] font-medium hover:bg-muted disabled:opacity-50"
              >
                {r}
              </button>
            ))}
            <button
              type="button"
              onClick={() => clearM.mutate()}
              disabled={clearM.isPending}
              className="inline-flex items-center gap-1 rounded-md border border-border bg-muted px-2 py-1 text-[11px] font-medium hover:bg-muted/70 disabled:opacity-50"
            >
              <RotateCcw className="h-3 w-3" aria-hidden />
              Restore
            </button>
          </div>
        </div>

        {d && (
          <dl className="mt-2 grid grid-cols-2 gap-2 border-t border-border pt-2 sm:grid-cols-4">
            <div className="min-w-0">
              <dt className="text-[10px] uppercase text-muted-foreground">Cameras down</dt>
              <dd className="text-sm font-semibold tabular-nums">
                {d.no_feed_count} / {d.count}
              </dd>
            </div>
            <div className="min-w-0">
              <dt className="text-[10px] uppercase text-muted-foreground">Degraded</dt>
              <dd className="text-sm font-semibold tabular-nums">{d.degraded_count}</dd>
            </div>
            <div className="min-w-0">
              <dt className="text-[10px] uppercase text-muted-foreground">Service rate</dt>
              <dd className="text-sm font-semibold tabular-nums">
                {d.effective_service_vph} veh/h
              </dd>
              <p className="text-[9px] leading-tight text-muted-foreground">
                of {d.nominal_service_vph} nominal (×{d.service_rate_factor})
              </p>
            </div>
            <div className="min-w-0">
              <dt className="text-[10px] uppercase text-muted-foreground">Detect budget</dt>
              <dd className="text-sm font-semibold tabular-nums">
                {d.timing_contract.no_feed_detect_seconds}s
              </dd>
              <p className="text-[9px] leading-tight text-muted-foreground">
                card DOWN within {d.timing_contract.card_down_visible_seconds}s
              </p>
            </div>
          </dl>
        )}

        {clearM.data?.reconciliation && (
          <p className="mt-2 flex items-start gap-1.5 rounded-md border border-emerald-500/40 bg-emerald-500/10 p-2 text-[11px] text-emerald-700 dark:text-emerald-400">
            <RotateCcw className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden />
            <span className="min-w-0 break-words">
              Restored — reconciliation written to the decision log (
              {String(
                (clearM.data.reconciliation as Record<string, unknown>).decision_path ?? "RESTORED",
              )}
              , from rung{" "}
              {String((clearM.data.reconciliation as Record<string, unknown>).from_rung ?? "—")}).
            </span>
          </p>
        )}

        {d && d.overall_rung !== "LIVE" && (
          <p className="mt-2 flex items-start gap-1.5 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-[11px] leading-snug text-amber-700 dark:text-amber-400">
            <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden />
            <span className="min-w-0">
              {d.no_feed_count > 0
                ? "Frames have stopped. The gate confirms on RFID alone at a reduced, recorded confidence and opens a manual-verify lane; the service rate is cut, so the queue forecast worsens."
                : "Frames are stale and served from cache. They are badged REPLAY, never LIVE, and the join confidence is reduced accordingly."}
            </span>
          </p>
        )}
      </Card>

      <DataTable<CameraDegradedMode>
        columns={columns}
        rows={d?.cameras ?? []}
        rowKey={(c) => c.camera_id}
        status={q}
        onRetry={() => void q.refetch()}
        emptyLabel="No cameras reporting."
      />

      {d && (
        <p className="text-[10px] leading-snug text-muted-foreground">
          <span className="inline-flex items-center gap-1">
            <Radio className="h-3 w-3" aria-hidden />
            {d.reconciliation.note}
          </span>{" "}
          Fault drill: {d.fault_injection.endpoint}.
        </p>
      )}
    </div>
  );
}
