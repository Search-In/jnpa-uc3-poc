// TrafficTile — live NH-348 corridor traffic from the TomTom integration
// (GET /api/traffic/current). Built from the existing WeatherTile/CarbonTile/
// TasWidget design (CollapsibleCard + Stat cells + provenance chips). The
// endpoint never fails for a TomTom outage — it degrades LIVE → CACHED →
// DATABASE → SYNTHETIC and says so, which this tile surfaces via the
// StatusChip (LIVE/DEGRADED/OFFLINE) and DecisionPathBadge, so a synthetic
// number is never presented as a live one. The TomTom API key stays
// backend-only: this component only ever calls the gateway.
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { TrafficCone } from "lucide-react";
import { api } from "@/lib/api";
import { CollapsibleCard } from "@/components/ui/CollapsibleCard";
import { DecisionPathBadge } from "@/components/DecisionPathBadge";
import { Spinner, ErrorState } from "@/components/ui/misc";
import { StatusChip } from "@/components/ui/dtccc";
import { fmtTimeIST } from "@/lib/utils";
import {
  congestionTone,
  fmtDelay,
  fmtSpeed,
  incidentSeverityTone,
  speedRatioPct,
  trafficSourceTone,
  trafficStatusTone,
} from "@/lib/traffic";

// Refresh cadence matches the backend's CACHED-rung TTL granularity (120 s
// cache; polling every 60 s keeps the tile fresh without hammering the API).
const POLL_MS = 60_000;

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border bg-muted/40 p-2 text-center">
      <div className="text-sm font-semibold tabular-nums">{value}</div>
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
    </div>
  );
}

export function TrafficTile() {
  const { t } = useTranslation();
  const q = useQuery({
    queryKey: ["traffic-current"],
    queryFn: () => api.trafficCurrent(),
    refetchInterval: POLL_MS,
  });
  const d = q.data;
  const ratio = speedRatioPct(d);

  return (
    <CollapsibleCard
      id="traffic"
      title={
        <span className="inline-flex items-center gap-1.5">
          <TrafficCone className="h-4 w-4 text-muted-foreground" />
          {t("panels.traffic.title", "Corridor Traffic")}
        </span>
      }
      subtitle={t("panels.traffic.subtitle", "TomTom · NH-348 JNPA corridor")}
      headerRight={
        d ? <StatusChip label={d.status} tone={trafficStatusTone(d.status)} /> : undefined
      }
      bodyClassName="space-y-3"
    >
      {q.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> {t("common.loading", "Loading…")}
        </div>
      ) : q.isError || !d ? (
        <ErrorState onRetry={() => void q.refetch()} />
      ) : (
        <>
          {/* Congestion label + provenance */}
          <div className="flex items-center justify-between gap-2">
            <div className="inline-flex items-center gap-1.5 text-sm font-medium">
              {t("panels.traffic.congestion", "Congestion")}
              <StatusChip
                label={d.traffic.congestion_level}
                tone={congestionTone(d.traffic.congestion_level)}
              />
              {d.traffic.road_closure && (
                <StatusChip
                  label={t("panels.traffic.roadClosed", "ROAD CLOSED")}
                  tone="critical"
                />
              )}
            </div>
            <DecisionPathBadge path={d.decision_path} />
          </div>

          {/* Flow readings */}
          <div className="grid grid-cols-3 gap-2">
            <Stat
              label={t("panels.traffic.currentSpeed", "Current")}
              value={fmtSpeed(d.traffic.current_speed)}
            />
            <Stat
              label={t("panels.traffic.freeFlowSpeed", "Free flow")}
              value={fmtSpeed(d.traffic.free_flow_speed)}
            />
            <Stat
              label={t("panels.traffic.delay", "Delay")}
              value={fmtDelay(d.traffic.delay_seconds)}
            />
          </div>
          {ratio != null && (
            <div className="text-[10px] text-muted-foreground">
              {t("panels.traffic.speedRatio", "Traffic is moving at {{pct}}% of free-flow speed", {
                pct: ratio,
              })}
            </div>
          )}

          {/* Incidents — count plus the first few, severity-chipped */}
          <div className="space-y-1">
            <div className="flex items-center justify-between text-xs">
              <span className="font-medium">
                {t("panels.traffic.incidents", "Incidents on corridor")}
              </span>
              <span className="tabular-nums font-semibold">{d.incident_count}</span>
            </div>
            {d.incidents.slice(0, 3).map((inc, i) => (
              <div
                key={i}
                className="flex items-center justify-between gap-2 rounded-md border border-border bg-muted/40 px-2 py-1 text-[11px]"
              >
                <span className="truncate">
                  {inc.description ?? inc.type}
                  {inc.road ? ` · ${inc.road}` : ""}
                </span>
                <StatusChip label={inc.severity} tone={incidentSeverityTone(inc.severity)} />
              </div>
            ))}
            {d.incident_count === 0 && (
              <div className="text-[11px] text-muted-foreground">
                {t("panels.traffic.noIncidents", "No active incidents reported.")}
              </div>
            )}
          </div>

          {/* Source + staleness. cache_age_s is only set on fallback rungs. */}
          <div className="flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
            <span className="inline-flex items-center gap-1.5">
              {t("panels.traffic.source", "Source")}
              <StatusChip label={d.source} tone={trafficSourceTone(d.source)} />
            </span>
            <span className="tabular-nums">
              {d.cache_age_s != null
                ? t("panels.traffic.cacheAge", "cached {{age}}s ago", {
                    age: Math.round(d.cache_age_s),
                  })
                : `${t("panels.traffic.updated", "Updated")} ${fmtTimeIST(d.timestamp)}`}
            </span>
          </div>
        </>
      )}
    </CollapsibleCard>
  );
}

export default TrafficTile;
