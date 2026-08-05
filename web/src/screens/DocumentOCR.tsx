// Document OCR (Feature 6) — upload EIR / gate-slip photos (PNG/JPG) and other
// transport documents. LIVE extraction goes through the dedicated eir_ocr
// sidecar (ingest/eir_ocr → GATEWAY_EIR_OCR_URL /infer); falls back to local
// Tesseract then MOCK. Backed by /api/ocr/*.
//
//   Upload → eir_ocr structured fields (+ extras) → browse the document log.

import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ScanText, Upload, FileSearch, CheckCircle2, X } from "lucide-react";
import { api } from "@/lib/api";
import { PageContainer, PageHeader, StatusChip } from "@/components/ui/dtccc";
import { Card } from "@/components/ui/card";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EmptyState, LoadingState } from "@/components/ui/misc";
import { fmtDateTimeIST } from "@/lib/utils";

/** EIR first — PNG/JPG gate slips are the primary eir_ocr workload. */
const DOC_TYPES = [
  "EIR",
  "GATE_SLIP",
  "FORM13",
  "LR",
  "INVOICE",
  "EWAYBILL",
  "PERMIT",
  "RC",
  "DL",
  "UNKNOWN",
];

function pct(v: any): string {
  const n = Number(v);
  if (!isFinite(n)) return "—";
  return `${Math.round(n <= 1 ? n * 100 : n)}%`;
}

function sourceKind(source: any): "service" | "ocr" | "mock" | "unknown" {
  const s = String(source ?? "").toLowerCase();
  if (s === "ocr_service" || s === "eir-ocr" || s === "eir_ocr") return "service";
  if (s === "mock") return "mock";
  if (s === "ocr" || s === "tesseract") return "ocr";
  return "unknown";
}

function statusTone(status: any): "ok" | "warn" | "critical" | "neutral" {
  const s = String(status ?? "").toLowerCase();
  if (s === "ok" || s === "done" || s === "processed" || s === "verified" || s === "extracted")
    return "ok";
  if (s === "pending" || s === "processing") return "warn";
  if (s === "error" || s === "failed") return "critical";
  return "neutral";
}

function SourceBadge({ source }: { source: any }) {
  const kind = sourceKind(source);
  if (kind === "mock") {
    return (
      <span className="inline-flex items-center rounded-full bg-amber-500/15 px-2 py-0.5 text-[11px] font-semibold text-amber-600 dark:text-amber-400">
        MOCK
      </span>
    );
  }
  if (kind === "service") {
    return (
      <span className="inline-flex items-center rounded-full bg-emerald-500/15 px-2 py-0.5 text-[11px] font-semibold text-emerald-600 dark:text-emerald-400">
        eir-ocr
      </span>
    );
  }
  return (
    <span className="inline-flex items-center rounded-full bg-sky-500/15 px-2 py-0.5 text-[11px] font-semibold text-sky-700 dark:text-sky-400">
      OCR
    </span>
  );
}

function FieldGrid({ fields }: { fields: any }) {
  const entries = Object.entries(fields || {});
  if (!entries.length)
    return <div className="text-[12px] text-muted-foreground">No fields extracted.</div>;
  return (
    <dl className="grid grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-2">
      {entries.map(([k, v]) => (
        <div
          key={k}
          className="min-w-0 rounded-md border border-border/60 bg-muted/30 px-2.5 py-1.5"
        >
          <dt className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
            {k.replace(/[_-]+/g, " ")}
          </dt>
          <dd className="break-words text-[13px] font-medium text-foreground">
            {v == null || v === "" ? "—" : typeof v === "object" ? JSON.stringify(v) : String(v)}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function ValidationStrip({ validation }: { validation: Record<string, any> | undefined }) {
  const entries = Object.entries(validation || {});
  if (!entries.length) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {entries.map(([k, v]) => {
        const ok = Boolean(v?.valid);
        return (
          <span
            key={k}
            className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-semibold ${
              ok
                ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400"
                : "bg-amber-500/15 text-amber-700 dark:text-amber-400"
            }`}
            title={v?.normalised ? `normalised: ${v.normalised}` : undefined}
          >
            {k}: {ok ? "valid" : "check"}
          </span>
        );
      })}
    </div>
  );
}

export default function DocumentOCR() {
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [docType, setDocType] = useState<string>("EIR");
  const [sourceRef, setSourceRef] = useState("");
  const [filterType, setFilterType] = useState("");
  const [viewId, setViewId] = useState<number | null>(null);

  const healthQ = useQuery({ queryKey: ["ocr-health"], queryFn: () => api.ocrHealth() });
  const docsQ = useQuery({
    queryKey: ["ocr-documents", filterType],
    queryFn: () => api.ocrDocuments({ doc_type: filterType || undefined, limit: 100 }),
  });

  const upload = useMutation({
    mutationFn: () => {
      if (!file) throw new Error("No file selected");
      return api.ocrUpload(file, docType, sourceRef.trim() || undefined);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["ocr-documents"] });
    },
  });

  const activeRung = String(healthQ.data?.active_rung ?? "");
  const upstreamReady = Boolean(healthQ.data?.upstream?.engine_ready);
  const mockEngine = !upstreamReady && String(healthQ.data?.engine ?? "").toLowerCase() === "mock";
  const result = upload.data;

  function clearFile() {
    setFile(null);
    if (fileRef.current) fileRef.current.value = "";
  }

  const engineLabel = upstreamReady
    ? "eir-ocr"
    : mockEngine
      ? "mock"
      : String(healthQ.data?.engine || "tesseract");

  return (
    <PageContainer>
      <PageHeader
        icon={ScanText}
        title="Document OCR"
        subtitle="EIR / gate-slip photos via eir_ocr — PNG · JPG · structured ContainerNo · LICNo · EIRNo"
        updatedAt={docsQ.dataUpdatedAt}
        isFetching={docsQ.isFetching && !docsQ.isLoading}
        onRefresh={() => qc.invalidateQueries({ queryKey: ["ocr-documents"] })}
        actions={
          healthQ.isLoading ? (
            <span className="text-[11px] text-muted-foreground">checking engine…</span>
          ) : mockEngine ? (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-500/15 px-2.5 py-1 text-[11px] font-semibold text-amber-600 dark:text-amber-400">
              MOCK OCR
            </span>
          ) : (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-500/15 px-2.5 py-1 text-[11px] font-semibold text-emerald-600 dark:text-emerald-400">
              {upstreamReady ? "eir-ocr LIVE" : "Tesseract"}
            </span>
          )
        }
      />

      <div className="space-y-3 px-4 py-3">
        <Card className="flex flex-wrap items-center gap-3 p-3 text-[13px]">
          <span className="font-semibold">OCR Engine</span>
          <span className="font-mono">{engineLabel}</span>
          {activeRung && (
            <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px]">
              {activeRung}
            </span>
          )}
          <span className="text-muted-foreground">
            {healthQ.data?.configured ? "DB configured" : "DB not configured"}
          </span>
          {healthQ.data?.upstream?.url && (
            <span className="truncate text-[11px] text-muted-foreground" title={healthQ.data.upstream.url}>
              upstream {healthQ.data.upstream.reachable ? "reachable" : "down"}
              {healthQ.data.upstream.tesseract_version
                ? ` · tesseract ${healthQ.data.upstream.tesseract_version}`
                : ""}
            </span>
          )}
          {healthQ.isError && (
            <span className="text-[12px] text-severity-critical">engine unreachable</span>
          )}
        </Card>

        <Card className="p-4">
          <div className="mb-3 flex items-center gap-2">
            <Upload size={15} />
            <h3 className="text-sm font-semibold">Upload a document</h3>
          </div>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <label className="flex flex-col gap-1 text-[11px] text-muted-foreground">
              File (PNG / JPG / PDF)
              <input
                ref={fileRef}
                type="file"
                accept="image/png,image/jpeg,image/jpg,image/webp,image/*,application/pdf"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                className="rounded-md border border-border bg-card px-2 py-1.5 text-[13px] text-foreground file:mr-2 file:rounded file:border-0 file:bg-muted file:px-2 file:py-1 file:text-[12px]"
              />
            </label>
            <label className="flex flex-col gap-1 text-[11px] text-muted-foreground">
              Document type
              <select
                value={docType}
                onChange={(e) => setDocType(e.target.value)}
                className="rounded-md border border-border bg-card px-2 py-1.5 text-[13px] text-foreground"
              >
                {DOC_TYPES.map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-[11px] text-muted-foreground">
              Source reference (optional)
              <input
                value={sourceRef}
                onChange={(e) => setSourceRef(e.target.value)}
                placeholder="e.g. WhatsApp EIR photo"
                className="rounded-md border border-border bg-card px-2 py-1.5 text-[13px] text-foreground outline-none"
              />
            </label>
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              disabled={!file || upload.isPending}
              onClick={() => upload.mutate()}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              <ScanText className="h-3.5 w-3.5" />
              {upload.isPending ? "Extracting…" : "Extract with eir-ocr"}
            </button>
            {file && (
              <span className="inline-flex items-center gap-1.5 text-[12px] text-muted-foreground">
                <span className="font-mono">{file.name}</span>
                <button
                  onClick={clearFile}
                  className="text-muted-foreground hover:text-foreground"
                  title="Clear file"
                >
                  <X size={13} />
                </button>
              </span>
            )}
          </div>

          {upload.isError && (
            <div className="mt-2 text-[12px] text-severity-critical">
              {(upload.error as Error)?.message ?? "Upload failed."}
            </div>
          )}

          {result && (
            <div className="mt-4 rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-3">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <CheckCircle2 className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />
                <span className="text-[13px] font-semibold">Extraction complete</span>
                <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px]">
                  {result.doc_type ?? docType}
                </span>
                <SourceBadge source={result.source} />
                <span className="ml-auto text-[12px] text-muted-foreground">
                  confidence <strong className="text-foreground">{pct(result.confidence)}</strong>
                </span>
              </div>
              <FieldGrid fields={result.fields} />
              <ValidationStrip validation={result.validation} />
              {result.extras && Object.keys(result.extras).length > 0 && (
                <details className="mt-3">
                  <summary className="cursor-pointer text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                    Unmapped OCR labels ({Object.keys(result.extras).length}) — not in verify report
                  </summary>
                  <div className="mt-2">
                    <FieldGrid fields={result.extras} />
                  </div>
                </details>
              )}
              {result.status && (
                <div className="mt-2 flex items-center gap-2 text-[11px] text-muted-foreground">
                  status{" "}
                  <StatusChip label={String(result.status)} tone={statusTone(result.status)} />
                  {result.engine?.service && (
                    <span className="font-mono">
                      via {result.engine.service}
                      {result.engine.tesseract_version
                        ? ` · ${result.engine.tesseract_version}`
                        : ""}
                    </span>
                  )}
                  {result.storage_url && (
                    <a
                      href={result.storage_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-severity-info hover:underline"
                    >
                      stored file
                    </a>
                  )}
                </div>
              )}
            </div>
          )}
        </Card>

        <Card className="p-4">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <FileSearch size={15} />
            <h3 className="text-sm font-semibold">Processed documents</h3>
            <span className="text-[11px] text-muted-foreground">({docsQ.data?.count ?? 0})</span>
            <select
              value={filterType}
              onChange={(e) => setFilterType(e.target.value)}
              className="ml-auto rounded-md border border-border bg-card px-2 py-1 text-[12px] text-foreground"
            >
              <option value="">All types</option>
              {DOC_TYPES.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
          </div>

          {docsQ.isLoading ? (
            <LoadingState />
          ) : !docsQ.data?.documents?.length ? (
            <EmptyState>No documents processed yet — upload an EIR photo above.</EmptyState>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] border-collapse text-[12px]">
                <thead>
                  <tr className="text-left text-muted-foreground">
                    <th className="py-1 pr-3 font-medium">When</th>
                    <th className="py-1 pr-3 font-medium">Type</th>
                    <th className="py-1 pr-3 font-medium">Engine</th>
                    <th className="py-1 pr-3 font-medium">Source ref</th>
                    <th className="py-1 pr-3 font-medium">Confidence</th>
                    <th className="py-1 pr-3 font-medium">Status</th>
                    <th className="py-1 pr-3 font-medium text-right">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {docsQ.data.documents.map((d: any) => (
                    <tr key={d.id} className="border-t border-border align-top">
                      <td className="whitespace-nowrap py-1.5 pr-3 text-muted-foreground">
                        {d.ts ? fmtDateTimeIST(d.ts) : "—"}
                      </td>
                      <td className="py-1.5 pr-3">
                        <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px]">
                          {d.doc_type ?? "—"}
                        </span>
                      </td>
                      <td className="py-1.5 pr-3">
                        <SourceBadge source={d.source} />
                      </td>
                      <td className="py-1.5 pr-3 font-mono">{d.source_ref ?? "—"}</td>
                      <td className="py-1.5 pr-3 tabular-nums">{pct(d.confidence)}</td>
                      <td className="py-1.5 pr-3">
                        <StatusChip label={String(d.status ?? "—")} tone={statusTone(d.status)} />
                      </td>
                      <td className="py-1.5 pr-3 text-right">
                        <button
                          onClick={() => setViewId(d.id)}
                          className="text-[12px] font-medium text-severity-info hover:underline"
                        >
                          View
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>

      <DocumentDrawer id={viewId} onClose={() => setViewId(null)} />
    </PageContainer>
  );
}

function DocumentDrawer({ id, onClose }: { id: number | null; onClose: () => void }) {
  const q = useQuery({
    queryKey: ["ocr-document", id],
    queryFn: () => api.ocrDocument(id as number),
    enabled: id != null,
  });
  const payload = q.data;
  const d = payload?.document ?? payload;
  return (
    <Dialog open={id != null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader className="flex items-center gap-3">
          <ScanText className="h-5 w-5 shrink-0 text-muted-foreground" aria-hidden />
          <DialogTitle className="flex min-w-0 flex-col gap-0.5">
            <span className="truncate text-base font-semibold leading-tight">
              {d?.doc_type ?? "Document"} #{id ?? ""}
            </span>
            {d && (
              <span className="font-mono text-xs font-medium text-muted-foreground">
                {d.source_ref ?? "—"}
              </span>
            )}
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-4 p-5">
          {q.isLoading ? (
            <LoadingState />
          ) : q.isError || !d ? (
            <EmptyState>Could not load this document.</EmptyState>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-2 text-[12px] text-muted-foreground">
                <SourceBadge source={d.source} />
                <StatusChip label={String(d.status ?? "—")} tone={statusTone(d.status)} />
                <span>
                  confidence <strong className="text-foreground">{pct(d.confidence)}</strong>
                </span>
                {d.ts && <span className="ml-auto">{fmtDateTimeIST(d.ts)}</span>}
              </div>

              <div>
                <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                  Extracted fields (eir_ocr)
                </div>
                <FieldGrid fields={d.fields} />
              </div>
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
