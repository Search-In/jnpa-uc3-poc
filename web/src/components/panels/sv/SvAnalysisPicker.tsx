// Analysis selector shared by every embedded SecureVision panel.
//
// SecureVision answers only per-analysis: there is no incident-history API and
// nothing is persisted on our side, so "which clip are we looking at?" is a
// question the operator must answer before any incident panel has data. This
// component asks it once, in one consistent way, and — when no clip has been
// analysed yet — points at the workbench instead of showing an empty table that
// implies data should be there.

import { useEffect, useMemo } from "react";
import { Link } from "react-router-dom";
import { FileVideo } from "lucide-react";

import { FilterSelect } from "@/components/ui/dtccc";
import { EmptyState, Spinner } from "@/components/ui/misc";
import { fmtDateTimeIST } from "@/lib/utils";
import type { SvAnalysis } from "@/lib/securevision";
import { useSvAnalyses } from "@/hooks/useSecureVision";
import { SvUnavailable } from "./SvCommon";

function analysisLabel(a: SvAnalysis): string {
  const camera = a.jnpa_camera_id ?? a.securevision_camera_code ?? "unmapped camera";
  return `${a.filename ?? a.analysis_id} · ${camera} · ${fmtDateTimeIST(a.uploaded_at)}`;
}

export function SvAnalysisPicker({
  value,
  onChange,
  enabled = true,
}: {
  value: string | null;
  onChange: (analysisId: string | null) => void;
  enabled?: boolean;
}) {
  const q = useSvAnalyses(enabled);
  const analyses = useMemo(() => q.data?.analyses ?? [], [q.data]);

  // Adopt the newest analysis as soon as one exists, so the panels below have
  // something to render without the operator having to pick first. The parent
  // owns the selection; this only seeds it.
  useEffect(() => {
    if (!value && analyses.length) onChange(analyses[0].analysis_id);
  }, [value, analyses, onChange]);

  if (q.isLoading) {
    return (
      <span className="inline-flex items-center gap-2 text-xs text-muted-foreground">
        <Spinner className="h-3 w-3" /> Loading analyses…
      </span>
    );
  }
  if (q.error) return <SvUnavailable error={q.error} onRetry={() => void q.refetch()} compact />;
  if (!analyses.length) {
    return (
      <EmptyState>
        <span className="inline-flex flex-wrap items-center justify-center gap-1">
          <FileVideo className="h-4 w-4" aria-hidden />
          No SecureVision analysis yet.
          <Link to="/video-analytics" className="underline underline-offset-2">
            Analyse a clip
          </Link>
          to see AI detections here.
        </span>
      </EmptyState>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-2">
      <FilterSelect
        label="Analysis"
        value={value ?? analyses[0].analysis_id}
        onChange={(v) => onChange(v || null)}
        options={analyses.map((a) => ({ value: a.analysis_id, label: analysisLabel(a) }))}
      />
      <span className="text-[10.5px] text-muted-foreground">
        Session only — SecureVision keeps no incident history.
      </span>
    </div>
  );
}
