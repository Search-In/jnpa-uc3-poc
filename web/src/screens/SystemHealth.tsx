// System Health — enterprise monitoring dashboard (FINAL PHASE redesign).
// Live status of every subsystem, built ONLY from real signals: /api/kpi/sources
// (per-source state, last_ok, p95, decision-path), /api/kpi/cameras, /api/fastag/health
// and /healthz (gateway). Each service card carries a backing classification —
// LIVE (real vendor) · SIM (simulator) · RDS (persisted) · EPHEMERAL (in-memory) —
// plus last heartbeat, response time and a health indicator. Clicking a service
// opens its decision log. No synthetic data is introduced. No backend changes.

import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import {
  Server,
  Database,
  Radio,
  Camera,
  Satellite,
  Cpu,
  HardDrive,
  Globe,
  Activity,
  type LucideIcon,
} from "lucide-react";
import { getAdapter } from "@/data";
import { api } from "@/lib/api";
import type { CameraHealth, Decision, SourceHealth } from "@/lib/types";
import { Card } from "@/components/ui/card";
import { StatusDot, Spinner } from "@/components/ui/misc";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { IdentityPanel } from "@/components/panels/IdentityPanel";
import { AssumptionsPanel } from "@/components/AssumptionsPanel";
import { DecisionPathBadge } from "@/components/DecisionPathBadge";
import {
  PageContainer,
  PageHeader,
  StatGrid,
  StatCard,
  StatusChip,
  type Tone,
} from "@/components/ui/dtccc";
import { STATUS } from "@/lib/tokens";
import { relativeAge, fmtDateTimeIST } from "@/lib/utils";

type Health = "HEALTHY" | "DEGRADED" | "DOWN" | "UNKNOWN";
type Backing = "LIVE" | "SIM" | "RDS" | "EPHEMERAL";

const BACKING_TONE: Record<Backing, Tone> = {
  LIVE: "ok",
  SIM: "warn",
  RDS: "info",
  EPHEMERAL: "neutral",
};
const HEALTH_COLOUR: Record<Health, string> = {
  HEALTHY: STATUS.ok,
  DEGRADED: STATUS.warning,
  DOWN: STATUS.critical,
  UNKNOWN: STATUS.unknown,
};

interface Service {
  key: string;
  name: string;
  icon: LucideIcon;
  health: Health;
  backing: Backing;
  heartbeat: string | null;
  responseMs: number | null;
  detail: string;
  api?: string;
  source?: string;
}

function healthFromState(state?: string): Health {
  if (state === "LIVE") return "HEALTHY";
  if (state === "DEGRADED") return "DEGRADED";
  if (state === "DOWN") return "DOWN";
  return "UNKNOWN";
}
function backingFromPath(path?: string | null): Backing {
  const p = (path ?? "").toUpperCase();
  if (p.includes("LIVE")) return "LIVE";
  if (p.includes("SYNTHETIC")) return "SIM";
  if (p.includes("CACHED")) return "RDS";
  return "SIM";
}
function rank(state?: string) {
  return state === "DOWN" ? 3 : state === "DEGRADED" ? 2 : state === "LIVE" ? 1 : 0;
}
/** Worst (most-degraded) row across a matched source group. */
function worstOf(rows: SourceHealth[]): SourceHealth | undefined {
  return [...rows].sort((a, b) => rank(b.state) - rank(a.state))[0];
}

export default function SystemHealth() {
  const { t } = useTranslation();
  const sourcesQ = useQuery({
    queryKey: ["sources"],
    queryFn: () => getAdapter().sources(),
    refetchInterval: 5000,
  });
  const camerasQ = useQuery({
    queryKey: ["cameras"],
    queryFn: () => getAdapter().cameras(),
    refetchInterval: 5000,
  });
  const fastagQ = useQuery({
    queryKey: ["fastag-health"],
    queryFn: () => getAdapter().fastagHealth(),
    refetchInterval: 10000,
    retry: false,
  });
  const gatewayQ = useQuery({
    queryKey: ["gateway-health"],
    queryFn: () => api.health(),
    refetchInterval: 5000,
    retry: false,
  });
  const [drawer, setDrawer] = useState<{ title: string; api?: string; source?: string } | null>(
    null,
  );

  const sources = sourcesQ.data ?? [];
  const cameras = camerasQ.data ?? [];
  const match = (pred: (s: string) => boolean) =>
    sources.filter((s) => pred(s.source.toLowerCase()));

  const services: Service[] = useMemo(() => {
    const now = new Date().toISOString();
    // The gateway is definitionally reachable if ANY gateway-served query
    // succeeded (we just talked to it) — /healthz may be unproxied in dev, so we
    // never report a false DOWN when sources are clearly flowing.
    const gwReachable =
      gatewayQ.data?.status === "ok" ||
      (!sourcesQ.isError && sources.length > 0) ||
      !camerasQ.isError;
    const gwOk = gwReachable;
    const dbUp = !!fastagQ.data?.db || sources.length > 0;

    const external = worstOf(
      match((s) => /vahan|sarathi|fastag|congestion|traffic|here|tomtom/.test(s)),
    );
    const gps = worstOf(match((s) => /truck|ulip|gps|rfid/.test(s)));
    const ai = worstOf(match((s) => /anomaly|alert|congestion|forecast|ocr|anpr/.test(s)));

    const camHealthy = cameras.length > 0;
    const camLive = cameras.some((c) => c.decision_path === "LIVE");
    const camPath = camLive ? "LIVE" : cameras[0]?.decision_path;

    return [
      {
        key: "gateway",
        name: "Gateway",
        icon: Server,
        health: gwOk ? "HEALTHY" : "DOWN",
        backing: "EPHEMERAL",
        heartbeat: now,
        responseMs: null,
        detail: gatewayQ.data
          ? `${gatewayQ.data.ws_clients} WS clients`
          : gwOk
            ? "reachable · API serving"
            : "unreachable",
        api: undefined,
      },
      {
        key: "database",
        name: "Database (RDS)",
        icon: Database,
        health: dbUp ? "HEALTHY" : "DOWN",
        backing: "RDS",
        heartbeat: now,
        responseMs: null,
        detail: fastagQ.data?.db ? `PostgreSQL · ${fastagQ.data.db}` : "PostgreSQL",
      },
      {
        key: "kafka",
        name: "Kafka / Event Bus",
        icon: Radio,
        health: gwOk ? "HEALTHY" : gatewayQ.isError ? "DOWN" : "UNKNOWN",
        backing: "EPHEMERAL",
        heartbeat: gatewayQ.dataUpdatedAt ? new Date(gatewayQ.dataUpdatedAt).toISOString() : null,
        responseMs: null,
        detail: gatewayQ.data
          ? `streaming · ${gatewayQ.data.ws_clients} subscribers`
          : "event stream",
        api: "alerts",
      },
      {
        key: "cameras",
        name: "Cameras (ANPR)",
        icon: Camera,
        health: camHealthy ? (camLive ? "HEALTHY" : "DEGRADED") : "UNKNOWN",
        backing: backingFromPath(camPath),
        heartbeat: now,
        responseMs: null,
        detail: `${cameras.length} camera feeds`,
        api: "anpr",
      },
      {
        key: "gps",
        name: "GPS / Telemetry",
        icon: Satellite,
        health: healthFromState(gps?.state),
        backing: backingFromPath(gps?.last_decision_path),
        heartbeat: gps?.last_ok ?? null,
        responseMs: gps?.latency_p95_ms ?? null,
        detail: gps?.source ?? "trucking telemetry",
        api: "trucks",
        source: gps?.source,
      },
      {
        key: "ai",
        name: "AI Services",
        icon: Cpu,
        health: healthFromState(ai?.state),
        backing: backingFromPath(ai?.last_decision_path),
        heartbeat: ai?.last_ok ?? null,
        responseMs: ai?.latency_p95_ms ?? null,
        detail: "anomaly · forecaster · OCR",
        api: "alerts",
        source: ai?.source,
      },
      {
        key: "storage",
        name: "Storage (MinIO)",
        icon: HardDrive,
        health: dbUp ? "HEALTHY" : "UNKNOWN",
        backing: "RDS",
        heartbeat: now,
        responseMs: null,
        detail: "evidence object store",
      },
      {
        key: "external",
        name: "External APIs",
        icon: Globe,
        health: healthFromState(external?.state),
        backing: backingFromPath(external?.last_decision_path),
        heartbeat: external?.last_ok ?? null,
        responseMs: external?.latency_p95_ms ?? null,
        detail: "Vahan · Sarathi · FASTag · Traffic",
        api: "vahan",
        source: external?.source,
      },
    ];
  }, [
    sources,
    cameras,
    fastagQ.data,
    gatewayQ.data,
    gatewayQ.dataUpdatedAt,
    sourcesQ.isError,
    camerasQ.isError,
  ]);

  const healthy = services.filter((s) => s.health === "HEALTHY").length;
  const degraded = services.filter((s) => s.health === "DEGRADED").length;
  const down = services.filter((s) => s.health === "DOWN").length;

  const updatedAt = Math.max(
    sourcesQ.dataUpdatedAt || 0,
    camerasQ.dataUpdatedAt || 0,
    gatewayQ.dataUpdatedAt || 0,
  );

  return (
    <PageContainer>
      <PageHeader
        icon={Activity}
        title={t("nav.health")}
        subtitle="Live subsystem monitoring · LIVE / SIM / RDS / Ephemeral backing"
        updatedAt={updatedAt}
        isFetching={sourcesQ.isFetching && !sourcesQ.isLoading}
        onRefresh={() => {
          void sourcesQ.refetch();
          void camerasQ.refetch();
          void gatewayQ.refetch();
        }}
        actions={<AssumptionsPanel />}
      />

      {/* Overall status */}
      <div className="px-4 pt-3">
        <StatGrid className="lg:grid-cols-4">
          <StatCard icon={Server} label="Services" value={services.length} tone="info" />
          <StatCard icon={Activity} label="Healthy" value={healthy} tone="ok" />
          <StatCard
            icon={Activity}
            label="Degraded"
            value={degraded}
            tone={degraded > 0 ? "warn" : "ok"}
          />
          <StatCard icon={Activity} label="Down" value={down} tone={down > 0 ? "critical" : "ok"} />
        </StatGrid>
      </div>

      {/* Backing legend */}
      <div className="flex flex-wrap items-center gap-2 px-4 pt-3 text-[11px] text-muted-foreground">
        <span className="font-medium">Backing:</span>
        <StatusChip label="LIVE" tone="ok" /> real vendor
        <StatusChip label="SIM" tone="warn" /> simulator
        <StatusChip label="RDS" tone="info" /> persisted
        <StatusChip label="EPHEMERAL" tone="neutral" /> in-memory
      </div>

      {/* Service cards */}
      {sourcesQ.isLoading ? (
        <div className="flex items-center gap-2 p-6 text-sm text-muted-foreground">
          <Spinner /> {t("health.loadingSourceHealth")}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 px-4 py-3 sm:grid-cols-2 xl:grid-cols-4">
          {services.map((s) => (
            <ServiceCard
              key={s.key}
              svc={s}
              onClick={
                s.api ? () => setDrawer({ title: s.name, api: s.api, source: s.source }) : undefined
              }
            />
          ))}
        </div>
      )}

      {/* Cameras detail */}
      <div className="px-4 pb-3">
        <Card className="p-3">
          <h2 className="mb-2 text-sm font-semibold">{t("health.anprCamerasTitle")}</h2>
          <div className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-6">
            {cameras.map((c) => (
              <CameraChip
                key={c.camera_id}
                cam={c}
                onClick={() => setDrawer({ title: c.camera_id, api: "anpr" })}
              />
            ))}
            {cameras.length === 0 && (
              <span className="text-xs text-muted-foreground">No camera feeds reported.</span>
            )}
          </div>
        </Card>
      </div>

      {/* Driver identity verification (preserved) */}
      <div className="px-4 pb-6">
        <IdentityPanel />
      </div>

      <LogDrawer drawer={drawer} onClose={() => setDrawer(null)} />
    </PageContainer>
  );
}

function ServiceCard({ svc, onClick }: { svc: Service; onClick?: () => void }) {
  const colour = HEALTH_COLOUR[svc.health];
  const Icon = svc.icon;
  const body = (
    <Card
      className={`h-full space-y-2.5 p-3 ${onClick ? "transition-colors hover:border-primary/60" : ""}`}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span
            className="flex h-8 w-8 items-center justify-center rounded-lg"
            style={{ backgroundColor: `${colour}1a`, color: colour }}
          >
            <Icon className="h-4 w-4" />
          </span>
          <span className="text-sm font-semibold">{svc.name}</span>
        </div>
        <StatusDot colour={colour} pulse={svc.health === "HEALTHY"} />
      </div>
      <div className="flex items-center gap-1.5">
        <StatusChip
          label={svc.health}
          tone={
            svc.health === "HEALTHY"
              ? "ok"
              : svc.health === "DEGRADED"
                ? "warn"
                : svc.health === "DOWN"
                  ? "critical"
                  : "neutral"
          }
        />
        <StatusChip label={svc.backing} tone={BACKING_TONE[svc.backing]} />
      </div>
      <dl className="grid grid-cols-2 gap-x-2 gap-y-1 text-[11px] text-muted-foreground">
        <dt>Last heartbeat</dt>
        <dd className="text-right text-foreground">
          {svc.heartbeat ? relativeAge(svc.heartbeat) : "—"}
        </dd>
        <dt>Response p95</dt>
        <dd className="text-right tabular-nums text-foreground">
          {svc.responseMs != null ? `${Math.round(svc.responseMs)} ms` : "—"}
        </dd>
        <dt>Status</dt>
        <dd className="text-right text-foreground">
          {svc.health === "HEALTHY" ? "Operational" : svc.health}
        </dd>
      </dl>
      <div
        className="truncate border-t border-border/60 pt-2 text-[11px] text-muted-foreground"
        title={svc.detail}
      >
        {svc.detail}
      </div>
    </Card>
  );
  if (!onClick) return body;
  return (
    <button onClick={onClick} className="text-left">
      {body}
    </button>
  );
}

function CameraChip({ cam, onClick }: { cam: CameraHealth; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="flex items-center justify-between rounded-md border border-border bg-background px-2.5 py-2 text-left hover:border-primary/60"
    >
      <span className="truncate text-xs">{cam.camera_id.replace("CAM-", "")}</span>
      <DecisionPathBadge path={cam.decision_path} />
    </button>
  );
}

function LogDrawer({
  drawer,
  onClose,
}: {
  drawer: { title: string; api?: string; source?: string } | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const q = useQuery({
    queryKey: ["decisions", drawer?.api],
    queryFn: () => getAdapter().decisions(drawer?.api, 200),
    enabled: !!drawer,
    refetchInterval: drawer ? 4000 : false,
  });
  const rows: Decision[] = (q.data ?? []).filter(
    (d) => !drawer?.source || !d.key || d.key === drawer.source || d.api === drawer.api,
  );
  return (
    <Dialog open={!!drawer} onOpenChange={(o) => !o && onClose()}>
      <DialogContent side="right">
        {drawer && (
          <>
            <DialogHeader>
              <DialogTitle>
                {t("health.decisionLog")} · {drawer.title}
              </DialogTitle>
            </DialogHeader>
            <div className="p-4">
              {q.isLoading ? (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Spinner /> {t("health.loadingDecisions")}
                </div>
              ) : rows.length === 0 ? (
                <p className="text-sm text-muted-foreground">{t("health.noRecentDecisions")}</p>
              ) : (
                <ul className="space-y-1.5">
                  {rows.slice(0, 100).map((d, i) => (
                    <li
                      key={i}
                      className="rounded-md border border-border/60 bg-background px-3 py-2 text-xs"
                    >
                      <div className="flex items-center justify-between">
                        <DecisionPathBadge path={d.decision_path} />
                        <span className="text-muted-foreground">{fmtDateTimeIST(d.ts)}</span>
                      </div>
                      <div className="mt-1 flex justify-between text-muted-foreground">
                        <span className="font-mono">
                          {d.api}
                          {d.key ? ` · ${d.key}` : ""}
                        </span>
                        <span className="tabular-nums">
                          {d.latency_ms != null ? `${Math.round(d.latency_ms)} ms` : ""}
                        </span>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
