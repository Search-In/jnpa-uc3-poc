// WeatherTile — live port-area weather + sea state from the Open-Meteo
// integration (GET /api/weather/current). Built from the existing
// CarbonTile/TasWidget design (CollapsibleCard + Stat cells + provenance
// chips). The endpoint never fails for an upstream outage — it degrades
// LIVE → CACHED → SYNTHETIC and says so, which this tile surfaces via the
// StatusChip (LIVE/DEGRADED/OFFLINE) and DecisionPathBadge, so a synthetic
// number is never presented as a live one.
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { CloudSun } from "lucide-react";
import { api } from "@/lib/api";
import type { WeatherCurrent } from "@/lib/types";
import { CollapsibleCard } from "@/components/ui/CollapsibleCard";
import { DecisionPathBadge } from "@/components/DecisionPathBadge";
import { Spinner, ErrorState } from "@/components/ui/misc";
import { StatusChip, type Tone } from "@/components/ui/dtccc";
import { fmtTimeIST } from "@/lib/utils";

// Refresh cadence matches the backend's CACHED-rung TTL granularity (600 s
// cache; polling every 120 s keeps the tile fresh without hammering Open-Meteo).
const POLL_MS = 120_000;

function statusTone(status?: WeatherCurrent["status"]): Tone {
  if (status === "LIVE") return "ok";
  if (status === "DEGRADED") return "warn";
  if (status === "OFFLINE") return "critical";
  return "neutral";
}

function fmt(value: number | null | undefined, unit: string, digits = 1): string {
  return value == null ? "—" : `${value.toFixed(digits)} ${unit}`;
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border bg-muted/40 p-2 text-center">
      <div className="text-sm font-semibold tabular-nums">{value}</div>
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
    </div>
  );
}

export function WeatherTile() {
  const { t } = useTranslation();
  const q = useQuery({
    queryKey: ["weather-current"],
    queryFn: () => api.weatherCurrent(),
    refetchInterval: POLL_MS,
  });
  const w = q.data;

  return (
    <CollapsibleCard
      id="weather"
      title={
        <span className="inline-flex items-center gap-1.5">
          <CloudSun className="h-4 w-4 text-muted-foreground" />
          {t("panels.weather.title", "Port Weather & Sea State")}
        </span>
      }
      subtitle={t("panels.weather.subtitle", "Open-Meteo · JNPA port area")}
      headerRight={w ? <StatusChip label={w.status} tone={statusTone(w.status)} /> : undefined}
      bodyClassName="space-y-3"
    >
      {q.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> {t("common.loading", "Loading…")}
        </div>
      ) : q.isError || !w ? (
        <ErrorState onRetry={() => void q.refetch()} />
      ) : (
        <>
          {/* Condition + provenance */}
          <div className="flex items-center justify-between gap-2">
            <div className="text-sm font-medium">
              {w.weather.condition ?? t("panels.weather.noCondition", "Conditions unavailable")}
            </div>
            <DecisionPathBadge path={w.decision_path} />
          </div>

          {/* Weather readings */}
          <div className="grid grid-cols-3 gap-2">
            <Stat
              label={t("panels.weather.temperature", "Temp")}
              value={fmt(w.weather.temperature, "°C")}
            />
            <Stat
              label={t("panels.weather.windSpeed", "Wind")}
              value={fmt(w.weather.wind_speed, "km/h", 0)}
            />
            <Stat
              label={t("panels.weather.visibility", "Visibility")}
              value={
                w.weather.visibility == null
                  ? "—"
                  : `${(w.weather.visibility / 1000).toFixed(1)} km`
              }
            />
          </div>

          {/* Marine readings */}
          <div className="grid grid-cols-2 gap-2">
            <Stat
              label={t("panels.weather.waveHeight", "Wave height")}
              value={fmt(w.marine.wave_height, "m")}
            />
            <Stat
              label={t("panels.weather.wavePeriod", "Wave period")}
              value={fmt(w.marine.wave_period, "s", 0)}
            />
          </div>

          {/* Source + staleness. cache_age_s is only set on the CACHED rung. */}
          <div className="flex items-center justify-between text-[10px] text-muted-foreground">
            <span className="inline-flex items-center gap-1.5">
              {t("panels.weather.source", "Source")}
              <StatusChip
                label={w.source}
                tone={w.source === "OPEN_METEO" ? "ok" : w.source === "SYNTHETIC" ? "info" : "warn"}
              />
            </span>
            <span className="tabular-nums">
              {w.cache_age_s != null
                ? t("panels.weather.cacheAge", "cached {{age}}s ago", {
                    age: Math.round(w.cache_age_s),
                  })
                : `${t("panels.weather.updated", "Updated")} ${fmtTimeIST(w.timestamp)}`}
            </span>
          </div>
        </>
      )}
    </CollapsibleCard>
  );
}

export default WeatherTile;
