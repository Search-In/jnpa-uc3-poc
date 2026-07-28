// WeatherTile — live port-area weather + sea state from the Open-Meteo +
// OpenWeatherMap integrations (GET /api/weather/current). Built from the
// existing CarbonTile/TasWidget design (CollapsibleCard + Stat cells +
// provenance chips). The endpoint never fails for an upstream outage — it
// degrades LIVE → CACHED → SYNTHETIC and says so, which this tile surfaces via
// the StatusChip (LIVE/DEGRADED/OFFLINE) and DecisionPathBadge, so a synthetic
// number is never presented as a live one. The OpenWeather row (humidity /
// rain / cloud cover) only renders when the backend has an OPENWEATHER_API_KEY
// — without one the tile is exactly the original Open-Meteo layout.
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { CloudSun } from "lucide-react";
import { api } from "@/lib/api";
import { CollapsibleCard } from "@/components/ui/CollapsibleCard";
import { DecisionPathBadge } from "@/components/DecisionPathBadge";
import { Spinner, ErrorState } from "@/components/ui/misc";
import { StatusChip } from "@/components/ui/dtccc";
import { fmtTimeIST } from "@/lib/utils";
import {
  fmtMeasure,
  weatherCondition,
  weatherLabelTone,
  weatherSourceTone,
  weatherStatusTone,
} from "@/lib/weather";

// Refresh cadence matches the backend's CACHED-rung TTL granularity (600 s
// cache; polling every 120 s keeps the tile fresh without hammering the APIs).
const POLL_MS = 120_000;

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
  const ow = w?.openweather;

  return (
    <CollapsibleCard
      id="weather"
      title={
        <span className="inline-flex items-center gap-1.5">
          <CloudSun className="h-4 w-4 text-muted-foreground" />
          {t("panels.weather.title", "Port Weather & Sea State")}
        </span>
      }
      subtitle={t("panels.weather.subtitle", "Open-Meteo + OpenWeather · JNPA port area")}
      headerRight={
        w ? <StatusChip label={w.status} tone={weatherStatusTone(w.status)} /> : undefined
      }
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
          {/* Condition (OpenWeather label wins, Open-Meteo backs it up) + provenance */}
          <div className="flex items-center justify-between gap-2">
            <div className="inline-flex items-center gap-1.5 text-sm font-medium">
              {weatherCondition(w) ?? t("panels.weather.noCondition", "Conditions unavailable")}
              {ow?.label && ow.label !== "UNKNOWN" && (
                <StatusChip label={ow.label} tone={weatherLabelTone(ow.label)} />
              )}
            </div>
            <DecisionPathBadge path={w.decision_path} />
          </div>

          {/* Weather readings (Open-Meteo) */}
          <div className="grid grid-cols-3 gap-2">
            <Stat
              label={t("panels.weather.temperature", "Temp")}
              value={fmtMeasure(w.weather.temperature, "°C")}
            />
            <Stat
              label={t("panels.weather.windSpeed", "Wind")}
              value={fmtMeasure(w.weather.wind_speed, "km/h", 0)}
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

          {/* OpenWeather readings — only when the provider is configured */}
          {ow && (
            <>
              <div className="grid grid-cols-3 gap-2">
                <Stat
                  label={t("panels.weather.humidity", "Humidity")}
                  value={fmtMeasure(ow.humidity, "%", 0)}
                />
                <Stat label={t("panels.weather.rain", "Rain")} value={fmtMeasure(ow.rain, "mm")} />
                <Stat
                  label={t("panels.weather.clouds", "Cloud")}
                  value={fmtMeasure(ow.clouds, "%", 0)}
                />
              </div>
              {/* Cross-provider temperature validation — only flagged on disagreement */}
              {ow.temperature_consistent === false && (
                <div className="text-[10px] text-muted-foreground">
                  {t(
                    "panels.weather.tempMismatch",
                    "Providers disagree on temperature (Δ {{delta}} °C)",
                    {
                      delta: ow.temperature_delta?.toFixed(1) ?? "?",
                    },
                  )}
                </div>
              )}
            </>
          )}

          {/* Marine readings */}
          <div className="grid grid-cols-2 gap-2">
            <Stat
              label={t("panels.weather.waveHeight", "Wave height")}
              value={fmtMeasure(w.marine.wave_height, "m")}
            />
            <Stat
              label={t("panels.weather.wavePeriod", "Wave period")}
              value={fmtMeasure(w.marine.wave_period, "s", 0)}
            />
          </div>

          {/* Sources + staleness. cache_age_s is only set on the CACHED rung. */}
          <div className="flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
            <span className="inline-flex flex-wrap items-center gap-1.5">
              {t("panels.weather.source", "Sources")}
              {w.source.split("+").map((s) => (
                <StatusChip key={s} label={s} tone={weatherSourceTone(w.source)} />
              ))}
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
