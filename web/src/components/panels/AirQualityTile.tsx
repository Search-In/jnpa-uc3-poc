// AirQualityTile — live port air quality from the OpenAQ integration
// (GET /api/air-quality/current). Built from the existing WeatherTile/
// TrafficTile design (CollapsibleCard + Stat cells + provenance chips). The
// endpoint never fails for an OpenAQ outage — it degrades LIVE → CACHED →
// DATABASE → SYNTHETIC and says so, which this tile surfaces via the
// "STATUS • SOURCE" StatusChip and DecisionPathBadge, so a synthetic number
// is never presented as a live one. The browser only ever calls the gateway —
// never api.openaq.org.
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { Wind } from "lucide-react";
import { api } from "@/lib/api";
import { CollapsibleCard } from "@/components/ui/CollapsibleCard";
import { DecisionPathBadge } from "@/components/DecisionPathBadge";
import { Spinner, ErrorState } from "@/components/ui/misc";
import { StatusChip } from "@/components/ui/dtccc";
import { fmtTimeIST } from "@/lib/utils";
import {
  airQualitySourceTone,
  airQualityStatusTone,
  aqStatusTone,
  dominantPollutant,
  fmtConc,
} from "@/lib/air_quality";

// OpenAQ stations report hourly at best and the backend caches for 300 s —
// polling every 5 min keeps the tile fresh without hammering the API.
const POLL_MS = 300_000;

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border bg-muted/40 p-2 text-center">
      <div className="text-sm font-semibold tabular-nums">{value}</div>
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
    </div>
  );
}

export function AirQualityTile() {
  const { t } = useTranslation();
  const q = useQuery({
    queryKey: ["air-quality-current"],
    queryFn: () => api.airQualityCurrent(),
    refetchInterval: POLL_MS,
  });
  const d = q.data;
  const dominant = dominantPollutant(d);

  return (
    <CollapsibleCard
      id="air-quality"
      title={
        <span className="inline-flex items-center gap-1.5">
          <Wind className="h-4 w-4 text-muted-foreground" />
          {t("panels.airQuality.title", "Air Quality Intelligence")}
        </span>
      }
      subtitle={t("panels.airQuality.subtitle", "OpenAQ · JNPA port")}
      headerRight={
        d ? (
          <StatusChip label={`${d.status} • ${d.source}`} tone={airQualityStatusTone(d.status)} />
        ) : undefined
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
          {/* AQ status label + provenance */}
          <div className="flex items-center justify-between gap-2">
            <div className="inline-flex items-center gap-1.5 text-sm font-medium">
              {t("panels.airQuality.status", "Air quality")}
              <StatusChip
                label={d.air_quality.air_quality_status}
                tone={aqStatusTone(d.air_quality.air_quality_status)}
              />
            </div>
            <DecisionPathBadge path={d.decision_path} />
          </div>

          {/* Pollutant readings (µg/m³) */}
          <div className="grid grid-cols-3 gap-2">
            <Stat label="PM2.5" value={fmtConc(d.air_quality.pm25)} />
            <Stat label="PM10" value={fmtConc(d.air_quality.pm10)} />
            <Stat label="NO₂" value={fmtConc(d.air_quality.no2)} />
          </div>
          {dominant != null && (
            <div className="text-[10px] text-muted-foreground">
              {t("panels.airQuality.dominant", "Dominant pollutant: {{pollutant}}", {
                pollutant: dominant,
              })}
            </div>
          )}

          {/* Source + staleness. cache_age_s is only set on fallback rungs. */}
          <div className="flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
            <span className="inline-flex items-center gap-1.5">
              {t("panels.airQuality.source", "Source")}
              <StatusChip label={d.source} tone={airQualitySourceTone(d.source)} />
            </span>
            <span className="tabular-nums">
              {d.cache_age_s != null
                ? t("panels.airQuality.cacheAge", "cached {{age}}s ago", {
                    age: Math.round(d.cache_age_s),
                  })
                : d.air_quality.observed_at
                  ? `${t("panels.airQuality.observed", "Observed")} ${fmtTimeIST(d.air_quality.observed_at)}`
                  : `${t("panels.airQuality.updated", "Updated")} ${fmtTimeIST(d.timestamp)}`}
            </span>
          </div>
        </>
      )}
    </CollapsibleCard>
  );
}

export default AirQualityTile;
