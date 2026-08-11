// Shared SecureVision query hooks.
//
// One place owns the query keys, the poll posture and the "which analyses do we
// even have?" question, so the six host screens stay thin and cannot drift.
//
// The important constraint encoded here: SecureVision has NO incident-history
// API and nothing is persisted on our side, so "recent analyses" means the clips
// uploaded through this gateway process. Screens that show SecureVision data are
// therefore scoped to a chosen analysis — never to an implied searchable
// history, which does not exist.

import { useMemo } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { getAdapter } from "@/data";
import type { SvAnalysis, SvCombinedReport, SvIncident, SvPersonResult } from "@/lib/securevision";

/** Analyses are session state, not a feed — no polling, just a manual refresh. */
export const SV_STALE_MS = 30_000;

/** Upper bound on how many analyses an entity-centric screen (Vehicle 360) will
 *  scan for matches. Each analysis costs one upstream call per analyzer; the
 *  cap keeps a 360 view from fanning out unboundedly, and the UI states it. */
export const SV_SCAN_LIMIT = 10;

export const svKeys = {
  health: ["sv", "health"] as const,
  analyses: ["sv", "analyses"] as const,
  incident: (analysisId: string, code: string) => ["sv", "incident", analysisId, code] as const,
  persons: (analysisId: string) => ["sv", "incident", analysisId, "i07"] as const,
  combined: (analysisId: string) => ["sv", "incident", analysisId, "all"] as const,
  faces: ["sv", "faces"] as const,
  faceEvents: (limit: number) => ["sv", "faces", "events", limit] as const,
  faceStatus: ["sv", "faces", "status"] as const,
};

export function useSvHealth(enabled = true) {
  return useQuery({
    queryKey: svKeys.health,
    queryFn: () => getAdapter().svHealth(),
    staleTime: SV_STALE_MS,
    enabled,
    retry: false,
  });
}

export function useSvAnalyses(enabled = true) {
  return useQuery({
    queryKey: svKeys.analyses,
    queryFn: () => getAdapter().svAnalyses(),
    staleTime: SV_STALE_MS,
    enabled,
    retry: false,
  });
}

export function useSvIncident(
  analysisId: string | null,
  code: "i01" | "i02" | "i09" | "i12",
  enabled = true,
) {
  return useQuery<SvIncident>({
    queryKey: svKeys.incident(analysisId ?? "-", code),
    queryFn: () => getAdapter().svIncident(analysisId as string, code),
    enabled: Boolean(analysisId) && enabled,
    staleTime: SV_STALE_MS,
    retry: false,
  });
}

export function useSvPersons(analysisId: string | null, enabled = true) {
  return useQuery<SvPersonResult>({
    queryKey: svKeys.persons(analysisId ?? "-"),
    queryFn: () => getAdapter().svIncidentPersons(analysisId as string),
    enabled: Boolean(analysisId) && enabled,
    staleTime: SV_STALE_MS,
    retry: false,
  });
}

export function useSvCombined(analysisId: string | null, enabled = true) {
  return useQuery<SvCombinedReport>({
    queryKey: svKeys.combined(analysisId ?? "-"),
    queryFn: () => getAdapter().svIncidentAll(analysisId as string),
    enabled: Boolean(analysisId) && enabled,
    staleTime: SV_STALE_MS,
    retry: false,
  });
}

export function useSvFaceStatus(enabled = true) {
  return useQuery({
    queryKey: svKeys.faceStatus,
    queryFn: () => getAdapter().svFaceStatus(),
    staleTime: SV_STALE_MS,
    enabled,
    retry: false,
  });
}

export function useSvFaces(enabled = true) {
  return useQuery({
    queryKey: svKeys.faces,
    queryFn: () => getAdapter().svFaces(),
    staleTime: SV_STALE_MS,
    enabled,
    retry: false,
  });
}

export function useSvFaceEvents(limit = 100, enabled = true) {
  return useQuery({
    queryKey: svKeys.faceEvents(limit),
    queryFn: () => getAdapter().svFaceEvents({ limit }),
    staleTime: SV_STALE_MS,
    enabled,
    retry: false,
  });
}

export interface SvVehicleHit {
  analysis: SvAnalysis;
  plate: SvIncident | null;
  container: SvIncident | null;
}

/**
 * SecureVision detections that mention one vehicle.
 *
 * Entity-centric screens (Vehicle 360) ask "what has SecureVision seen of THIS
 * plate?", but the vendor only answers per-analysis. So this scans the most
 * recent analyses' I-01 (plate) and I-09 (container, which also carries the
 * towing plate) and keeps the ones whose read matches — with the scan bound
 * stated in `scanned` so the screen can say what it looked at instead of
 * implying it searched everything.
 */
export function useSvVehicleHits(plate: string | null, enabled = true) {
  const normalized = (plate ?? "").replace(/[^A-Za-z0-9]/g, "").toUpperCase();
  const analysesQ = useSvAnalyses(enabled && Boolean(normalized));
  const analyses = useMemo(
    () => (analysesQ.data?.analyses ?? []).slice(0, SV_SCAN_LIMIT),
    [analysesQ.data],
  );

  const results = useQueries({
    queries: analyses.flatMap((a) => [
      {
        queryKey: svKeys.incident(a.analysis_id, "i01"),
        queryFn: () => getAdapter().svIncident(a.analysis_id, "i01"),
        enabled: enabled && Boolean(normalized),
        staleTime: SV_STALE_MS,
        retry: false,
      },
      {
        queryKey: svKeys.incident(a.analysis_id, "i09"),
        queryFn: () => getAdapter().svIncident(a.analysis_id, "i09"),
        enabled: enabled && Boolean(normalized),
        staleTime: SV_STALE_MS,
        retry: false,
      },
    ]),
  });

  const hits = useMemo(() => {
    const out: SvVehicleHit[] = [];
    analyses.forEach((analysis, i) => {
      const plateInc = results[i * 2]?.data as SvIncident | undefined;
      const containerInc = results[i * 2 + 1]?.data as SvIncident | undefined;
      const matches = (value: string | null | undefined) =>
        Boolean(value) && value!.replace(/[^A-Za-z0-9]/g, "").toUpperCase() === normalized;
      const plateHit = plateInc?.fired && matches(plateInc.plate?.plate) ? plateInc : null;
      const containerHit =
        containerInc?.fired && matches(containerInc.container?.plate) ? containerInc : null;
      if (plateHit || containerHit) {
        out.push({ analysis, plate: plateHit, container: containerHit });
      }
    });
    return out;
  }, [analyses, results, normalized]);

  return {
    hits,
    scanned: analyses.length,
    truncated: (analysesQ.data?.analyses?.length ?? 0) > analyses.length,
    isLoading: analysesQ.isLoading || results.some((r) => r.isLoading),
    isFetching: analysesQ.isFetching || results.some((r) => r.isFetching),
    error: analysesQ.error ?? results.find((r) => r.error)?.error ?? null,
    refetch: () => {
      void analysesQ.refetch();
      results.forEach((r) => void r.refetch());
    },
  };
}
