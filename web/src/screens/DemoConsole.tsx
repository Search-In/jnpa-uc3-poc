// Operations Testing Console (FINAL PHASE redesign of the Demo Console).
// A presenter/QA surface that groups the platform's testing capabilities into
// clearly-labelled DEMO cards — Fault Injection, AI Testing, Traffic Simulation,
// Camera Testing, API Testing — so simulation controls are never confused with
// production. Fault injection (camera/vahan/trucks fallback chains), the operator
// banner (live WS), and the realism probes (OCR / congestion-F1) are preserved
// verbatim; only the Calcite chrome is replaced by the DTCCC kit. No backend edits.

import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import {
  SlidersHorizontal,
  Bug,
  Cpu,
  Truck,
  Camera,
  Plug,
  RotateCcw,
  TriangleAlert,
  Play,
  ExternalLink,
  Rocket,
  Snowflake,
  CalendarClock,
} from "lucide-react";
import { getAdapter, DATA_MODE } from "@/data";
import { api } from "@/lib/api";
import { useSocket } from "@/hooks/SocketContext";
import type { FaultControlResult, FaultSeverity, FaultState, OperatorBanner } from "@/lib/types";
import { Card } from "@/components/ui/card";
import { Spinner } from "@/components/ui/misc";
import { KpiStrip } from "@/components/panels/KpiStrip";
import { DecisionPathBadge } from "@/components/DecisionPathBadge";
import { PageContainer, PageHeader, StatusChip, type Tone } from "@/components/ui/dtccc";
import { STATUS } from "@/lib/tokens";
import { cn } from "@/lib/utils";

const CHAINS: { domain: "camera" | "vahan" | "trucks"; label: string; blurb: string }[] = [
  { domain: "camera", label: "Camera", blurb: "LIVE → CACHED → SYNTHETIC frame fallback" },
  { domain: "vahan", label: "Vahan", blurb: "LIVE_PRIMARY → LIVE_FALLBACK → CACHED → PROVISIONAL" },
  { domain: "trucks", label: "Trucks", blurb: "PRIMARY → SECONDARY → TERTIARY telemetry" },
];

function sevTone(sev?: FaultSeverity | null): Tone {
  if (sev === "RED") return "critical";
  if (sev === "AMBER") return "warn";
  if (sev === "GREEN") return "ok";
  return "neutral";
}

/** Card wrapper with a persistent DEMO badge so simulation features never read as production. */
function TestingCard({
  icon: Icon,
  title,
  subtitle,
  children,
  className,
}: {
  icon: typeof Bug;
  title: string;
  subtitle?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <Card className={cn("flex flex-col p-3", className)}>
      <div className="mb-2 flex items-start justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <Icon className="h-4 w-4" />
          </span>
          <div>
            <h3 className="text-sm font-semibold leading-tight">{title}</h3>
            {subtitle && <p className="text-[11px] text-muted-foreground">{subtitle}</p>}
          </div>
        </div>
        <StatusChip label="DEMO" tone="warn" />
      </div>
      {children}
    </Card>
  );
}

export default function DemoConsole() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { operatorBanner: wsBanner } = useSocket();
  const [localBanner, setLocalBanner] = useState<OperatorBanner | null>(null);

  // UC-III demo shortcuts — seed helpers reuse existing @/lib/api methods, then navigate.
  const seedReefer = useMutation({
    mutationFn: () => api.reeferSeed(24),
    onSuccess: () => navigate("/parking?tab=reefer"),
  });
  const seedRms = useMutation({
    mutationFn: () => api.rmsSeed({ gate_id: "G-NSICT", slots_per_day: 8 }),
    onSuccess: () => navigate("/health?tab=integrations"),
  });

  const faultsQ = useQuery<FaultState>({
    queryKey: ["faults"],
    queryFn: () => getAdapter().getFaults(),
  });
  const force = useMutation({
    mutationFn: ({ domain, rung }: { domain: string; rung: string }) =>
      getAdapter().forceFault(domain, rung),
    onSuccess: (res: FaultControlResult) => {
      setLocalBanner(res.banner);
      void qc.invalidateQueries({ queryKey: ["faults"] });
    },
  });
  const clear = useMutation({
    mutationFn: (domain?: string) => getAdapter().clearFault(domain),
    onSuccess: (res: FaultControlResult) => {
      setLocalBanner(res.banner);
      void qc.invalidateQueries({ queryKey: ["faults"] });
    },
  });

  const banner: OperatorBanner | null = wsBanner ?? localBanner ?? null;
  const bannerActive = banner?.active ?? false;

  const ocrQ = useQuery({ queryKey: ["ocr-eval"], queryFn: () => getAdapter().ocrEval() });
  const f1Q = useQuery({
    queryKey: ["congestion-metrics"],
    queryFn: () => getAdapter().congestionMetrics(),
  });

  const faults = faultsQ.data;
  const anyForced = useMemo(
    () => (faults ? Object.values(faults.domains).some((d) => d.forced_rung != null) : false),
    [faults],
  );

  const chainCard = (domain: "camera" | "vahan" | "trucks") => {
    const chain = CHAINS.find((c) => c.domain === domain)!;
    const state = faults?.domains[domain];
    const rungs = faults?.rungs[domain] ?? [];
    const forced = state?.forced_rung ?? null;
    return (
      <div
        key={domain}
        className={cn(
          "rounded-lg border p-2.5",
          forced ? "border-primary bg-primary/5" : "border-border",
        )}
      >
        <div className="mb-1.5 flex items-center justify-between">
          <span className="text-[13px] font-semibold">{chain.label}</span>
          <StatusChip label={state?.severity ?? "GREEN"} tone={sevTone(state?.severity)} />
        </div>
        <p className="mb-2 text-[11px] text-muted-foreground">{chain.blurb}</p>
        {faultsQ.isLoading ? (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Spinner /> {t("demo.loadingRungs")}
          </div>
        ) : (
          <div className="flex flex-wrap gap-1">
            {rungs.map((rung) => (
              <button
                key={rung}
                onClick={() => rung !== forced && force.mutate({ domain, rung })}
                className={cn(
                  "rounded-md border px-2 py-1 text-[11px] font-medium transition-colors",
                  rung === forced
                    ? "border-primary bg-primary text-primary-foreground"
                    : "border-border hover:bg-muted",
                )}
              >
                {rung}
              </button>
            ))}
          </div>
        )}
        <div className="mt-2 flex items-center justify-between">
          <span className="text-[11px] text-muted-foreground">
            {t("demo.forced")}:{" "}
            <span className="font-medium text-foreground">{forced ?? t("demo.none")}</span>
          </span>
          <button
            onClick={() => clear.mutate(domain)}
            disabled={!forced || clear.isPending}
            className="rounded-md border border-border px-2 py-0.5 text-[11px] hover:bg-muted disabled:opacity-40"
          >
            {t("demo.clear")}
          </button>
        </div>
      </div>
    );
  };

  return (
    <PageContainer>
      <PageHeader
        icon={SlidersHorizontal}
        title="Operations Testing Console"
        subtitle="Presenter & QA surface — fault injection, AI/camera/API testing. Every control is DEMO-only."
        actions={
          <button
            onClick={() => clear.mutate(undefined)}
            disabled={!anyForced || clear.isPending}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium hover:bg-muted disabled:opacity-40"
          >
            <RotateCcw className="h-3.5 w-3.5" />{" "}
            {clear.isPending && clear.variables === undefined
              ? t("demo.resetting")
              : t("demo.resetAllFaults")}
          </button>
        }
      />


      {/* Operator banner (live WS) */}
      <div className="px-4 pt-3">
        <div
          className={cn(
            "flex items-center gap-2 rounded-lg border px-3 py-2 text-xs",
            bannerActive
              ? banner?.severity === "RED"
                ? "border-severity-critical/40 bg-severity-critical/10"
                : "border-severity-warning/40 bg-severity-warning/10"
              : "border-border bg-muted/40",
          )}
        >
          <TriangleAlert
            className={cn(
              "h-4 w-4",
              bannerActive ? "text-severity-warning" : "text-muted-foreground",
            )}
          />
          <span className="font-semibold">{t("demo.operatorBannerTitle")}</span>
          <span className="text-muted-foreground">
            {banner && banner.domains.length > 0
              ? `${t("demo.forcedChains")}: ${banner.domains.join(", ")} · ${t("demo.severity")} ${banner.severity ?? "—"}`
              : t("demo.noFaultsForced")}
          </span>
          <StatusChip
            label={`${DATA_MODE.toUpperCase()} MODE`}
            tone={DATA_MODE === "live" ? "ok" : "warn"}
          />
        </div>
      </div>

      {/* Feature cards */}
      <div className="grid grid-cols-1 gap-3 px-4 py-3 lg:grid-cols-2">
        {/* Fault Injection */}
        <TestingCard
          icon={Bug}
          title="Fault Injection"
          subtitle="Force fallback rungs on the resilience chains"
          className="lg:col-span-2"
        >
          <div className="grid grid-cols-1 gap-2.5 md:grid-cols-3">
            {chainCard("camera")}
            {chainCard("vahan")}
            {chainCard("trucks")}
          </div>
        </TestingCard>

        {/* AI Testing */}
        <TestingCard
          icon={Cpu}
          title="AI Testing"
          subtitle="Model realism probes (RDS-backed metrics)"
        >
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <Probe
              label={t("demo.ocrAccuracyClear")}
              loading={ocrQ.isLoading}
              value={ocrQ.data ? `${(ocrQ.data.clear_accuracy * 100).toFixed(1)}%` : null}
              met={
                ocrQ.data
                  ? (ocrQ.data.target_met ?? ocrQ.data.clear_accuracy >= (ocrQ.data.target ?? 0.95))
                  : undefined
              }
              target={`≥${((ocrQ.data?.target ?? 0.95) * 100).toFixed(0)}%`}
            />
            <Probe
              label={t("demo.f1ForecastLabel")}
              loading={f1Q.isLoading}
              value={f1Q.data ? f1Q.data.f1.toFixed(3) : null}
              met={
                f1Q.data
                  ? (f1Q.data.target_met ?? f1Q.data.f1 >= (f1Q.data.target ?? 0.85))
                  : undefined
              }
              target={`≥${(f1Q.data?.target ?? 0.85).toFixed(2)}`}
            />
          </div>
        </TestingCard>

        {/* Traffic Simulation */}
        <TestingCard
          icon={Truck}
          title="Traffic Simulation"
          subtitle="Reactive scenarios & fleet simulator"
        >
          <p className="mb-2 text-xs text-muted-foreground">
            Drive corridor scenarios (TFC-1/2/3) and the live fleet simulator. Wired controls live
            in their own consoles.
          </p>
          <div className="flex flex-wrap gap-2">
            <Link
              to="/what-if"
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90"
            >
              <Play className="h-3.5 w-3.5" /> What-If Console
            </Link>
            <Link
              to="/simulator"
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-[13px] font-medium hover:bg-muted"
            >
              <ExternalLink className="h-3.5 w-3.5" /> Simulator
            </Link>
          </div>
        </TestingCard>

        {/* Camera Testing */}
        <TestingCard
          icon={Camera}
          title="Camera Testing"
          subtitle="ANPR frame-source fallback & provenance"
        >
          <p className="mb-2 text-xs text-muted-foreground">
            Camera fallback (LIVE → CACHED → SYNTHETIC) is forced from Fault Injection. Per-frame
            provenance:
          </p>
          <div className="flex flex-wrap items-center gap-2">
            {["LIVE", "CACHED", "SYNTHETIC"].map((p) => (
              <DecisionPathBadge key={p} path={p} />
            ))}
            <Link
              to="/health"
              className="ml-auto text-[11px] font-semibold text-primary hover:underline"
            >
              Camera health →
            </Link>
          </div>
        </TestingCard>

        {/* API Testing */}
        <TestingCard
          icon={Plug}
          title="API Testing"
          subtitle="Vendor fallback & decision-path provenance"
        >
          <p className="mb-2 text-xs text-muted-foreground">
            Vahan/Sarathi/FASTag fallback is forced from Fault Injection. Provenance badges follow
            the CB-safe palette.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            {["LIVE", "CACHED", "PROVISIONAL"].map((p) => (
              <DecisionPathBadge key={p} path={p} />
            ))}
            <Link
              to="/health"
              className="ml-auto text-[11px] font-semibold text-primary hover:underline"
            >
              Source health →
            </Link>
          </div>
        </TestingCard>

        {/* UC-III Demo Shortcuts */}
        <TestingCard
          icon={Rocket}
          title="UC-III Demo Shortcuts"
          subtitle="One-click seed + jump to the host screen for the UC-III walkthrough"
          className="lg:col-span-2"
        >
          <div className="flex flex-wrap gap-2">
            <button
              onClick={() => seedReefer.mutate()}
              disabled={seedReefer.isPending}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
            >
              <Snowflake className="h-3.5 w-3.5" />{" "}
              {seedReefer.isPending ? "Seeding…" : "Seed Reefer Slots"}
            </button>
            <button
              onClick={() => seedRms.mutate()}
              disabled={seedRms.isPending}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
            >
              <CalendarClock className="h-3.5 w-3.5" />{" "}
              {seedRms.isPending ? "Seeding…" : "Seed RMS-TAS Slots"}
            </button>
            <Link
              to="/alerts?tab=accidents"
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-[13px] font-medium hover:bg-muted"
            >
              <TriangleAlert className="h-3.5 w-3.5" /> Open Accident Console
            </Link>
            <Link
              to="/live?tab=trt"
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-[13px] font-medium hover:bg-muted"
            >
              <ExternalLink className="h-3.5 w-3.5" /> Open ECY TRT
            </Link>
            <Link
              to="/geofencing?tab=bottlenecks"
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-[13px] font-medium hover:bg-muted"
            >
              <ExternalLink className="h-3.5 w-3.5" /> Open Road Bottlenecks
            </Link>
            <Link
              to="/gate-customs"
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-[13px] font-medium hover:bg-muted"
            >
              <Camera className="h-3.5 w-3.5" /> Open Camera AI
            </Link>
          </div>
        </TestingCard>
      </div>

      {/* KPI deltas */}
      <div className="px-4 pb-6">
        <h2 className="mb-2 text-sm font-semibold">{t("demo.kpiStripTitle")}</h2>
        <KpiStrip />
      </div>
    </PageContainer>
  );
}

function Probe({
  label,
  value,
  met,
  target,
  loading,
}: {
  label: string;
  value: string | null;
  met?: boolean;
  target: string;
  loading: boolean;
}) {
  const colour = met === undefined ? STATUS.unknown : met ? STATUS.ok : STATUS.critical;
  return (
    <div className="rounded-lg border border-border p-3">
      {loading ? (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Spinner /> …
        </div>
      ) : value == null ? (
        <div className="text-xs text-muted-foreground">
          Metrics endpoint not exposed — {target} target.
        </div>
      ) : (
        <>
          <div className="text-2xl font-bold tabular-nums" style={{ color: colour }}>
            {value}
          </div>
          <div className="mt-0.5 text-[11px] text-muted-foreground">{label}</div>
          <div className="mt-1">
            <StatusChip
              label={met ? `meets ${target}` : `below ${target}`}
              tone={met ? "ok" : "critical"}
            />
          </div>
        </>
      )}
    </div>
  );
}
