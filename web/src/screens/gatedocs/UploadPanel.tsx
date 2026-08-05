// Gate-document Data-Upload panel (EIR / PIN ticket / Form-13).
//
// Same three-step workflow as the CFS-ECY and shipping-lines panels — download
// template → validate (dry-run preview) → confirm import → history — so the
// operator learns one flow for every module. Backed by /api/gate-docs/*.
//
// Uses the shared DTCCC kit (Card / Button / FilterSelect / StatusChip /
// Loading-Error-Empty states) and semantic theme tokens only.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Download, FileUp, Inbox, UploadCloud } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { FilterSelect, StatusChip, type Tone } from "@/components/ui/dtccc";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/misc";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";

import { api } from "../../lib/api";

const DOC_TYPES = [
  { value: "EIR", label: "EIR (Equipment Interchange Report)" },
  { value: "PIN", label: "PIN ticket" },
  { value: "FORM13", label: "Form 13" },
];

function statusTone(s: string): Tone {
  if (s === "SUCCESS" || s === "VALIDATED" || s === "EXTRACTED") return "ok";
  if (s === "FAILED" || s === "REJECTED") return "critical";
  if (s === "PARTIAL") return "warn";
  return "neutral";
}

function isImageFile(f: File | null): boolean {
  if (!f) return false;
  if ((f.type || "").toLowerCase().startsWith("image/")) return true;
  return /\.(jpe?g|png|webp|tif{1,2}|bmp|gif)$/i.test(f.name);
}

/** Map gate-doc type → /api/ocr doc_type for the eir_ocr sidecar. */
function ocrDocType(gateDocType: string): string {
  if (gateDocType === "FORM13") return "FORM13";
  if (gateDocType === "PIN") return "GATE_SLIP";
  return "EIR";
}

export default function GateDocUploadPanel() {
  const qc = useQueryClient();
  const [docType, setDocType] = useState("EIR");
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<Record<string, any> | null>(null);
  const [ocrResult, setOcrResult] = useState<Record<string, any> | null>(null);
  // Per-file drill-down: the ledger listed uploads but no panel could open one,
  // so an operator saw an error COUNT with no way to read the reasons.
  const [openFileId, setOpenFileId] = useState<number | null>(null);

  const imageMode = isImageFile(file);

  const detailQ = useQuery({
    queryKey: ["gate-doc-upload-detail", openFileId],
    queryFn: () => api.gateDocUploadDetail(openFileId as number),
    enabled: openFileId !== null,
  });

  const historyQ = useQuery({
    queryKey: ["gate-doc-uploads", docType],
    queryFn: () => api.gateDocUploads({ doc_type: docType, limit: 20 }),
  });

  const validate = useMutation({
    mutationFn: () => api.gateDocUploadValidate(docType, file as File),
    onSuccess: setPreview,
  });

  const upload = useMutation({
    mutationFn: () => api.gateDocUpload(docType, file as File),
    onSuccess: () => {
      setPreview(null);
      setFile(null);
      qc.invalidateQueries({ queryKey: ["gate-doc-uploads"] });
      qc.invalidateQueries({ queryKey: ["uc3-docs"] });
    },
  });

  const ocrUpload = useMutation({
    mutationFn: () =>
      api.ocrUpload(file as File, ocrDocType(docType), file?.name),
    onSuccess: (data) => {
      setOcrResult(data);
      setPreview(null);
      setFile(null);
      qc.invalidateQueries({ queryKey: ["ocr-documents"] });
    },
  });

  return (
    <div className="flex flex-col gap-3 sm:gap-4">
      <Card>
        <CardHeader className="flex-row items-center gap-2">
          <UploadCloud className="h-4 w-4 text-muted-foreground" aria-hidden />
          <CardTitle>Upload gate documents</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1">
            <span className="text-[11px] font-medium text-muted-foreground">Document type</span>
            <FilterSelect
              value={docType}
              onChange={(v) => {
                setDocType(v);
                setPreview(null);
                setOcrResult(null);
              }}
              options={DOC_TYPES}
              label="Document type"
            />
          </label>

          {!imageMode && (
            <Button variant="outline" size="sm" onClick={() => api.gateDocDownloadTemplate(docType)}>
              <Download className="h-3.5 w-3.5" />
              Template
            </Button>
          )}

          <label className="flex flex-col gap-1">
            <span className="text-[11px] font-medium text-muted-foreground">
              File (PNG / JPG · CSV / XLS / XLSX)
            </span>
            <input
              type="file"
              accept=".csv,.xls,.xlsx,image/png,image/jpeg,image/jpg,image/webp,.png,.jpg,.jpeg,.webp"
              onChange={(e) => {
                setFile(e.target.files?.[0] ?? null);
                setPreview(null);
                setOcrResult(null);
              }}
              className="h-9 max-w-[16rem] rounded-md border border-border bg-background px-2 py-1.5 text-[13px] text-foreground file:mr-2 file:rounded file:border-0 file:bg-muted file:px-2 file:py-1 file:text-xs file:font-medium file:text-foreground"
            />
          </label>

          {imageMode ? (
            <Button
              size="sm"
              disabled={!file || ocrUpload.isPending}
              onClick={() => ocrUpload.mutate()}
            >
              {ocrUpload.isPending ? "Running eir-ocr…" : "Extract with eir-ocr"}
            </Button>
          ) : (
            <>
              <Button
                variant="subtle"
                size="sm"
                disabled={!file || validate.isPending}
                onClick={() => validate.mutate()}
              >
                {validate.isPending ? "Validating…" : "Validate"}
              </Button>

              <Button
                size="sm"
                disabled={!preview?.valid || upload.isPending}
                onClick={() => upload.mutate()}
              >
                {upload.isPending ? "Importing…" : "Import"}
              </Button>
            </>
          )}
        </CardContent>
        {imageMode && (
          <CardContent className="pt-0 text-[11px] text-muted-foreground">
            Image selected — PNG/JPG gate slips are OCR’d by the eir_ocr service
            (<span className="font-mono"> ingest/eir_ocr</span>), not the CSV importer.
          </CardContent>
        )}
      </Card>

      {(validate.isError || upload.isError || ocrUpload.isError) && (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-lg border border-severity-critical/40 bg-severity-critical/10 px-3 py-2 text-xs text-foreground"
        >
          <AlertTriangle
            className="mt-0.5 h-3.5 w-3.5 shrink-0 text-severity-critical"
            aria-hidden
          />
          <span>
            {String(
              ((validate.error || upload.error || ocrUpload.error) as Error).message,
            )}
          </span>
        </div>
      )}

      {ocrUpload.isSuccess && ocrResult && (
        <Card className="overflow-hidden">
          <CardHeader className="flex-row flex-wrap items-center gap-2 border-b border-border">
            <CheckCircle2 className="h-4 w-4 text-severity-ok" aria-hidden />
            <CardTitle>eir-ocr extraction</CardTitle>
            <StatusChip
              label={String(ocrResult.source ?? ocrResult.status ?? "EXTRACTED")}
              tone={statusTone(String(ocrResult.status ?? "EXTRACTED"))}
            />
            {ocrResult.engine?.service && (
              <span className="text-[11px] text-muted-foreground">
                via {ocrResult.engine.service}
              </span>
            )}
          </CardHeader>
          <CardContent className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {Object.entries(ocrResult.fields || {}).map(([k, v]) => (
              <div key={k} className="rounded-md border border-border/60 bg-muted/30 px-2.5 py-1.5">
                <div className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                  {k}
                </div>
                <div className="font-mono text-[13px] font-medium text-foreground">
                  {v == null || v === "" ? "—" : String(v)}
                </div>
              </div>
            ))}
            {Object.keys(ocrResult.fields || {}).length === 0 && (
              <p className="text-xs text-muted-foreground sm:col-span-2">
                No structured fields — check raw OCR on Document OCR tab.
              </p>
            )}
          </CardContent>
        </Card>
      )}

      {upload.isSuccess && (
        <div className="flex items-start gap-2 rounded-lg border border-severity-ok/40 bg-severity-ok/10 px-3 py-2 text-xs text-foreground">
          <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-severity-ok" aria-hidden />
          <span>
            {upload.data.status}: imported {upload.data.imported}, skipped {upload.data.skipped},
            invalid {upload.data.invalid}
          </span>
        </div>
      )}

      {preview && (
        <Card className="overflow-hidden">
          <CardHeader className="flex-row flex-wrap items-center gap-2 border-b border-border">
            <FileUp className="h-4 w-4 text-muted-foreground" aria-hidden />
            <CardTitle>Validation preview</CardTitle>
            <StatusChip label={preview.status} tone={statusTone(preview.status)} />
            <span className="text-[11px] text-muted-foreground">
              {preview.summary?.valid}/{preview.summary?.rows} rows importable
            </span>
          </CardHeader>

          {preview.preview?.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-[12px]">
                <thead className="border-b border-border bg-muted/40 text-[11px] uppercase tracking-wide text-muted-foreground">
                  <tr>
                    {Object.keys(preview.preview[0]).map((k) => (
                      <th key={k} className="px-2.5 py-1.5 font-semibold">
                        {k}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {preview.preview.map((row: Record<string, unknown>, i: number) => (
                    <tr key={i} className="hover:bg-muted/40">
                      {Object.values(row).map((v, j) => (
                        <td key={j} className="px-2.5 py-1.5 font-mono text-foreground">
                          {String(v)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {(preview.errors?.length > 0 || preview.warnings?.length > 0) && (
            <CardContent className="space-y-1">
              {preview.errors?.slice(0, 10).map((e: Record<string, any>, i: number) => (
                <div key={`e${i}`} className="flex items-start gap-1.5 text-[11px]">
                  <AlertTriangle
                    className="mt-0.5 h-3 w-3 shrink-0 text-severity-critical"
                    aria-hidden
                  />
                  <span className="text-foreground">
                    {e.row_number ? `row ${e.row_number}: ` : ""}
                    {e.error_detail}
                  </span>
                </div>
              ))}
              {preview.warnings?.slice(0, 10).map((w: Record<string, any>, i: number) => (
                <div key={`w${i}`} className="flex items-start gap-1.5 text-[11px]">
                  <AlertTriangle
                    className="mt-0.5 h-3 w-3 shrink-0 text-severity-warning"
                    aria-hidden
                  />
                  <span className="text-muted-foreground">
                    {w.row_number ? `row ${w.row_number}: ` : ""}
                    {w.error_detail}
                  </span>
                </div>
              ))}
            </CardContent>
          )}
        </Card>
      )}

      <Card className="overflow-hidden">
        <CardHeader className="flex-row items-center gap-2 border-b border-border">
          <CardTitle>Import history</CardTitle>
        </CardHeader>
        {historyQ.isLoading ? (
          <LoadingState />
        ) : historyQ.isError ? (
          <ErrorState onRetry={() => historyQ.refetch()} />
        ) : (historyQ.data?.items?.length ?? 0) === 0 ? (
          <EmptyState>
            <div className="flex flex-col items-center gap-2">
              <Inbox className="h-6 w-6 text-muted-foreground" aria-hidden />
              <div className="font-medium text-foreground">No uploads yet</div>
              <p className="text-xs text-muted-foreground">
                Download the template, fill it in, then validate and import.
              </p>
            </div>
          </EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[12px]">
              <thead className="border-b border-border bg-muted/40 text-[11px] uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-semibold">File</th>
                  <th className="px-3 py-2 font-semibold">Type</th>
                  <th className="px-3 py-2 text-right font-semibold">Rows</th>
                  <th className="px-3 py-2 text-right font-semibold">Imported</th>
                  <th className="px-3 py-2 font-semibold">Status</th>
                  <th className="px-3 py-2 font-semibold">When</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {historyQ.data?.items?.map((f: Record<string, any>) => (
                  <tr
                    key={f.id}
                    onClick={() => setOpenFileId(Number(f.id))}
                    className="cursor-pointer hover:bg-muted/40"
                  >
                    <td className="px-3 py-2 font-mono text-foreground">{f.source_file}</td>
                    <td className="px-3 py-2 text-muted-foreground">{f.doc_type}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{f.record_count}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{f.imported_count}</td>
                    <td className="px-3 py-2">
                      <StatusChip label={f.import_status} tone={statusTone(f.import_status)} />
                    </td>
                    <td className="px-3 py-2 text-muted-foreground">
                      {f.created_at ? new Date(f.created_at).toLocaleString() : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Dialog open={openFileId !== null} onOpenChange={(o) => !o && setOpenFileId(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>Upload #{openFileId}</DialogTitle>
          </DialogHeader>
          {detailQ.isLoading ? (
            <LoadingState />
          ) : detailQ.isError ? (
            <ErrorState onRetry={() => detailQ.refetch()} />
          ) : (
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-3">
                {(
                  [
                    ["File", detailQ.data?.source_file],
                    ["Type", detailQ.data?.doc_type],
                    ["Status", detailQ.data?.import_status],
                    ["Rows", detailQ.data?.record_count],
                    ["Imported", detailQ.data?.imported_count],
                    ["Errors", detailQ.data?.error_count],
                  ] as [string, unknown][]
                ).map(([k, v]) => (
                  <div key={k}>
                    <div className="text-muted-foreground">{k}</div>
                    <div className="font-medium text-foreground">{String(v ?? "—")}</div>
                  </div>
                ))}
              </div>

              {(detailQ.data?.errors?.length ?? 0) === 0 ? (
                <EmptyState>No row errors recorded for this file.</EmptyState>
              ) : (
                <div className="max-h-72 overflow-y-auto rounded-lg border border-border">
                  <table className="w-full text-left text-[12px]">
                    <thead className="border-b border-border bg-muted/40 text-[11px] uppercase text-muted-foreground">
                      <tr>
                        <th className="px-2.5 py-1.5 font-semibold">Row</th>
                        <th className="px-2.5 py-1.5 font-semibold">Code</th>
                        <th className="px-2.5 py-1.5 font-semibold">Detail</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {detailQ.data?.errors?.map((e: Record<string, any>) => (
                        <tr key={e.id}>
                          <td className="px-2.5 py-1.5 text-muted-foreground">
                            {e.record_ref ?? "—"}
                          </td>
                          <td className="px-2.5 py-1.5 font-mono text-severity-critical">
                            {e.error_code}
                          </td>
                          <td className="px-2.5 py-1.5 text-foreground">{e.error_detail}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
