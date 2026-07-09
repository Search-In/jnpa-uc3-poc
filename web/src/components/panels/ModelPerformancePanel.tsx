import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { getAdapter } from "@/data";
import { CollapsibleCard } from "@/components/ui/CollapsibleCard";
import { Spinner, EmptyState } from "@/components/ui/misc";
import { STATUS } from "@/lib/tokens";
import type { CongestionMetrics, OcrEval } from "@/data/types";

// Evaluator-facing AI model-performance card (UC-3 audit P0). Surfaces the REAL
// evaluation metrics behind the two headline models so a reviewer can verify
// accuracy without opening a terminal:
//   • ANPR   — YOLOv8n + PaddleOCR PP-OCRv4  (GET /api/anpr/eval)
//   • Traffic — GraphSAGE + LSTM             (GET /api/traffic/metrics)
// Honesty: every value is proxied from the model's own eval; when the PoC host
// runs the weights-less fallback OCR the tile shows the true (low) numbers and a
// "SIMULATED / DEGRADED" badge. It never fabricates the committed target.

function pct(v: number | undefined): string {
  return v == null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function ModeBadge({ mode }: { mode?: string }) {
  if (!mode) return null;
  const live = mode === "live";
  return (
    <span
      className="rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide"
      style={{
        color: live ? STATUS.ok : STATUS.warning,
        backgroundColor: (live ? STATUS.ok : STATUS.warning) + "22",
      }}
      title="Whole-twin data posture (DATA_MODE)"
    >
      {mode}
    </span>
  );
}

function SynthBadge({ synthetic }: { synthetic?: boolean }) {
  if (!synthetic) return null;
  return (
    <span
      className="rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide"
      style={{ color: STATUS.warning, backgroundColor: STATUS.warning + "22" }}
      title="Metrics come from the degraded fallback model, not the trained weights"
    >
      simulated
    </span>
  );
}

function Metric({ label, value, target, met }: { label: string; value: string; target?: string; met?: boolean }) {
  const colour = met == null ? undefined : met ? STATUS.ok : STATUS.warning;
  return (
    <div className="rounded-md border border-border bg-muted/30 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="text-lg font-semibold tabular-nums" style={{ color: colour }}>
        {value}
      </div>
      {target != null && (
        <div className="text-[9px] text-muted-foreground tabular-nums">target {target}</div>
      )}
    </div>
  );
}

function AnprBlock({ e }: { e: OcrEval }) {
  const met = e.target_met;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">ANPR · OCR</span>
        <span className="text-[11px] text-muted-foreground">{e.model_name ?? "—"}</span>
        <ModeBadge mode={e.data_mode} />
        <SynthBadge synthetic={e.metrics_synthetic ?? e.degraded} />
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Metric label="Accuracy" value={pct(e.accuracy ?? e.clear_accuracy)} target={pct(e.target)} met={met} />
        <Metric label="Precision" value={pct(e.precision)} />
        <Metric label="Detect recall" value={pct(e.recall)} />
        <Metric label="OCR conf" value={pct(e.ocr_confidence)} />
      </div>
      {e.dataset_breakdown && e.dataset_breakdown.length > 0 && (
        <div>
          <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
            Dataset breakdown (per condition)
          </div>
          <div className="space-y-1">
            {e.dataset_breakdown.map((s) => (
              <div key={s.condition} className="flex items-center gap-2">
                <span className="w-16 shrink-0 text-[11px]">{s.condition}</span>
                <div className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
                  <div
                    className="h-full"
                    style={{
                      width: `${Math.round((s.exact_match ?? 0) * 100)}%`,
                      backgroundColor: STATUS.info,
                    }}
                  />
                </div>
                <span className="w-24 shrink-0 text-right text-[10px] tabular-nums text-muted-foreground">
                  {pct(s.exact_match)} · n={s.n ?? "?"}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function CongestionBlock({ m }: { m: CongestionMetrics }) {
  const met = m.target_met;
  return (
    <div className="space-y-2 border-t border-border pt-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">Traffic · Congestion onset</span>
        <span className="text-[11px] text-muted-foreground">{m.model_name ?? "—"}</span>
        <ModeBadge mode={m.data_mode} />
        <SynthBadge synthetic={m.metrics_synthetic} />
      </div>
      <div className="grid grid-cols-3 gap-2">
        <Metric label="F1" value={pct(m.f1)} target={pct(m.target)} met={met} />
        <Metric label="Precision" value={pct(m.precision)} />
        <Metric label="Recall" value={pct(m.recall)} />
      </div>
      {m.evaluation_dataset && (
        <div className="text-[10px] leading-snug text-muted-foreground">
          <span className="font-medium">Eval set:</span> {m.evaluation_dataset}
        </div>
      )}
    </div>
  );
}

export function ModelPerformancePanel() {
  const { t } = useTranslation();
  const anpr = useQuery({ queryKey: ["ocr-eval"], queryFn: () => getAdapter().ocrEval() });
  const cong = useQuery({ queryKey: ["congestion-metrics"], queryFn: () => getAdapter().congestionMetrics() });

  const loading = anpr.isLoading || cong.isLoading;
  const empty = !anpr.data && !cong.data;

  return (
    <CollapsibleCard
      id="model-performance"
      title={t("panels.modelPerf.title", "AI Model Performance")}
      subtitle={t(
        "panels.modelPerf.subtitle",
        "Held-out evaluation metrics — proxied live from each model's /eval",
      )}
      bodyClassName="space-y-3"
    >
      {loading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> {t("common.loading")}
        </div>
      ) : empty ? (
        <EmptyState>
          {t(
            "panels.modelPerf.empty",
            "Model eval endpoints unreachable — start the anpr / congestion services to verify.",
          )}
        </EmptyState>
      ) : (
        <>
          {anpr.data && <AnprBlock e={anpr.data} />}
          {cong.data && <CongestionBlock m={cong.data} />}
        </>
      )}
    </CollapsibleCard>
  );
}

export default ModelPerformancePanel;
