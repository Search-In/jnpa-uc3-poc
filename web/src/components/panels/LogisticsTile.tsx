// LogisticsTile — ULIP logistics intelligence for the corridor
// (GET /api/logistics/current). Built from the existing AirQualityTile/
// TrafficTile design (CollapsibleCard + Stat cells + provenance chips). The
// endpoint never fails for a ULIP outage — it degrades LIVE → CACHED →
// DATABASE → FALLBACK and says so, which this tile surfaces via the
// "STATUS • SOURCE" StatusChip and DecisionPathBadge. The FALLBACK rung is
// explicitly EMPTY (data_available: false) — an "awaiting ULIP data" note is
// shown instead of fabricated shipments. The browser only ever calls the
// gateway — never the ULIP platform.
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { Truck } from "lucide-react";
import { api } from "@/lib/api";
import { CollapsibleCard } from "@/components/ui/CollapsibleCard";
import { DecisionPathBadge } from "@/components/DecisionPathBadge";
import { Spinner, ErrorState } from "@/components/ui/misc";
import { StatusChip } from "@/components/ui/dtccc";
import { fmtTimeIST } from "@/lib/utils";
import { eventCaption, fmtCount, logisticsSourceTone, logisticsStatusTone } from "@/lib/logistics";

// ULIP source systems batch their feeds (toll crossings arrive minutes after
// the fact) and the backend caches the summary — polling every 2 min keeps
// the tile fresh without hammering the platform call budget.
const POLL_MS = 120_000;

// Latest events shown inline (full history lives on /api/logistics/events).
const EVENTS_SHOWN = 4;

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border bg-muted/40 p-2 text-center">
      <div className="text-sm font-semibold tabular-nums">{value}</div>
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
    </div>
  );
}

export function LogisticsTile() {
  const { t } = useTranslation();
  const q = useQuery({
    queryKey: ["logistics-current"],
    queryFn: () => api.logisticsCurrent(),
    refetchInterval: POLL_MS,
  });
  const d = q.data;
  const block = d?.logistics;

  return (
    <CollapsibleCard
      id="logistics"
      title={
        <span className="inline-flex items-center gap-1.5">
          <Truck className="h-4 w-4 text-muted-foreground" />
          {t("panels.logistics.title", "Logistics Intelligence")}
        </span>
      }
      subtitle={t("panels.logistics.subtitle", "ULIP · NH-348 JNPA corridor")}
      headerRight={
        d ? (
          <StatusChip label={`${d.status} • ${d.source}`} tone={logisticsStatusTone(d.status)} />
        ) : undefined
      }
      bodyClassName="space-y-3"
    >
      {q.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> {t("common.loading", "Loading…")}
        </div>
      ) : q.isError || !d || !block ? (
        <ErrorState onRetry={() => void q.refetch()} />
      ) : (
        <>
          {/* Movement volumes (last 24 h) + provenance */}
          <div className="flex items-center justify-between gap-2">
            <div className="text-sm font-medium">
              {t("panels.logistics.window", "Movements · last {{hours}} h", {
                hours: block.window_h,
              })}
            </div>
            <DecisionPathBadge path={d.decision_path} />
          </div>
          <div className="grid grid-cols-3 gap-2">
            <Stat
              label={t("panels.logistics.events", "Events")}
              value={fmtCount(block.event_count)}
            />
            <Stat
              label={t("panels.logistics.vehicles", "Vehicles")}
              value={fmtCount(block.vehicle_count)}
            />
            <Stat
              label={t("panels.logistics.containers", "Containers")}
              value={fmtCount(block.container_count)}
            />
          </div>

          {/* Latest logistics events (toll crossings / container movements) */}
          {block.latest_events.length > 0 ? (
            <ul className="space-y-1">
              {block.latest_events.slice(0, EVENTS_SHOWN).map((ev, i) => (
                <li
                  key={`${ev.ref_id}-${ev.event_ts ?? i}`}
                  className="flex items-center justify-between gap-2 text-[11px]"
                >
                  <span className="truncate">{eventCaption(ev)}</span>
                  <span className="shrink-0 tabular-nums text-muted-foreground">
                    {ev.event_ts ? fmtTimeIST(ev.event_ts) : "—"}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-[11px] text-muted-foreground">
              {block.data_available
                ? t("panels.logistics.noRecent", "No movements in the window.")
                : t(
                    "panels.logistics.awaiting",
                    "Awaiting ULIP data — no shipment data is fabricated.",
                  )}
            </div>
          )}

          {/* Source + staleness. cache_age_s is only set on fallback rungs. */}
          <div className="flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
            <span className="inline-flex items-center gap-1.5">
              {t("panels.logistics.source", "Source")}
              <StatusChip label={d.source} tone={logisticsSourceTone(d.source)} />
            </span>
            <span className="tabular-nums">
              {d.cache_age_s != null
                ? t("panels.logistics.cacheAge", "cached {{age}}s ago", {
                    age: Math.round(d.cache_age_s),
                  })
                : block.last_event_ts
                  ? `${t("panels.logistics.lastEvent", "Last event")} ${fmtTimeIST(block.last_event_ts)}`
                  : `${t("panels.logistics.updated", "Updated")} ${fmtTimeIST(d.timestamp)}`}
            </span>
          </div>
        </>
      )}
    </CollapsibleCard>
  );
}

export default LogisticsTile;
