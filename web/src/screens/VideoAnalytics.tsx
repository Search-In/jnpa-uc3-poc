// Video Analytics — the SecureVision clip-analysis workbench.
//
// The ONE new screen in this integration. Everything else SecureVision offers
// was folded into a screen that already owned the subject (plate/container reads
// -> Camera AI, zone intrusions -> Geo Analytics, camera tamper -> Gate & Lane
// Board, combined report -> Reports, faces -> Driver Enrollment). This workflow
// had no home: upload a clip -> get an analysis_id -> run analyzers -> review
// evidence -> watch the annotated replay -> delete the analysis. No existing
// screen does file -> job -> result -> media playback.
//
// Two things this screen is careful NOT to claim:
//
//   * It is not live CCTV. The supplied SecureVision API analyses UPLOADED
//     CLIPS; there is no documented continuous-ingestion endpoint. The screen is
//     named and worded accordingly.
//   * It is not a history. SecureVision publishes no incident-history API and
//     nothing is persisted to RDS, so the analysis list is explicitly
//     session-scoped rather than presented as a searchable archive.

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import {
  Camera,
  FileVideo,
  Film,
  ScanLine,
  ShieldAlert,
  Trash2,
  Upload,
  Video,
} from "lucide-react";

import { getAdapter } from "@/data";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  DataTable,
  FilterSelect,
  PageContainer,
  PageHeader,
  SegmentedTabs,
  StatCard,
  StatGrid,
  StatusChip,
  type Column,
} from "@/components/ui/dtccc";
import { EmptyState, Spinner } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { STATUS } from "@/lib/tokens";
import { fmtDateTimeIST } from "@/lib/utils";
import { svErrorMessage, type SvAnalysis } from "@/lib/securevision";
import { svKeys, useSvAnalyses, useSvHealth } from "@/hooks/useSecureVision";
import {
  SvCombinedReportPanel,
  SvPersonZonePanel,
  SvPlateContainerPanel,
  SvTamperPanel,
  SvVehicleCountPanel,
} from "@/components/panels/sv/SvIncidentPanels";
import { SvLiveDetectionStream } from "@/components/panels/sv/SvLiveDetectionStream";
import { SvSourceBadge, SvUnavailable } from "@/components/panels/sv/SvCommon";
import { useQuery } from "@tanstack/react-query";

type TabKey = "summary" | "vehicle" | "person" | "camera" | "replay";

export default function VideoAnalytics() {
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [tab, setTab] = useState<TabKey>("summary");
  const [selected, setSelected] = useState<string | null>(params.get("analysis"));

  const healthQ = useSvHealth();
  const analysesQ = useSvAnalyses();
  const analyses = analysesQ.data?.analyses ?? [];
  const analysisId = selected ?? analyses[0]?.analysis_id ?? null;
  const current = analyses.find((a) => a.analysis_id === analysisId) ?? null;

  // Keep the selection in the URL so an analysis is linkable for the length of
  // the session (it is session state — the link stops resolving after a restart,
  // which is honest given nothing is persisted).
  useEffect(() => {
    if (!analysisId) return;
    if (params.get("analysis") !== analysisId) {
      const next = new URLSearchParams(params);
      next.set("analysis", analysisId);
      setParams(next, { replace: true });
    }
  }, [analysisId, params, setParams]);

  const configured = healthQ.data?.configured !== false;

  return (
    <PageContainer>
      <PageHeader
        icon={Video}
        title="Video Analytics"
        subtitle="SecureVision AI analysis of uploaded camera clips — plate, container, vehicle count, restricted-zone person and camera-tamper detection"
        updatedAt={analysesQ.dataUpdatedAt}
        isFetching={analysesQ.isFetching}
        onRefresh={() => void qc.invalidateQueries({ queryKey: svKeys.analyses })}
      />

      <div className="space-y-3 px-4 pt-3">
        {healthQ.data?.status === "NOT_CONFIGURED" && (
          <Card className="p-3">
            <EmptyState>
              SecureVision is not configured on this deployment. Set the gateway&apos;s SecureVision
              service credentials to enable video analysis. Every other console surface is
              unaffected.
            </EmptyState>
          </Card>
        )}

        <UploadPanel disabled={!configured} onUploaded={(a) => setSelected(a.analysis_id)} />

        <AnalysisList
          analyses={analyses}
          selected={analysisId}
          onSelect={setSelected}
          isLoading={analysesQ.isLoading}
          error={analysesQ.error}
          onRetry={() => void analysesQ.refetch()}
          note={analysesQ.data?.note}
        />

        {analysisId && current && (
          <>
            <AnalysisSummary analysis={current} />
            <SegmentedTabs
              value={tab}
              onChange={setTab}
              tabs={[
                { key: "summary", label: "Combined report", icon: ScanLine },
                { key: "vehicle", label: "Vehicle & container", icon: Camera },
                { key: "person", label: "Restricted zone", icon: ShieldAlert },
                { key: "camera", label: "Camera health", icon: Film },
                { key: "replay", label: "Annotated replay", icon: Video },
              ]}
            />
            {tab === "summary" && <SvCombinedReportPanel analysisId={analysisId} />}
            {tab === "vehicle" && (
              <div className="space-y-3">
                <SvPlateContainerPanel analysisId={analysisId} />
                <SvVehicleCountPanel analysisId={analysisId} />
              </div>
            )}
            {tab === "person" && <SvPersonZonePanel analysisId={analysisId} />}
            {tab === "camera" && <SvTamperPanel analysisId={analysisId} />}
            {tab === "replay" && (
              <SvLiveDetectionStream
                analysisId={analysisId}
                onReRun={() =>
                  document.getElementById("sv-upload-input")?.scrollIntoView({ behavior: "smooth" })
                }
              />
            )}
          </>
        )}
      </div>
    </PageContainer>
  );
}

// ------------------------------------------------------------------- upload
function UploadPanel({
  disabled,
  onUploaded,
}: {
  disabled: boolean;
  onUploaded: (analysis: SvAnalysis) => void;
}) {
  const qc = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [cameraCode, setCameraCode] = useState("");
  const [error, setError] = useState<string | null>(null);

  // The camera list comes from the explicit SecureVision -> JNPA mapping. When
  // nothing is mapped the operator can still type the vendor's own code, but the
  // screen says the mapping is missing rather than silently attributing the clip.
  const mapQ = useQuery({
    queryKey: ["sv", "cameras"],
    queryFn: () => api.svCameraMap(),
    staleTime: 60_000,
    retry: false,
  });
  const mapped = mapQ.data?.cameras ?? [];

  const upload = useMutation({
    mutationFn: () => getAdapter().svUploadVideo(file as File, cameraCode.trim()),
    onSuccess: (analysis) => {
      void qc.invalidateQueries({ queryKey: svKeys.analyses });
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
      onUploaded(analysis);
    },
    onError: (e) => setError(svErrorMessage(e)),
  });

  const canUpload = Boolean(file && cameraCode.trim() && !upload.isPending && !disabled);

  return (
    <Card className="p-3">
      <div className="mb-2 flex items-center gap-2">
        <Upload className="h-4 w-4 text-muted-foreground" />
        <h3 className="text-sm font-semibold">Analyse a clip</h3>
        <SvSourceBadge />
      </div>

      <div className="flex flex-wrap items-end gap-2">
        <label className="block">
          <span className="text-[11px] font-medium text-muted-foreground">Video clip</span>
          <input
            id="sv-upload-input"
            ref={inputRef}
            type="file"
            accept="video/mp4,video/quicktime,video/x-matroska,video/webm,video/x-msvideo"
            disabled={disabled}
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null);
              setError(null);
            }}
            className="mt-0.5 block text-xs file:mr-2 file:rounded file:border file:border-border file:bg-background file:px-2 file:py-1 file:text-xs"
          />
        </label>

        {mapped.length > 0 ? (
          <FilterSelect
            label="Camera"
            value={cameraCode}
            onChange={setCameraCode}
            options={[
              { value: "", label: "Select camera…" },
              ...mapped.map((c) => ({
                value: c.securevision_code,
                label: `${c.jnpa_camera_id} (${c.securevision_code})`,
              })),
            ]}
          />
        ) : (
          <label className="block">
            <span className="text-[11px] font-medium text-muted-foreground">
              SecureVision camera code
            </span>
            <input
              value={cameraCode}
              onChange={(e) => setCameraCode(e.target.value)}
              placeholder="CAM-01"
              disabled={disabled}
              className="mt-0.5 h-9 w-40 rounded-md border border-border bg-background px-2 text-[13px] outline-none focus:ring-2 focus:ring-primary/20"
            />
          </label>
        )}

        <Button disabled={!canUpload} onClick={() => upload.mutate()}>
          {upload.isPending ? (
            <Spinner className="mr-2 h-3 w-3" />
          ) : (
            <Upload className="mr-2 h-3.5 w-3.5" />
          )}
          Upload &amp; analyse
        </Button>
      </div>

      {mapped.length === 0 && (
        <p className="mt-2 text-[10.5px] text-muted-foreground">
          No SecureVision camera mapping is configured, so detections cannot be attributed to a JNPA
          camera. Enter the vendor&apos;s own camera code — it must match a camera SecureVision
          knows, or zone-based person detection (I-07) will load no zones.
        </p>
      )}
      {error && (
        <p className="mt-2 text-xs" style={{ color: STATUS.critical }}>
          {error}
        </p>
      )}
      {upload.isPending && (
        <p className="mt-2 text-[10.5px] text-muted-foreground">
          Decoding and running one YOLOv11 detection pass. Large clips can take a minute.
        </p>
      )}
    </Card>
  );
}

// ------------------------------------------------------------- analysis list
function AnalysisList({
  analyses,
  selected,
  onSelect,
  isLoading,
  error,
  onRetry,
  note,
}: {
  analyses: SvAnalysis[];
  selected: string | null;
  onSelect: (id: string) => void;
  isLoading: boolean;
  error: unknown;
  onRetry: () => void;
  note?: string;
}) {
  const qc = useQueryClient();
  const remove = useMutation({
    mutationFn: (id: string) => getAdapter().svDeleteAnalysis(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: svKeys.analyses }),
  });

  const columns: Column<SvAnalysis>[] = useMemo(
    () => [
      { key: "file", header: "Clip", render: (a) => a.filename ?? a.analysis_id },
      {
        key: "camera",
        header: "Camera",
        render: (a) =>
          a.camera_mapped && a.jnpa_camera_id ? (
            <span className="font-mono text-xs">{a.jnpa_camera_id}</span>
          ) : (
            <span
              className="font-mono text-xs text-muted-foreground"
              title="Camera mapping unavailable — this clip cannot be attributed to a JNPA camera."
            >
              {a.securevision_camera_code ?? "—"}
            </span>
          ),
      },
      { key: "frames", header: "Frames", render: (a) => a.frames_sampled ?? "—" },
      {
        key: "zones",
        header: "Zones",
        render: (a) =>
          (a.zones_loaded ?? 0) > 0 ? (
            <StatusChip label={String(a.zones_loaded)} tone="ok" />
          ) : (
            <span title="SecureVision loaded no zones for this camera, so restricted-zone detection (I-07) cannot fire.">
              <StatusChip label="none" tone="warn" />
            </span>
          ),
      },
      { key: "when", header: "Uploaded", render: (a) => fmtDateTimeIST(a.uploaded_at) },
      { key: "by", header: "By", render: (a) => a.uploaded_by ?? "—" },
      {
        key: "actions",
        header: "",
        render: (a) => (
          <Button
            size="sm"
            variant="outline"
            disabled={remove.isPending}
            onClick={(e) => {
              e.stopPropagation();
              if (
                window.confirm(`Delete the cached analysis for ${a.filename ?? a.analysis_id}?`)
              ) {
                remove.mutate(a.analysis_id);
              }
            }}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        ),
      },
    ],
    [remove],
  );

  if (error) return <SvUnavailable error={error} onRetry={onRetry} />;

  return (
    <Card className="p-3">
      <div className="mb-2 flex items-center gap-2">
        <FileVideo className="h-4 w-4 text-muted-foreground" />
        <h3 className="text-sm font-semibold">Analyses</h3>
        <SvSourceBadge />
        <span className="text-[10.5px] text-muted-foreground">
          {note ?? "Session-scoped — SecureVision publishes no incident history."}
        </span>
      </div>
      <DataTable
        columns={columns}
        rows={analyses}
        rowKey={(a) => a.analysis_id}
        status={{ isLoading, isFetching: false, isError: Boolean(error), error }}
        onRetry={onRetry}
        onRowClick={(a) => onSelect(a.analysis_id)}
        isRowActive={(a) => a.analysis_id === selected}
        emptyLabel="No clip analysed yet in this session."
        pageSize={5}
      />
    </Card>
  );
}

function AnalysisSummary({ analysis }: { analysis: SvAnalysis }) {
  return (
    <StatGrid>
      <StatCard icon={FileVideo} label="Clip" value={analysis.filename ?? "—"} tone="info" />
      <StatCard
        icon={Camera}
        label="Camera"
        value={analysis.jnpa_camera_id ?? analysis.securevision_camera_code ?? "—"}
        tone={analysis.camera_mapped ? "ok" : "warn"}
        sub={analysis.camera_mapped ? undefined : "Camera mapping unavailable"}
      />
      <StatCard
        icon={Film}
        label="Frames sampled"
        value={analysis.frames_sampled ?? "—"}
        tone="neutral"
      />
      <StatCard
        icon={ShieldAlert}
        label="Zones loaded"
        value={analysis.zones_loaded ?? 0}
        tone={(analysis.zones_loaded ?? 0) > 0 ? "ok" : "warn"}
        sub={(analysis.zones_loaded ?? 0) > 0 ? undefined : "I-07 cannot fire without zones"}
      />
    </StatGrid>
  );
}
