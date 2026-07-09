import { useMemo, useState } from "react";
import { ArrowDown, Lightbulb, Info } from "lucide-react";
import { CollapsibleCard } from "@/components/ui/CollapsibleCard";
import { STATUS } from "@/lib/tokens";
import {
  CAUSAL_CHAINS,
  CHAIN_ORDER,
  DEFAULT_CHAIN_ID,
  getCausalChain,
  type CausalStage,
  type StageKind,
} from "@/whatif/causalGraph";

// Reactive Guide (UC-3 audit P1, spec §8.1 differentiator) — explainable-AI
// side panel that turns a reactive scenario into a plain-language
// Cause → Impact → Action → Expected-outcome chain, so an evaluator sees WHY
// congestion happened and WHAT the system recommends. Adapts the UC-2
// causalGraph approach; deterministic (no LLM), house-style CollapsibleCard.
//
// When a What-If scenario is running its id is passed in and the guide locks to
// it; otherwise the user can pick a teaching chain (defaults to the spec's
// heavy-vehicle-surge example).

const KIND_META: Record<StageKind, { label: string; color: string }> = {
  cause: { label: "Cause", color: STATUS.warning },
  impact: { label: "Impact", color: STATUS.critical },
  action: { label: "Action", color: STATUS.info },
  outcome: { label: "Expected outcome", color: STATUS.ok },
};

function StageBlock({ stage, last }: { stage: CausalStage; last: boolean }) {
  const meta = KIND_META[stage.kind];
  return (
    <div className="flex flex-col items-stretch">
      <div className="rounded-lg border bg-card p-3" style={{ borderColor: meta.color + "66" }}>
        <div className="mb-1 flex items-center gap-2">
          <span
            className="rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide"
            style={{ color: meta.color, backgroundColor: meta.color + "22" }}
          >
            {meta.label}
          </span>
          {stage.kpi && (
            <span className="text-[10px] text-muted-foreground">KPI · {stage.kpi}</span>
          )}
          {stage.where && (
            <span className="ml-auto rounded bg-muted px-1.5 py-0.5 text-[10px] tabular-nums text-muted-foreground">
              📍 {stage.where}
            </span>
          )}
        </div>
        <div className="text-sm font-medium text-foreground">{stage.label}</div>
        <div className="mt-0.5 text-[12px] leading-snug text-muted-foreground">
          {stage.mechanism}
        </div>
        {stage.magnitude && (
          <div className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-border bg-muted/40 px-2 py-1 text-[11px]">
            <span className="text-muted-foreground tabular-nums">{stage.magnitude.from}</span>
            <span className="text-muted-foreground">→</span>
            <strong className="tabular-nums text-foreground">
              {stage.magnitude.to}
              {stage.magnitude.unit ? ` ${stage.magnitude.unit}` : ""}
            </strong>
            {stage.magnitude.simulated && (
              <span
                className="ml-1 rounded px-1 py-0.5 text-[9px] font-semibold uppercase"
                style={{ color: STATUS.warning, backgroundColor: STATUS.warning + "22" }}
                title="Simulated propagation figure under stated assumptions — not a live measurement"
              >
                sim
              </span>
            )}
          </div>
        )}
      </div>
      {!last && (
        <div className="flex justify-center py-1 text-muted-foreground">
          <ArrowDown size={16} />
        </div>
      )}
    </div>
  );
}

export function ReactiveGuidePanel({ scenarioId }: { scenarioId?: string | null }) {
  const [picked, setPicked] = useState<string>(DEFAULT_CHAIN_ID);
  // A running scenario locks the guide to it; otherwise the picker drives it.
  const activeId = scenarioId && CAUSAL_CHAINS[scenarioId] ? scenarioId : picked;
  const chain = useMemo(() => getCausalChain(activeId), [activeId]);
  const locked = Boolean(scenarioId && CAUSAL_CHAINS[scenarioId]);

  return (
    <CollapsibleCard
      id="reactive-guide"
      title="Reactive Guide · causal chain"
      subtitle="Why it happened → what the system recommends"
      bodyClassName="space-y-3"
    >
      {/* Scenario picker (hidden while a live scenario is locked in). */}
      {!locked && (
        <div className="flex flex-wrap gap-1.5">
          {CHAIN_ORDER.map((id) => {
            const on = id === activeId;
            return (
              <button
                key={id}
                onClick={() => setPicked(id)}
                className={`inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] ${
                  on
                    ? "border-primary bg-primary/10 text-foreground"
                    : "border-border bg-muted/40 text-muted-foreground hover:text-foreground"
                }`}
              >
                <Lightbulb size={11} />
                {CAUSAL_CHAINS[id].title}
              </button>
            );
          })}
        </div>
      )}

      {locked && (
        <div className="text-[11px] text-muted-foreground">
          Locked to the running scenario <strong className="text-foreground">{scenarioId}</strong>.
        </div>
      )}

      {chain && (
        <>
          <div className="text-[12px] text-muted-foreground">{chain.summary}</div>
          <div>
            {chain.stages.map((s, i) => (
              <StageBlock key={s.kind + i} stage={s} last={i === chain.stages.length - 1} />
            ))}
          </div>
        </>
      )}

      {/* Persistent honesty caption (Integrity Rule). */}
      <div
        className="flex items-start gap-1.5 rounded-md border px-2 py-1.5 text-[11px] text-muted-foreground"
        style={{ borderColor: STATUS.warning + "55", backgroundColor: STATUS.warning + "12" }}
      >
        <Info size={13} className="mt-0.5 shrink-0" />
        <span>
          Simulated causal propagation under stated assumptions — figures marked{" "}
          <strong>sim</strong> are shadow-run deltas anchored to documented KPI baselines, not
          claimed live JNPA measurements.
        </span>
      </div>
    </CollapsibleCard>
  );
}

export default ReactiveGuidePanel;
