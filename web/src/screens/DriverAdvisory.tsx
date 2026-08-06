import { useState } from "react";
import { useTranslation } from "react-i18next";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getAdapter } from "@/data";
import { api } from "@/lib/api";
import type { TruckDevice } from "@/lib/types";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Spinner, EmptyState, ErrorState } from "@/components/ui/misc";
import {
  PageContainer,
  PageHeader,
  RefreshButton,
  StatGrid,
  StatCard,
  StatusChip,
} from "@/components/ui/dtccc";
import { DecisionPathBadge } from "@/components/DecisionPathBadge";
import { fmtEta } from "@/lib/utils";
import { gateIdColour } from "@/lib/tokens";
import { weatherCondition, weatherHumidityPct, weatherRainMm } from "@/lib/weather";
import {
  congestionTone,
  fmtDelay,
  fmtSpeed,
  incidentSeverityTone,
  trafficStatusTone,
} from "@/lib/traffic";
import {
  Navigation,
  CheckCircle2,
  AlertCircle,
  Route,
  DoorOpen,
  AlertTriangle,
  CloudRain,
  TrafficCone,
} from "lucide-react";

const GATES = ["G-NSICT", "G-JNPCT", "G-NSIGT", "G-BMCT"];

// Free-flow highway speed (km/h) used as a client-side safety net when the
// truck-sim payload lacks `eta_s`. The backend now always supplies one (seeded
// at inject + a serializer fallback), so this rarely triggers; the value mirrors
// the backend's speed_highway_kmh so the estimate stays consistent if it does.
const FREE_FLOW_KMH = 55;

// ETA-to-gate in seconds: prefer the live `eta_s`; otherwise fall back to the
// remaining distance at free-flow speed (0 km remaining -> ~0 s -> "<1 min").
function etaSeconds(truck: TruckDevice): number {
  if (truck.eta_s != null) return truck.eta_s;
  return (truck.remaining_km / FREE_FLOW_KMH) * 3600;
}

// Trucks AT_GATE_QUEUE with ETA-to-gate and a re-routing recommendation. The
// recommendation picks the least-loaded alternative gate; "Push Re-route" forces
// it via POST /api/trucks/{id}/route (used in the TFC-3 scenario).
export default function DriverAdvisory() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const queued = useQuery({
    queryKey: ["trucks", "AT_GATE_QUEUE", "advisory"],
    queryFn: () => getAdapter().trucks("AT_GATE_QUEUE", 500),
    // Serve the last queue instantly on remount / tab return instead of a fresh
    // spinner-guarded fetch: the gateway memoises this list for a few seconds
    // anyway, so a sub-10 s refetch cannot say anything new. On refetch the
    // previous rows stay rendered (no table flash) while the update runs.
    staleTime: 10_000,
    placeholderData: keepPreviousData,
  });

  const devices = queued.data ?? [];

  // --- Accident Route Advisory (additive) ---------------------------------
  // Reuse the existing accidents API to surface ACTIVE (REPORTED /
  // INVESTIGATING) accidents as route hazards. No new data is fabricated.
  // Secondary panels: none of these gate the rerouting table — each renders its
  // own loading state and lands whenever it lands. staleTime keeps a remount
  // (tab switch, navigation) from re-firing all four calls within the window.
  const accReported = useQuery({
    queryKey: ["accidents", "REPORTED", "advisory"],
    queryFn: () => api.accidents({ status: "REPORTED", limit: 20 }),
    staleTime: 30_000,
  });
  const accInvestigating = useQuery({
    queryKey: ["accidents", "INVESTIGATING", "advisory"],
    queryFn: () => api.accidents({ status: "INVESTIGATING", limit: 20 }),
    staleTime: 30_000,
  });
  const activeAccidents = [
    ...(accReported.data?.accidents ?? []),
    ...(accInvestigating.data?.accidents ?? []),
  ];
  const accidentsLoading = accReported.isLoading || accInvestigating.isLoading;

  // --- Weather Advisory (additive) ----------------------------------------
  // Live port-area conditions from the Open-Meteo integration
  // (GET /api/weather/current). The endpoint degrades LIVE → CACHED →
  // SYNTHETIC instead of failing; the panel always shows which rung served
  // the data so a synthetic reading is never presented as live.
  const weather = useQuery({
    queryKey: ["weather-current", "advisory"],
    queryFn: () => api.weatherCurrent(),
    staleTime: 60_000,
  });

  // --- Traffic Advisory (additive) ----------------------------------------
  // Live corridor conditions from the TomTom integration
  // (GET /api/traffic/current) — real flow + incident data replacing any
  // static traffic placeholder. Degrades LIVE → CACHED → DATABASE → SYNTHETIC
  // instead of failing; the panel shows which rung served the data.
  const traffic = useQuery({
    queryKey: ["traffic-current", "advisory"],
    queryFn: () => api.trafficCurrent(),
    staleTime: 60_000,
  });

  // Queue depth per gate -> the recommendation steers toward the shortest queue.
  const depth = new Map<string, number>();
  for (const t of devices) if (t.gate_id) depth.set(t.gate_id, (depth.get(t.gate_id) ?? 0) + 1);
  const recommendFor = (current?: string | null) => {
    const ranked = GATES.filter((g) => g !== current).sort(
      (a, b) => (depth.get(a) ?? 0) - (depth.get(b) ?? 0),
    );
    return ranked[0];
  };
  const busiest = GATES.reduce(
    (a, b) => ((depth.get(b) ?? 0) > (depth.get(a) ?? 0) ? b : a),
    GATES[0],
  );

  return (
    <PageContainer>
      <PageHeader
        icon={Route}
        title={t("nav.advisory")}
        subtitle={`${t("advisory.subtitlePrefix")} AT_GATE_QUEUE · ${t("advisory.subtitleSuffix")}`}
        updatedAt={queued.dataUpdatedAt}
        isFetching={queued.isFetching && !queued.isLoading}
        onRefresh={() =>
          qc.invalidateQueries({ queryKey: ["trucks", "AT_GATE_QUEUE", "advisory"] })
        }
      />

      <div className="px-4 pt-3">
        <StatGrid className="lg:grid-cols-5">
          <StatCard
            icon={DoorOpen}
            label={t("advisory.queuedTrucks")}
            value={devices.length}
            tone={devices.length > 40 ? "warn" : "info"}
            loading={queued.isLoading}
          />
          {GATES.map((g) => (
            <StatCard
              key={g}
              label={g.replace("G-", "")}
              value={depth.get(g) ?? 0}
              tone={g === busiest && (depth.get(g) ?? 0) > 0 ? "warn" : "ok"}
              sub={g === busiest && (depth.get(g) ?? 0) > 0 ? "busiest" : "queue depth"}
              loading={queued.isLoading}
            />
          ))}
        </StatGrid>
      </div>

      <div className="px-4 py-3">
        <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-foreground">
          <Route className="h-4 w-4 text-muted-foreground" />
          {t("advisory.congestionRerouting", "Congestion Rerouting")}
          {/* Table-level re-fetch: this query only, no page reload; the rendered
              rows stay put while it runs (keepPreviousData above). */}
          <RefreshButton
            onRefresh={() => void queued.refetch()}
            isRefreshing={queued.isFetching}
            className="ml-auto"
          />
        </div>
        {queued.isError && devices.length === 0 ? (
          <Card>
            <ErrorState
              onRetry={() => void queued.refetch()}
              detail={(queued.error as Error)?.message}
            />
          </Card>
        ) : queued.isLoading ? (
          <QueueSkeleton />
        ) : devices.length === 0 ? (
          <Card>
            <EmptyState>{t("advisory.emptyQueue")}</EmptyState>
          </Card>
        ) : (
          <Card data-guided-id="advisory-queue" className="overflow-hidden">
            <CardContent className="p-0">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="border-b border-border bg-muted/60 text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                    <tr>
                      <th className="px-4 py-2">{t("advisory.colDevice")}</th>
                      <th className="px-4 py-2">{t("advisory.colPlate")}</th>
                      <th className="px-4 py-2">{t("advisory.colGate")}</th>
                      <th className="px-4 py-2">{t("advisory.colEta")}</th>
                      <th className="px-4 py-2">{t("advisory.colRemaining")}</th>
                      <th className="px-4 py-2">{t("advisory.colRecommend")}</th>
                      <th className="px-4 py-2 text-right">{t("advisory.colAction")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {devices.slice(0, 200).map((t) => (
                      <QueueRow
                        key={t.device_id}
                        truck={t}
                        recommend={recommendFor(t.gate_id)}
                        qc={qc}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            </CardContent>
          </Card>
        )}
      </div>

      {/* Accident Route Advisory (additive) */}
      <div className="px-4 py-3">
        <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-foreground">
          <AlertTriangle className="h-4 w-4 text-severity-crit" />
          {t("advisory.accidentAdvisory", "Accident Route Advisory")}
        </div>
        <Card>
          <CardContent className="p-0">
            {accidentsLoading ? (
              <div className="flex items-center gap-2 p-6 text-sm text-muted-foreground">
                <Spinner /> {t("advisory.loadingAccidents", "Checking active accidents…")}
              </div>
            ) : activeAccidents.length === 0 ? (
              <EmptyState>
                {t("advisory.emptyAccidents", "No active accidents on the corridor.")}
              </EmptyState>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="border-b border-border bg-muted/60 text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                    <tr>
                      <th className="px-4 py-2">{t("advisory.colRef", "Ref")}</th>
                      <th className="px-4 py-2">{t("advisory.colSeverity", "Severity")}</th>
                      <th className="px-4 py-2">
                        {t("advisory.colLocation", "Location / Segment")}
                      </th>
                      <th className="px-4 py-2">{t("advisory.colPlate")}</th>
                      <th className="px-4 py-2">{t("advisory.colAdvisory", "Advisory")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {activeAccidents.map((a: any) => (
                      <tr
                        key={a.id ?? a.accident_ref}
                        className="border-b border-border/50 hover:bg-muted/40"
                      >
                        <td className="px-4 py-2 font-mono text-xs">
                          {a.accident_ref ?? a.id ?? "—"}
                        </td>
                        <td className="px-4 py-2">{a.severity ?? "—"}</td>
                        <td className="px-4 py-2">{accidentLocation(a)}</td>
                        <td className="px-4 py-2 font-mono text-xs">{a.plate ?? "—"}</td>
                        <td className="px-4 py-2 text-severity-crit">
                          {t("advisory.avoidSegment", "Avoid affected corridor segment")}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Traffic Advisory — live TomTom corridor conditions with provenance */}
      <div className="px-4 py-3">
        <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-foreground">
          <TrafficCone className="h-4 w-4 text-muted-foreground" />
          {t("advisory.trafficAdvisory", "Traffic Advisory")}
        </div>
        <Card>
          <CardContent className="space-y-2 p-4 text-sm">
            {traffic.isLoading ? (
              <div className="flex items-center gap-2 text-muted-foreground">
                <Spinner /> {t("advisory.loadingTraffic", "Loading corridor traffic…")}
              </div>
            ) : traffic.isError || !traffic.data ? (
              <ErrorState onRetry={() => void traffic.refetch()} />
            ) : (
              <>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="inline-flex items-center gap-1.5 font-medium">
                    {t("advisory.trafficCongestion", "Corridor congestion")}
                    <StatusChip
                      label={traffic.data.traffic.congestion_level}
                      tone={congestionTone(traffic.data.traffic.congestion_level)}
                    />
                  </span>
                  <span className="inline-flex items-center gap-1.5">
                    <StatusChip
                      label={traffic.data.status}
                      tone={trafficStatusTone(traffic.data.status)}
                    />
                    <DecisionPathBadge path={traffic.data.decision_path} />
                  </span>
                </div>
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs tabular-nums text-foreground">
                  <span>
                    {t("advisory.trafficSpeed", "Speed")}:{" "}
                    {fmtSpeed(traffic.data.traffic.current_speed)}
                  </span>
                  <span>
                    {t("advisory.trafficFreeFlow", "Free flow")}:{" "}
                    {fmtSpeed(traffic.data.traffic.free_flow_speed)}
                  </span>
                  <span>
                    {t("advisory.trafficDelay", "Delay")}:{" "}
                    {fmtDelay(traffic.data.traffic.delay_seconds)}
                  </span>
                  <span>
                    {t("advisory.trafficIncidents", "Incidents")}: {traffic.data.incident_count}
                  </span>
                </div>
                {traffic.data.incidents.slice(0, 3).map((inc, i) => (
                  <div
                    key={i}
                    className="flex items-center justify-between gap-2 rounded-md border border-border bg-muted/40 px-2 py-1 text-xs"
                  >
                    <span className="truncate">
                      {inc.description ?? inc.type}
                      {inc.road ? ` · ${inc.road}` : ""}
                    </span>
                    <StatusChip label={inc.severity} tone={incidentSeverityTone(inc.severity)} />
                  </div>
                ))}
                <p className="text-xs text-muted-foreground">
                  {t(
                    "advisory.trafficCaption",
                    "Live TomTom feed for the NH-348 JNPA corridor; when the feed is unreachable the last cached reading is shown and labelled.",
                  )}
                </p>
              </>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Weather Advisory — live Open-Meteo conditions with provenance */}
      <div className="px-4 py-3">
        <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-foreground">
          <CloudRain className="h-4 w-4 text-muted-foreground" />
          {t("advisory.weatherAdvisory", "Weather Advisory")}
        </div>
        <Card>
          <CardContent className="space-y-2 p-4 text-sm">
            {weather.isLoading ? (
              <div className="flex items-center gap-2 text-muted-foreground">
                <Spinner /> {t("advisory.loadingWeather", "Loading port weather…")}
              </div>
            ) : weather.isError || !weather.data ? (
              <ErrorState onRetry={() => void weather.refetch()} />
            ) : (
              <>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-medium">
                    {weatherCondition(weather.data) ??
                      t("advisory.weatherNoCondition", "Conditions unavailable")}
                  </span>
                  <span className="inline-flex items-center gap-1.5">
                    <StatusChip
                      label={weather.data.status}
                      tone={
                        weather.data.status === "LIVE"
                          ? "ok"
                          : weather.data.status === "DEGRADED"
                            ? "warn"
                            : "critical"
                      }
                    />
                    <DecisionPathBadge path={weather.data.decision_path} />
                  </span>
                </div>
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs tabular-nums text-foreground">
                  <span>
                    {t("advisory.weatherTemp", "Temp")}:{" "}
                    {weather.data.weather.temperature != null
                      ? `${weather.data.weather.temperature.toFixed(1)} °C`
                      : "—"}
                  </span>
                  <span>
                    {t("advisory.weatherWind", "Wind")}:{" "}
                    {weather.data.weather.wind_speed != null
                      ? `${weather.data.weather.wind_speed.toFixed(0)} km/h`
                      : "—"}
                  </span>
                  <span>
                    {t("advisory.weatherVisibility", "Visibility")}:{" "}
                    {weather.data.weather.visibility != null
                      ? `${(weather.data.weather.visibility / 1000).toFixed(1)} km`
                      : "—"}
                  </span>
                  <span>
                    {t("advisory.weatherRain", "Rain")}:{" "}
                    {weatherRainMm(weather.data) != null
                      ? `${weatherRainMm(weather.data)!.toFixed(1)} mm`
                      : "—"}
                  </span>
                  {weatherHumidityPct(weather.data) != null && (
                    <span>
                      {t("advisory.weatherHumidity", "Humidity")}:{" "}
                      {`${weatherHumidityPct(weather.data)!.toFixed(0)} %`}
                    </span>
                  )}
                  <span>
                    {t("advisory.weatherWave", "Waves")}:{" "}
                    {weather.data.marine.wave_height != null
                      ? `${weather.data.marine.wave_height.toFixed(1)} m`
                      : "—"}
                  </span>
                </div>
                <p className="text-xs text-muted-foreground">
                  {t(
                    "advisory.weatherCaption",
                    "Live Open-Meteo + OpenWeather feed for the JNPA port area; when a feed is unreachable the last cached reading is shown and labelled.",
                  )}
                </p>
              </>
            )}
          </CardContent>
        </Card>
      </div>
    </PageContainer>
  );
}

// First-load placeholder for the rerouting table: the real column layout with
// shimmering cells, so the page keeps its shape while the queue arrives (and
// nothing else on the page waits for it — each panel loads independently).
function QueueSkeleton() {
  return (
    <Card className="overflow-hidden" aria-busy="true">
      <CardContent className="p-0">
        <div className="animate-pulse divide-y divide-border/50">
          <div className="h-9 bg-muted/60" />
          {Array.from({ length: 6 }, (_, i) => (
            <div key={i} className="flex items-center gap-4 px-4 py-3">
              {[24, 20, 12, 10, 10, 28, 20].map((w, j) => (
                <div key={j} className="h-3.5 rounded bg-muted" style={{ width: `${w * 4}px` }} />
              ))}
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

// Best-effort human label for an accident's location. `location` may be a JSON
// object (parsed by the API) or a plain string; fall back to accident_type.
function accidentLocation(a: any): string {
  const loc = a?.location;
  if (typeof loc === "string" && loc) return loc;
  if (loc && typeof loc === "object") {
    return loc.name ?? loc.segment ?? loc.detail ?? loc.corridor ?? a?.accident_type ?? "—";
  }
  return a?.accident_type ?? "—";
}

/**
 * A gate name tinted with its shared identity colour (`gateIdColour`, defined
 * once in lib/palette.ts alongside the other Okabe–Ito ramps). Styling matches
 * the app's existing StatusChip idiom exactly — a 12%-alpha tint of the hue
 * behind solid, semibold text — so the chips read as part of the current theme
 * rather than as a new visual language.
 *
 * Presentation only: it renders whatever gate string it is handed and carries no
 * state, no handlers and no routing logic. Because Radix portals a SelectItem's
 * <ItemText> into the trigger's <SelectValue>, using this inside SelectItem
 * colours BOTH the dropdown option and the selected value from one definition,
 * which is what keeps the two permanently in sync.
 */
function GateChip({ gate, arrow = false }: { gate: string; arrow?: boolean }) {
  const colour = gateIdColour(gate);
  return (
    <span
      className="inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-semibold"
      style={{ backgroundColor: `${colour}1f`, color: colour }}
    >
      {arrow ? "→ " : ""}
      {gate.replace("G-", "")}
    </span>
  );
}

function QueueRow({
  truck,
  recommend,
  qc,
}: {
  truck: TruckDevice;
  recommend: string;
  qc: ReturnType<typeof useQueryClient>;
}) {
  const { t } = useTranslation();
  // The dropdown shows the suggested gate by default, but stays editable. Once
  // the operator picks a gate we keep their choice (`selected`) regardless of
  // how the auto-recommendation shifts as the queue rebalances.
  const [selected, setSelected] = useState<string | null>(null);
  const gate = selected ?? recommend;
  const [done, setDone] = useState(false);

  const reroute = useMutation({
    mutationFn: (gateId: string) =>
      getAdapter().reroute(truck.device_id, {
        gate_id: gateId,
        force_state: "EN_ROUTE_TO_PORT",
      }),
    onSuccess: async () => {
      setDone(true);
      // Refetch so the Gate column reflects the persisted change immediately.
      await qc.invalidateQueries({ queryKey: ["trucks"] });
    },
  });

  const onGateChange = (gateId: string) => {
    setSelected(gateId);
    setDone(false);
    reroute.mutate(gateId);
  };

  return (
    <tr className="border-b border-border/50 hover:bg-muted/40">
      <td className="px-4 py-2 font-mono text-xs">{truck.device_id}</td>
      <td className="px-4 py-2 font-mono text-xs">{truck.plate ?? "—"}</td>
      <td className="px-4 py-2">{truck.gate_id ? <GateChip gate={truck.gate_id} /> : "—"}</td>
      <td className="px-4 py-2 tabular-nums">{fmtEta(etaSeconds(truck))}</td>
      <td className="px-4 py-2 tabular-nums">{truck.remaining_km.toFixed(1)} km</td>
      <td className="px-4 py-2">
        <Select value={gate} onValueChange={onGateChange} disabled={reroute.isPending}>
          <SelectTrigger
            className="w-[140px]"
            data-guided-id="advisory-reroute"
            aria-label={t("advisory.selectGateAria", { device: truck.device_id })}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {GATES.map((g) => (
              <SelectItem key={g} value={g}>
                <GateChip gate={g} arrow />
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </td>
      <td className="px-4 py-2 text-right">
        {reroute.isPending ? (
          <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
            <Spinner /> {t("advisory.saving")}
          </span>
        ) : reroute.isError ? (
          <button
            type="button"
            className="inline-flex items-center gap-1 text-xs text-severity-crit"
            onClick={() => reroute.mutate(gate)}
          >
            <AlertCircle className="h-3.5 w-3.5" /> {t("common.retry")}
          </button>
        ) : done ? (
          <span className="inline-flex items-center gap-1 text-xs text-severity-ok">
            <CheckCircle2 className="h-3.5 w-3.5" /> {t("advisory.gateUpdated")}
          </span>
        ) : (
          <Button
            size="sm"
            variant="outline"
            onClick={() => reroute.mutate(gate)}
            disabled={reroute.isPending}
          >
            <Navigation className="h-3.5 w-3.5" />
            {t("advisory.pushReroute")}
          </Button>
        )}
      </td>
    </tr>
  );
}
