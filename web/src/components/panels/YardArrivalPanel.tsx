import { useState } from "react";
import { useTranslation } from "react-i18next";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { TruckArrivalHold, YardCapacity, YardEvaluation } from "@/lib/types";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, Spinner } from "@/components/ui/misc";
import { StatCard, StatGrid, StatusChip, RefreshButton } from "@/components/ui/dtccc";
import type { Tone } from "@/components/ui/dtccc";
import { fmtEta } from "@/lib/utils";
import { Warehouse, ParkingCircle, TriangleAlert, PlayCircle, Undo2 } from "lucide-react";

// UC-3 — Peak yard utilisation and truck-arrival management.
//
// Two panels that sit on the Congestion Rerouting console, above the gate-queue
// table they explain:
//
//   YardCapacityPanel   the live yard board (utilisation %, capacity, occupied,
//                       available, status) plus the audited demo controls that
//                       drive utilisation up and release it again.
//   ArrivalManagementPanel  the trucks whose arrival was managed, with the REAL
//                       parking facility each was sent to and the provenance
//                       (simulator vs enrolled PWA driver) kept visible.
//
// Every number rendered here comes from GET /api/yard/capacity/board or
// GET /api/yard/arrivals/holds. Nothing is computed in the browser — the panel
// cannot drift from the decision the backend actually made and audited.

const REFRESH_MS = 5_000;

function toneFor(status: string | undefined): Tone {
  switch (status) {
    case "CRITICAL":
      return "critical";
    case "HIGH":
      return "warn";
    case "ELEVATED":
      return "info";
    case "NORMAL":
      return "ok";
    default:
      return "neutral";
  }
}

export function YardCapacityPanel({
  yardId,
  onYardChange,
}: {
  yardId?: string;
  onYardChange?: (id: string) => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [note, setNote] = useState<string | null>(null);

  const board = useQuery({
    queryKey: ["yard", "capacity", yardId ?? "default"],
    queryFn: () => api.yardCapacityBoard({ yard_id: yardId, events: 8 }),
    refetchInterval: REFRESH_MS,
    placeholderData: keepPreviousData,
  });

  const yard = board.data?.yard ?? null;
  const activeYardId = yard?.yard_id;

  const invalidate = async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["yard"] }),
      qc.invalidateQueries({ queryKey: ["trucks"] }),
    ]);
  };

  // Raise utilisation to the configured critical threshold (~95%), then run the
  // detection in one click — the demo's "peak yard" action.
  const peak = useMutation({
    mutationFn: async () => {
      if (!activeYardId || !yard) throw new Error("no yard selected");
      await api.yardCapacityAdjust(activeYardId, {
        target_utilization_pct: yard.thresholds.critical_utilization_pct,
        event_type: "INCREASE",
        reason: "demo: drive yard to peak utilisation",
      });
      return api.yardArrivalEvaluate(activeYardId);
    },
    onSuccess: (res: YardEvaluation) => {
      setNote(
        res.constrained
          ? t("yard.peakHeld", "{{n}} truck(s) held — {{reason}}", {
              n: res.held.length,
              reason: res.reason ?? "",
            })
          : (res.detail ?? t("yard.peakNoHold", "No arrival management required.")),
      );
      void invalidate();
    },
    onError: (e: Error) => setNote(e.message),
  });

  // Re-run detection without changing occupancy (arrivals may have grown).
  const evaluate = useMutation({
    mutationFn: () => api.yardArrivalEvaluate(activeYardId!),
    onSuccess: (res: YardEvaluation) => {
      setNote(
        res.constrained
          ? t("yard.evalHeld", "{{n}} newly held.", { n: res.held.length })
          : (res.detail ?? t("yard.peakNoHold", "No arrival management required.")),
      );
      void invalidate();
    },
    onError: (e: Error) => setNote(e.message),
  });

  // Capacity recovery: free ground slots (5 containers x slots-per-truck) and
  // release as many held trucks as the recovered room absorbs.
  const releaseFive = useMutation({
    mutationFn: () => {
      const per = yard?.thresholds.slots_per_truck ?? 2;
      return api.yardArrivalRelease(activeYardId!, {
        free_slots: 5 * per,
        reason: "demo: release 5 containers",
      });
    },
    onSuccess: (res) => {
      setNote(
        t("yard.released", "{{n}} truck(s) released — {{reason}}", {
          n: res.released_count,
          reason: res.reason,
        }),
      );
      void invalidate();
    },
    onError: (e: Error) => setNote(e.message),
  });

  const resetNormal = useMutation({
    mutationFn: async () => {
      await api.yardCapacityAdjust(activeYardId!, {
        target_utilization_pct: 70,
        event_type: "RELEASE",
        reason: "demo: reset yard to normal utilisation",
      });
      return api.yardArrivalRelease(activeYardId!, { reason: "yard back to normal" });
    },
    onSuccess: (res) => {
      setNote(
        t("yard.resetDone", "Yard reset to normal; {{n}} truck(s) released.", {
          n: res.released_count,
        }),
      );
      void invalidate();
    },
    onError: (e: Error) => setNote(e.message),
  });

  const busy =
    peak.isPending || evaluate.isPending || releaseFive.isPending || resetNormal.isPending;

  return (
    <div className="px-4 py-3" data-testid="yard-capacity-panel">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-sm font-semibold text-foreground">
        <Warehouse className="h-4 w-4 text-muted-foreground" />
        {t("yard.title", "Yard Capacity")}
        {board.data?.yards && board.data.yards.length > 1 && (
          <select
            className="rounded-md border border-border bg-background px-2 py-1 text-xs font-normal text-foreground"
            aria-label={t("yard.selectYard", "Select yard")}
            value={activeYardId ?? ""}
            onChange={(e) => onYardChange?.(e.target.value)}
          >
            {board.data.yards.map((y: YardCapacity) => (
              <option key={y.yard_id} value={y.yard_id}>
                {y.name}
              </option>
            ))}
          </select>
        )}
        <span className="ml-auto" />
        <RefreshButton
          onRefresh={() => void board.refetch()}
          isRefreshing={board.isFetching && !board.isLoading}
        />
      </div>

      {board.isError ? (
        <Card>
          <ErrorState
            onRetry={() => void board.refetch()}
            detail={(board.error as Error)?.message}
          />
        </Card>
      ) : board.data?.degraded || (!board.isLoading && !yard) ? (
        <Card>
          <div className="flex flex-col items-center gap-2 p-6 text-center">
            <TriangleAlert className="h-5 w-5 text-severity-warning" aria-hidden />
            <p className="text-sm font-medium text-foreground">
              {t("yard.unavailable", "Yard capacity store unavailable")}
            </p>
            <p className="max-w-xl text-xs text-muted-foreground">
              {board.data?.detail ??
                t(
                  "yard.unavailableHint",
                  "core.yard_capacity_state could not be read. Apply migration 0144 and retry.",
                )}
            </p>
          </div>
        </Card>
      ) : (
        <>
          <StatGrid className="lg:grid-cols-5">
            <StatCard
              icon={Warehouse}
              label={t("yard.utilization", "Yard utilisation")}
              value={yard ? `${yard.utilization_pct.toFixed(1)}%` : "—"}
              tone={toneFor(yard?.capacity_status)}
              sub={yard ? yard.name : undefined}
              loading={board.isLoading}
            />
            <StatCard
              label={t("yard.capacity", "Total capacity")}
              value={yard?.capacity_slots ?? "—"}
              tone="neutral"
              sub={
                yard
                  ? yard.capacity_declared
                    ? t("yard.capacityDeclared", "declared · {{src}}", {
                        src: yard.capacity_source,
                      })
                    : t("yard.capacityMeasured", "from {{src}}", { src: yard.capacity_source })
                  : undefined
              }
              loading={board.isLoading}
            />
            <StatCard
              label={t("yard.occupied", "Occupied slots")}
              value={yard?.occupied_slots ?? "—"}
              tone="info"
              loading={board.isLoading}
            />
            <StatCard
              label={t("yard.available", "Available slots")}
              value={yard?.available_slots ?? "—"}
              tone={yard && yard.headroom_slots === 0 ? "warn" : "ok"}
              sub={
                yard
                  ? t("yard.headroom", "{{n}} bookable below ceiling", {
                      n: yard.headroom_slots,
                    })
                  : undefined
              }
              loading={board.isLoading}
            />
            <StatCard
              label={t("yard.status", "Capacity status")}
              value={yard?.capacity_status ?? "—"}
              tone={toneFor(yard?.capacity_status)}
              sub={
                yard
                  ? t("yard.thresholdHint", "high {{h}}% · critical {{c}}%", {
                      h: yard.thresholds.high_utilization_pct,
                      c: yard.thresholds.critical_utilization_pct,
                    })
                  : undefined
              }
              loading={board.isLoading}
            />
          </StatGrid>

          <Card className="mt-3">
            <CardContent className="flex flex-wrap items-center gap-2 p-3">
              <Button
                size="sm"
                variant="outline"
                disabled={busy || !activeYardId}
                onClick={() => peak.mutate()}
                data-testid="yard-peak"
              >
                <PlayCircle className="h-3.5 w-3.5" />
                {t("yard.actionPeak", "Increase to peak ({{pct}}%)", {
                  pct: yard?.thresholds.critical_utilization_pct ?? 95,
                })}
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={busy || !activeYardId}
                onClick={() => evaluate.mutate()}
                data-testid="yard-evaluate"
              >
                {t("yard.actionEvaluate", "Run arrival management")}
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={busy || !activeYardId}
                onClick={() => releaseFive.mutate()}
                data-testid="yard-release"
              >
                <Undo2 className="h-3.5 w-3.5" />
                {t("yard.actionRelease", "Release 5 containers")}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={busy || !activeYardId}
                onClick={() => resetNormal.mutate()}
              >
                {t("yard.actionReset", "Reset to normal")}
              </Button>
              {busy && <Spinner />}
              {note && (
                <span role="status" className="text-xs text-muted-foreground">
                  {note}
                </span>
              )}
              {yard?.source_note && (
                <span className="w-full text-[11px] text-muted-foreground">{yard.source_note}</span>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}

export function ArrivalManagementPanel({ yardId }: { yardId?: string }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const holds = useQuery({
    queryKey: ["yard", "holds", yardId ?? "all"],
    queryFn: () => api.yardArrivalHolds({ yard_id: yardId, history: 10 }),
    refetchInterval: REFRESH_MS,
    placeholderData: keepPreviousData,
  });

  const releaseOne = useMutation({
    mutationFn: (h: TruckArrivalHold) =>
      api.yardArrivalRelease(h.yard_id, {
        device_ids: [h.device_id],
        force: true,
        reason: "operator released this truck",
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["yard"] }),
  });

  const rows = holds.data?.holds ?? [];

  return (
    <div className="px-4 py-3" data-testid="arrival-management-panel">
      <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-foreground">
        <ParkingCircle className="h-4 w-4 text-muted-foreground" />
        {t("yard.arrivalTitle", "Truck Arrival Management")}
        <span className="rounded-full border border-border bg-muted px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
          {t("yard.arrivalCount", "{{n}} waiting", { n: holds.data?.active_count ?? 0 })}
        </span>
        <span className="ml-auto" />
        <RefreshButton
          onRefresh={() => void holds.refetch()}
          isRefreshing={holds.isFetching && !holds.isLoading}
        />
      </div>
      <Card className="overflow-hidden">
        {holds.isError ? (
          <ErrorState onRetry={() => void holds.refetch()} />
        ) : holds.isLoading ? (
          <div className="flex items-center gap-2 p-6 text-sm text-muted-foreground">
            <Spinner /> {t("yard.arrivalLoading", "Loading held trucks…")}
          </div>
        ) : rows.length === 0 ? (
          <EmptyState>
            {t(
              "yard.arrivalEmpty",
              "No truck arrivals are being managed — the yard has capacity for the current approach.",
            )}
          </EmptyState>
        ) : (
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b border-border bg-muted/60 text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2">{t("yard.colDevice", "Device / Vehicle")}</th>
                    <th className="px-4 py-2">{t("yard.colDriver", "Driver")}</th>
                    <th className="px-4 py-2">{t("yard.colGate", "Current gate")}</th>
                    <th className="px-4 py-2">{t("yard.colEta", "ETA")}</th>
                    <th className="px-4 py-2">{t("yard.colYardStatus", "Yard status")}</th>
                    <th className="px-4 py-2">{t("yard.colYardUtil", "Yard utilisation")}</th>
                    <th className="px-4 py-2">{t("yard.colParking", "Recommended parking")}</th>
                    <th className="px-4 py-2">{t("yard.colSource", "Source")}</th>
                    <th className="px-4 py-2 text-right">{t("yard.colAction", "Action")}</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((h) => (
                    <tr key={h.id} className="border-b border-border/50 hover:bg-muted/40">
                      <td className="px-4 py-2 font-mono text-xs">
                        {h.device_id}
                        {h.plate ? (
                          <span className="block text-[11px] text-muted-foreground">{h.plate}</span>
                        ) : null}
                      </td>
                      <td className="px-4 py-2 text-xs">{h.driver_name ?? h.driver_id ?? "—"}</td>
                      <td className="px-4 py-2 text-xs">{h.gate_id ?? "—"}</td>
                      <td className="px-4 py-2 tabular-nums">
                        {h.eta_s == null ? "—" : fmtEta(h.eta_s)}
                      </td>
                      <td className="px-4 py-2">
                        <StatusChip label={t("yard.waiting", "WAITING")} tone="warn" />
                        <span className="block text-[11px] text-muted-foreground">{h.reason}</span>
                      </td>
                      <td className="px-4 py-2 tabular-nums">
                        {h.yard_utilization_pct == null
                          ? "—"
                          : `${Number(h.yard_utilization_pct).toFixed(1)}%`}
                      </td>
                      <td className="px-4 py-2 text-xs">
                        {h.recommended_facility_name ?? (
                          <span className="text-severity-warning">
                            {t("yard.noParking", "No facility with capacity")}
                          </span>
                        )}
                        <span className="block text-[11px] text-muted-foreground">
                          {h.facility_available != null
                            ? t("yard.parkingFree", "{{n}} bays free", {
                                n: h.facility_available,
                              })
                            : ""}
                          {h.estimated_wait_min != null
                            ? ` · ${t("yard.wait", "~{{m}} min wait", { m: h.estimated_wait_min })}`
                            : ""}
                        </span>
                      </td>
                      <td className="px-4 py-2">
                        <span
                          className={
                            h.source === "pwa-registered"
                              ? "rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-primary"
                              : "rounded-full border border-border bg-muted px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground"
                          }
                        >
                          {h.source === "pwa-registered"
                            ? t("advisory.sourcePwaShort", "PWA")
                            : t("advisory.sourceSim", "Simulator")}
                        </span>
                        <span className="mt-0.5 block text-[10px] text-muted-foreground">
                          {h.notified
                            ? t("yard.notified", "driver notified")
                            : t("yard.notifyPending", "notify pending")}
                        </span>
                      </td>
                      <td className="px-4 py-2 text-right">
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={releaseOne.isPending}
                          onClick={() => releaseOne.mutate(h)}
                        >
                          {t("yard.actionReleaseOne", "Release")}
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        )}
      </Card>
    </div>
  );
}
