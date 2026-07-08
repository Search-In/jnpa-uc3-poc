// Alerts Center — the single consolidated alert page (FINAL PHASE redesign).
//
// The brief calls for one page that categorises every active alert (Critical /
// Traffic / Parking / Geo-fence / Customs / AI / Vehicle) with a Today / 24h /
// 7d time filter. It reuses the existing alerts feed (WS-live ∪ adapter seed) and
// customs flags — no backend change. Clicking an alert focuses it on the live map.

import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, RefreshCw, ChevronRight } from "lucide-react";
import { getAdapter } from "@/data";
import { useSocket } from "@/hooks/SocketContext";
import { mergeAlerts, alertKey } from "@/lib/alerts";
import { alertFocusStore } from "@/lib/alertFocus";
import { severityColour, severityRank } from "@/lib/palette";
import { Card } from "@/components/ui/card";
import { LoadingState, ErrorState, EmptyState, LastUpdated } from "@/components/ui/misc";
import { relativeAge } from "@/lib/utils";
import { cn } from "@/lib/utils";
import {
  ALERT_CATEGORIES,
  TIME_RANGES,
  categoryOf,
  withinRange,
  type AlertCategory as Category,
  type TimeRange as Range,
} from "@/lib/alertCategory";
import type { Alert } from "@/lib/types";

const CATEGORIES = ALERT_CATEGORIES;
const RANGES = TIME_RANGES;

export default function AlertsCenter() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { alerts: liveAlerts } = useSocket();
  const [category, setCategory] = useState<Category>("all");
  const [range, setRange] = useState<Range>("7d");

  const seedQ = useQuery({
    queryKey: ["alerts-seed"],
    queryFn: () => getAdapter().alerts({ limit: 100 }),
    refetchInterval: 15_000,
  });
  const merged = useMemo(
    () => mergeAlerts(liveAlerts, seedQ.data ?? [], 200),
    [liveAlerts, seedQ.data],
  );

  const now = Date.now();
  const inRange = useMemo(
    () => merged.filter((a) => withinRange(a, range, now)),
    [merged, range, now],
  );

  // Per-category counts (within the selected time range).
  const counts = useMemo(() => {
    const c: Record<Category, number> = {
      all: inRange.length,
      critical: 0,
      traffic: 0,
      parking: 0,
      geofence: 0,
      customs: 0,
      ai: 0,
      vehicle: 0,
    };
    for (const a of inRange) c[categoryOf(a)]++;
    return c;
  }, [inRange]);

  const filtered = useMemo(() => {
    const list = category === "all" ? inRange : inRange.filter((a) => categoryOf(a) === category);
    return [...list].sort(
      (a, b) =>
        severityRank(b.severity) - severityRank(a.severity) || Date.parse(b.ts) - Date.parse(a.ts),
    );
  }, [inRange, category]);

  function focus(a: Alert) {
    alertFocusStore.focus(a);
    navigate("/live");
  }

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background">
      {/* Header */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border bg-card px-4 py-3">
        <div>
          <h1 className="text-lg font-bold tracking-tight text-foreground">
            {t("alertsCenter.title")}
          </h1>
          <p className="text-xs text-muted-foreground">{t("alertsCenter.subtitle")}</p>
        </div>
        <div className="ml-auto flex items-center gap-3">
          <LastUpdated at={seedQ.dataUpdatedAt || undefined} isFetching={seedQ.isFetching} />
          <button
            type="button"
            onClick={() => seedQ.refetch()}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-muted"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", seedQ.isFetching && "animate-spin")} />
            {t("commandCenter.refresh")}
          </button>
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border bg-card px-4 py-2.5">
        <div className="flex flex-wrap gap-1.5">
          {CATEGORIES.map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => setCategory(c)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors",
                category === c
                  ? "border-primary bg-primary text-primary-foreground"
                  : "border-border bg-background text-foreground hover:bg-muted",
              )}
            >
              {t(`alertsCenter.cat.${c}`)}
              <span
                className={cn(
                  "rounded-full px-1 text-[10px] font-bold tabular-nums",
                  category === c ? "bg-white/20" : "bg-muted",
                )}
              >
                {counts[c]}
              </span>
            </button>
          ))}
        </div>
        <div className="ml-auto flex gap-1 rounded-md border border-border bg-background p-0.5">
          {RANGES.map((r) => (
            <button
              key={r}
              type="button"
              onClick={() => setRange(r)}
              className={cn(
                "rounded px-2.5 py-1 text-xs font-medium transition-colors",
                range === r
                  ? "bg-primary text-primary-foreground"
                  : "text-foreground hover:bg-muted",
              )}
            >
              {t(`alertsCenter.range.${r}`)}
            </button>
          ))}
        </div>
      </div>

      {/* List */}
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {seedQ.isLoading ? (
          <LoadingState />
        ) : seedQ.isError ? (
          <ErrorState onRetry={() => seedQ.refetch()} detail={(seedQ.error as Error)?.message} />
        ) : filtered.length === 0 ? (
          <EmptyState>{t("alertsCenter.empty")}</EmptyState>
        ) : (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-3">
            {filtered.map((a, i) => (
              <AlertCard key={`${alertKey(a)}-${i}`} alert={a} onClick={() => focus(a)} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function AlertCard({ alert: a, onClick }: { alert: Alert; onClick: () => void }) {
  const { t } = useTranslation();
  const sev = severityColour(a.severity);
  const loc = a.gate_id ?? (a.payload?.zone_id as string) ?? "—";
  return (
    <Card
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onClick();
        }
      }}
      className="group relative cursor-pointer overflow-hidden p-3 pl-4 transition-all hover:-translate-y-0.5 hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
    >
      <span
        className="absolute inset-y-0 left-0 w-1"
        style={{ backgroundColor: sev }}
        aria-hidden
      />
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-1.5">
          <AlertTriangle className="h-4 w-4 shrink-0" style={{ color: sev }} aria-hidden />
          <span className="truncate text-sm font-semibold text-foreground">
            {t(`alertKind.${a.kind}`, { defaultValue: a.kind })}
          </span>
        </div>
        <span
          className="shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide"
          style={{ backgroundColor: `${sev}1f`, color: sev }}
        >
          {a.severity}
        </span>
      </div>
      <div className="mt-2 flex items-center justify-between text-[12px] text-muted-foreground">
        <span className="truncate">
          {a.plate ? (
            <span className="font-mono font-medium text-foreground">{a.plate}</span>
          ) : null}
          {a.plate ? " · " : ""}
          {loc}
        </span>
        <span className="shrink-0">{relativeAge(a.ts)}</span>
      </div>
      <div className="mt-1 flex items-center gap-0.5 text-[11px] font-semibold text-primary opacity-0 transition-opacity group-hover:opacity-100">
        {t("notifications.locate")}
        <ChevronRight className="h-3.5 w-3.5" />
      </div>
    </Card>
  );
}
