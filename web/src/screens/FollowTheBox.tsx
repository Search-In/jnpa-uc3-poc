// Follow-the-Box — cross-twin container journey (UC-3 audit P1 + cross-twin
// continuity). Search one ISO 6346 container number and follow it continuously
// across BOTH twins: UC-II (vessel discharge → yard → release → cross-twin
// PUBLISH) hands the cargo.dpd_release event over to UC-III (cross-twin RECEIVE
// → truck → ANPR → gate → ETA). The handoff is shown as an explicit centre card
// so an evaluator sees UC-II → UC-III continuity at a glance. Fully localised
// (en/hi/mr): stage titles/details resolve through i18n keyed by stage; the API
// English strings are the fallback. Backed by GET /api/journey/container/{no}.

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Box, Search, Ship, Truck, ArrowRight, ArrowDown, Check, Radio } from "lucide-react";
import { getAdapter, DATA_MODE } from "@/data";
import { isValidContainerNo } from "@/lib/iso6346";
import { PageContainer, PageHeader, StatusChip } from "@/components/ui/dtccc";
import { Card } from "@/components/ui/card";
import { EmptyState, LoadingState } from "@/components/ui/misc";
import { STATUS } from "@/lib/tokens";
import { fmtDateTimeIST } from "@/lib/utils";
import type { ContainerJourney, JourneyStage } from "@/data/types";

const DEMO_CONTAINER = "MSCU1234566"; // pinned valid ISO 6346 demo box

function StageItem({ s, accent }: { s: JourneyStage; accent: string }) {
  const { t } = useTranslation();
  const real = s.source.includes("gate-data") || s.source === "live";
  const f = (s.facts ?? {}) as Record<string, unknown>;
  const title = t(`followBox.stage.${s.stage}`, { defaultValue: s.title });
  const detail = t(`followBox.detail.${s.stage}`, {
    defaultValue: s.detail,
    vessel: f.vessel,
    block: f.yard_block,
    topic: f.topic,
    vehicle: f.vehicle_no,
    gate: f.gate,
    camera: f.camera_id,
    conf: f.conf,
    eta: f.eta_min,
  });
  return (
    <li className="relative pb-3 last:pb-0">
      <span className="absolute -left-4 top-1.5 h-2 w-2 rounded-full" style={{ backgroundColor: accent }} />
      <span className="absolute -left-[13px] top-3.5 h-full w-px" style={{ backgroundColor: accent + "33" }} />
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[13px] font-semibold">{title}</span>
        <span
          className="rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide"
          style={{ color: real ? STATUS.ok : STATUS.unknown, backgroundColor: (real ? STATUS.ok : STATUS.unknown) + "22" }}
          title={real ? "Real captured record" : "Deterministically reconstructed (SIMULATED)"}
        >
          {real ? t("followBox.real") : t("followBox.simulated")}
        </span>
      </div>
      <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[10px] text-muted-foreground">
        <span>{fmtDateTimeIST(s.ts)}</span>
        {s.source_system && <span>· {s.source_system}</span>}
        {s.event_id && <span className="font-mono">· {s.event_id}</span>}
      </div>
      <div className="mt-0.5 text-[12px] leading-snug text-muted-foreground">{detail}</div>
      {s.facts && Object.keys(s.facts).length > 0 && (
        <div className="mt-1 flex flex-wrap gap-1">
          {Object.entries(s.facts)
            .filter(([, v]) => v != null && v !== "")
            .map(([k, v]) => (
              <span key={k} className="inline-flex items-center gap-1 rounded border border-border bg-muted/40 px-1.5 py-0.5 text-[10px]">
                <span className="text-muted-foreground">{k}</span>
                <span className="font-mono text-foreground">{String(v)}</span>
              </span>
            ))}
        </div>
      )}
    </li>
  );
}

function TwinColumn({ twin, icon: Icon, stages }: { twin: string; icon: typeof Ship; stages: JourneyStage[] }) {
  const { t } = useTranslation();
  const accent = twin === "UC-II" ? STATUS.info : STATUS.ok;
  const who = twin === "UC-II" ? t("followBox.cargoTwin") : t("followBox.trafficTwin");
  return (
    <Card className="p-4" style={{ borderColor: accent + "55" }}>
      <div className="mb-3 flex items-center gap-2">
        <Icon size={16} style={{ color: accent }} />
        <h3 className="text-sm font-semibold">{twin} · {who}</h3>
      </div>
      <ol className="relative space-y-0 pl-4">
        {stages.map((s, i) => (
          <StageItem key={s.stage + i} s={s} accent={accent} />
        ))}
      </ol>
    </Card>
  );
}

function Row({ k, v, mono }: { k: string; v?: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <dt className="shrink-0 text-muted-foreground">{k}</dt>
      <dd className={`truncate text-right ${mono ? "font-mono" : ""}`} title={v}>{v ?? "—"}</dd>
    </div>
  );
}

function HandoffCard({ j }: { j: ContainerJourney }) {
  const { t } = useTranslation();
  const x = j.cross_twin;
  if (!x) return null;
  return (
    <div className="flex flex-col items-center justify-center gap-1">
      <ArrowRight className="hidden text-muted-foreground lg:block" size={20} />
      <ArrowDown className="text-muted-foreground lg:hidden" size={18} />
      <Card className="w-full p-3" style={{ borderColor: STATUS.warning + "88", borderWidth: 2 }}>
        <div className="mb-2 flex items-center gap-1.5">
          <Radio size={14} style={{ color: STATUS.warning }} />
          <span className="text-[12px] font-bold">{t("followBox.crossTwinEvent")}</span>
        </div>
        <dl className="space-y-1 text-[11px]">
          <Row k={t("followBox.topicLabel")} v={x.topic} mono />
          <Row k={t("followBox.publishing")} v={x.publishing_twin} />
          <Row k={t("followBox.receiving")} v={x.receiving_twin} />
          <Row k={t("followBox.correlation")} v={x.correlation_id} mono />
          <Row k={t("followBox.caseId")} v={x.case_id} mono />
          <Row k={t("followBox.eventId")} v={x.event_id} mono />
          <Row k={t("followBox.eventTime")} v={fmtDateTimeIST(x.event_time)} />
        </dl>
        <div className="mt-2 flex items-center justify-between">
          <StatusChip label={`${t("followBox.statusWord")} · ${t("followBox.delivered")}`} tone="ok" />
          {x.simulated && (
            <span
              className="rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase"
              style={{ color: STATUS.warning, backgroundColor: STATUS.warning + "22" }}
              title="The cross-twin transport is simulated in the PoC"
            >
              {t("followBox.simulated")}
            </span>
          )}
        </div>
      </Card>
      <ArrowRight className="hidden text-muted-foreground lg:block" size={20} />
      <ArrowDown className="text-muted-foreground lg:hidden" size={18} />
    </div>
  );
}

function JourneyStatusBar({ j }: { j: ContainerJourney }) {
  const { t } = useTranslation();
  const steps = j.journey_status ?? [];
  if (!steps.length) return null;
  const done = steps.filter((s) => s.done).length;
  return (
    <Card className="p-3">
      <div className="mb-2 flex items-center gap-2 text-[12px] font-semibold">
        <span>{t("followBox.journeyStatus")}</span>
        <span className="text-muted-foreground">
          {t("followBox.complete", { done, total: steps.length })}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        {steps.map((s, i) => (
          <div key={s.key} className="flex items-center gap-1.5">
            <span
              className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px]"
              style={{
                color: s.done ? STATUS.ok : STATUS.unknown,
                backgroundColor: (s.done ? STATUS.ok : STATUS.unknown) + "18",
              }}
            >
              {s.done && <Check size={11} />}
              {t(`followBox.status.${s.key}`, { defaultValue: s.label })}
            </span>
            {i < steps.length - 1 && <span className="text-muted-foreground">›</span>}
          </div>
        ))}
      </div>
    </Card>
  );
}

function MetaBar({ j }: { j: ContainerJourney }) {
  const { t } = useTranslation();
  const cells: { k: string; v?: string }[] = [
    { k: t("followBox.meta.container"), v: j.container_no },
    { k: t("followBox.meta.correlation"), v: j.correlation_id },
    { k: t("followBox.meta.caseId"), v: j.case_id },
    { k: t("followBox.meta.vehicle"), v: j.vehicle_no },
    { k: t("followBox.meta.gate"), v: j.gate },
    { k: t("followBox.meta.eta"), v: j.eta_min != null ? t("followBox.etaMin", { n: j.eta_min }) : undefined },
    { k: t("followBox.meta.topic"), v: j.cross_twin?.topic },
    { k: t("followBox.meta.delivery"), v: j.cross_twin?.status },
  ];
  return (
    <Card className="p-3">
      <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-4 xl:grid-cols-8">
        {cells.map((c) => (
          <div key={c.k}>
            <div className="text-[9px] uppercase tracking-wide text-muted-foreground">{c.k}</div>
            <div className="truncate font-mono text-[12px] font-medium" title={c.v}>{c.v ?? "—"}</div>
          </div>
        ))}
      </div>
    </Card>
  );
}

export default function FollowTheBox() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const [term, setTerm] = useState(params.get("c") || DEMO_CONTAINER);
  const [submitted, setSubmitted] = useState(params.get("c") || DEMO_CONTAINER);

  useEffect(() => {
    const c = params.get("c");
    if (c && c !== submitted) {
      setTerm(c);
      setSubmitted(c);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params]);

  const valid = useMemo(() => isValidContainerNo(submitted.trim().toUpperCase()), [submitted]);

  const q = useQuery({
    queryKey: ["container-journey", submitted],
    queryFn: () => getAdapter().containerJourney(submitted),
    enabled: submitted.trim().length > 0,
  });

  function go() {
    const c = term.trim().toUpperCase();
    setSubmitted(c);
    setParams(c ? { c } : {});
  }

  const uc2 = (q.data?.stages || []).filter((s) => s.twin === "UC-II");
  const uc3 = (q.data?.stages || []).filter((s) => s.twin === "UC-III");

  return (
    <PageContainer>
      <PageHeader title={t("followBox.title")} subtitle={t("followBox.subtitle")} icon={Box} />

      <div className="px-4 pt-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex items-center gap-2 rounded-md border border-border bg-card px-2 py-1.5">
            <Search size={14} className="text-muted-foreground" />
            <input
              value={term}
              onChange={(e) => setTerm(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && go()}
              placeholder={t("followBox.searchPlaceholder")}
              className="w-56 bg-transparent text-sm outline-none"
              spellCheck={false}
            />
          </div>
          <button
            onClick={go}
            className="rounded-md bg-primary px-3 py-1.5 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90"
          >
            {t("followBox.follow")}
          </button>
          {submitted && (
            <StatusChip
              label={valid ? t("followBox.isoValid") : t("followBox.isoInvalid")}
              tone={valid ? "ok" : "critical"}
            />
          )}
          <span className="ml-auto text-[11px] text-muted-foreground">
            DATA_MODE: <strong className="text-foreground">{q.data?.data_mode ?? DATA_MODE}</strong>
          </span>
        </div>
      </div>

      <div className="space-y-3 px-4 py-3">
        {q.isLoading ? (
          <LoadingState />
        ) : !q.data ? (
          <EmptyState>{t("followBox.unavailable")}</EmptyState>
        ) : (
          <>
            <MetaBar j={q.data} />
            <JourneyStatusBar j={q.data} />

            <div className="grid grid-cols-1 items-stretch gap-3 lg:grid-cols-[1fr_16rem_1fr]">
              <TwinColumn twin="UC-II" icon={Ship} stages={uc2} />
              <HandoffCard j={q.data} />
              <TwinColumn twin="UC-III" icon={Truck} stages={uc3} />
            </div>

            {q.data.note && (
              <p className="text-[11px] leading-snug text-muted-foreground">
                <strong>Note:</strong> {t("followBox.note")}
              </p>
            )}
          </>
        )}
      </div>
    </PageContainer>
  );
}
