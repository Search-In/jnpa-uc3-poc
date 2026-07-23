// Performance Data Upload (module 12 sub-module) — admin-only.
// Pick the official JNPA report PDF (or a CSV/XLSX built from the template) →
// Validate (preview + errors) → Import (atomic) → dashboard refresh. All data flows
// through /api/performance/* — nothing is mocked; the preview and history come
// straight from the backend, which parses the PDF with the same validated engine
// used for the offline backfill.
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, FileUp, CheckCircle2, AlertTriangle, Loader2, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import { Card } from "@/components/ui/card";
import {
  StatGrid,
  StatCard,
  StatusChip,
  FilterSelect,
  DataTable,
  type Column,
  type Tone,
} from "@/components/ui/dtccc";

const REPORT_TYPES = [
  { value: "daily_status", label: "Daily Status Report" },
  { value: "monthly_teu", label: "Monthly TEU Report" },
  { value: "ldb_report", label: "LDB Report" },
];
// What the official JNPA PDF for each report type looks like, so an operator can tell
// at a glance whether they have picked the right file for the selected report type.
const PDF_HINT: Record<string, string> = {
  daily_status: "Daily Status Report — e.g. “Daily Status Report 26.05.2026.pdf”",
  monthly_teu: "Monthwise TEUs handled in JNPA — e.g. “FY_2026-27_JN_Port_TEUs_April.pdf”",
  ldb_report: "NLDS/LDB Analytics Report — e.g. “NLDS_LDB_Full Analysis_March_2026.pdf”",
};
const ACCEPT = ".pdf,.csv,.xlsx";
const MAX_MB = 25;

// The API answers 4xx with {detail:{error,detail}}; surface that instead of "[object Object]".
function errText(e: unknown): string {
  const d = (e as any)?.detail ?? (e as any)?.response?.data?.detail ?? e;
  if (d && typeof d === "object")
    return [d.error, d.detail].filter(Boolean).join(" — ") || JSON.stringify(d);
  return String(d ?? "Upload failed");
}
const BTN =
  "inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm font-medium text-foreground transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50";
const BTN_PRIMARY =
  "inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-semibold text-primary-foreground transition-colors hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50";

function statusTone(s?: string): Tone {
  return s === "IMPORTED"
    ? "ok"
    : s === "VALIDATED"
      ? "info"
      : s === "FAILED" || s === "REJECTED"
        ? "critical"
        : "neutral";
}

export default function UploadPanel() {
  const qc = useQueryClient();
  const [reportType, setReportType] = useState("daily_status");
  const [file, setFile] = useState<File | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [validation, setValidation] = useState<any | null>(null);
  const [importResult, setImportResult] = useState<any | null>(null);

  const historyQ = useQuery({
    queryKey: ["perf-uploads"],
    queryFn: () => api.perfUploads({ limit: 25 }),
  });

  const [pickError, setPickError] = useState<string | null>(null);

  const reset = () => {
    setValidation(null);
    setImportResult(null);
    setPickError(null);
  };
  // Client-side pre-checks so an obviously wrong file is caught before the round-trip.
  // The server re-validates by content (magic bytes) regardless — this is UX, not a gate.
  const pickFile = (f: File | null) => {
    reset();
    if (f && !/\.(pdf|csv|xlsx|xlsm)$/i.test(f.name)) {
      setFile(null);
      setPickError(`“${f.name}” is not a supported file — upload the official JNPA report PDF, or a CSV/XLSX built from the template.`);
      return;
    }
    if (f && f.size > MAX_MB * 1024 * 1024) {
      setFile(null);
      setPickError(`“${f.name}” is ${(f.size / 1024 / 1024).toFixed(1)} MB — the limit is ${MAX_MB} MB.`);
      return;
    }
    setFile(f);
  };

  const validateMut = useMutation({
    mutationFn: () => api.perfUploadValidate(reportType, file as File),
    onSuccess: (res) => {
      setValidation(res);
      setImportResult(null);
      qc.invalidateQueries({ queryKey: ["perf-uploads"] });
    },
  });
  const importMut = useMutation({
    mutationFn: () => api.perfUploadImport(reportType, file as File),
    onSuccess: (res) => {
      setImportResult(res);
      // refresh upload history AND the live dashboard (all perf-* read queries)
      qc.invalidateQueries({
        predicate: (q) => Array.isArray(q.queryKey) && String(q.queryKey[0]).startsWith("perf-"),
      });
    },
  });

  const errors: any[] = (validation?.errors ?? []).map((e: any, i: number) => ({ _k: i, ...e }));
  const warnings: any[] = validation?.warnings ?? [];
  const preview: any[] = (validation?.preview ?? []).map((r: any, i: number) => ({ _k: i, ...r }));
  const canImport = !!file && validation?.valid === true && !importResult;
  const busy = validateMut.isPending || importMut.isPending;

  const previewCols: Column<any>[] =
    preview.length > 0
      ? Object.keys(preview[0])
          .filter((k) => k !== "_k")
          .slice(0, 8)
          .map((k) => ({ key: k, header: k, render: (r) => String(r[k] ?? "—") }))
      : [];
  const errorCols: Column<any>[] = [
    { key: "row_number", header: "Row", align: "right", render: (r) => r.row_number ?? "—" },
    { key: "column_name", header: "Column", render: (r) => r.column_name ?? "—" },
    {
      key: "error_code",
      header: "Code",
      render: (r) => <StatusChip label={r.error_code} tone="critical" />,
    },
    { key: "error_detail", header: "Detail", render: (r) => r.error_detail ?? "—" },
    {
      key: "raw_value",
      header: "Value",
      render: (r) => <span className="font-mono">{r.raw_value ?? "—"}</span>,
    },
  ];
  const histCols: Column<any>[] = [
    {
      key: "created_at",
      header: "When",
      render: (r) => (r.created_at ? String(r.created_at).replace("T", " ").slice(0, 16) : "—"),
    },
    { key: "report_type", header: "Type" },
    {
      key: "original_filename",
      header: "File",
      render: (r) => <span className="font-mono">{r.original_filename}</span>,
    },
    {
      key: "file_format",
      header: "Format",
      render: (r) => <StatusChip label={r.file_format ?? "—"} tone="neutral" />,
    },
    {
      key: "status",
      header: "Status",
      render: (r) => <StatusChip label={r.status} tone={statusTone(r.status)} />,
    },
    { key: "row_count", header: "Rows", align: "right", render: (r) => r.row_count ?? 0 },
    {
      key: "inserted_count",
      header: "Inserted",
      align: "right",
      render: (r) => r.inserted_count ?? 0,
    },
    {
      key: "updated_count",
      header: "Refreshed",
      align: "right",
      render: (r) => r.updated_count ?? 0,
    },
    { key: "error_count", header: "Errors", align: "right", render: (r) => r.error_count ?? 0 },
    { key: "uploaded_by", header: "By" },
  ];

  return (
    <div className="flex flex-col gap-4">
      {/* Step 1 — pick type + template + file */}
      <Card className="p-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1">
            <label className="text-[12px] font-medium text-muted-foreground">Report type</label>
            <FilterSelect
              label="Report type"
              value={reportType}
              onChange={(v) => {
                setReportType(v);
                pickFile(null);
                if (fileRef.current) fileRef.current.value = "";
              }}
              options={REPORT_TYPES}
            />
          </div>
          <button
            type="button"
            className={BTN}
            onClick={() => api.perfDownloadTemplate(reportType)}
          >
            <Download size={15} /> Download template
          </button>
          <input
            ref={fileRef}
            type="file"
            accept={ACCEPT}
            className="hidden"
            onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
            aria-label="Choose report PDF or data file"
          />
          <button type="button" className={BTN} onClick={() => fileRef.current?.click()}>
            <FileUp size={15} /> {file ? "Change file" : "Choose file"}
          </button>
          {file && (
            <span className="text-[12px] text-muted-foreground">
              {file.name} ({(file.size / 1024).toFixed(1)} KB)
            </span>
          )}
          <div className="ml-auto flex items-center gap-2">
            <button
              type="button"
              className={BTN}
              disabled={!file || busy}
              onClick={() => validateMut.mutate()}
            >
              {validateMut.isPending ? (
                <Loader2 size={15} className="animate-spin" />
              ) : (
                <CheckCircle2 size={15} />
              )}{" "}
              Validate
            </button>
            <button
              type="button"
              className={BTN_PRIMARY}
              disabled={!canImport || busy}
              onClick={() => importMut.mutate()}
            >
              {importMut.isPending ? (
                <Loader2 size={15} className="animate-spin" />
              ) : (
                <FileUp size={15} />
              )}{" "}
              Import
            </button>
          </div>
        </div>
        <p className="mt-2 text-[12px] text-muted-foreground">
          Upload the official JNPA <strong>PDF</strong> — {PDF_HINT[reportType]} — and it is
          parsed exactly as published. A CSV/XLSX built from the template also works. Max{" "}
          {MAX_MB} MB. Re-uploading a corrected report replaces the figures for that
          reporting period.
        </p>
        {pickError && (
          <div className="mt-3 flex items-center gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-[13px] text-critical">
            <AlertTriangle size={15} /> {pickError}
          </div>
        )}
        {(validateMut.isError || importMut.isError) && (
          <div className="mt-3 flex items-center gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-[13px] text-critical">
            <AlertTriangle size={15} /> {errText(validateMut.error || importMut.error).slice(0, 300)}
          </div>
        )}
      </Card>

      {/* Step 2 — validation result */}
      {validation && (
        <>
          <StatGrid className="lg:grid-cols-4 xl:grid-cols-5">
            <StatCard
              icon={FileUp}
              label="Rows in file"
              value={validation.summary?.rows ?? 0}
              tone="neutral"
            />
            <StatCard
              icon={CheckCircle2}
              label="Importable records"
              value={validation.summary?.importable ?? 0}
              tone="info"
            />
            <StatCard
              icon={AlertTriangle}
              label="Errors"
              value={validation.summary?.errors ?? 0}
              tone={errors.length ? "critical" : "ok"}
            />
            <StatCard
              icon={AlertTriangle}
              label="Warnings"
              value={validation.summary?.warnings ?? 0}
              tone={warnings.length ? "warn" : "ok"}
            />
            <StatCard
              icon={validation.valid ? CheckCircle2 : AlertTriangle}
              label="Verdict"
              value={validation.valid ? "READY" : "REJECTED"}
              tone={validation.valid ? "ok" : "critical"}
            />
          </StatGrid>

          {errors.length > 0 && (
            <Card className="p-0">
              <div className="border-b border-border px-3 py-2 text-sm font-semibold text-critical">
                Validation errors ({errors.length})
              </div>
              <DataTable
                columns={errorCols}
                rows={errors}
                rowKey={(r) => String(r._k)}
                pageSize={10}
              />
            </Card>
          )}
          {preview.length > 0 && (
            <Card className="p-0">
              <div className="border-b border-border px-3 py-2 text-sm font-semibold text-foreground">
                Preview (first {preview.length} parsed records)
              </div>
              <DataTable
                columns={previewCols}
                rows={preview}
                rowKey={(r) => String(r._k)}
                pageSize={10}
              />
            </Card>
          )}
        </>
      )}

      {/* Step 3 — import result */}
      {importResult && (
        <Card className="p-4">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <StatusChip label={importResult.status} tone={statusTone(importResult.status)} />
            <span className="text-foreground">
              {importResult.inserted} inserted · {importResult.updated ?? 0} refreshed
              {importResult.status === "IMPORTED" && " — dashboard updated"}
            </span>
          </div>
          {importResult.error && (
            <div className="mt-2 text-[13px] text-critical">{String(importResult.error)}</div>
          )}
        </Card>
      )}

      {/* Upload history */}
      <Card className="p-0">
        <div className="flex items-center justify-between border-b border-border px-3 py-2">
          <span className="text-sm font-semibold text-foreground">Upload history</span>
          <button type="button" className={BTN} onClick={() => historyQ.refetch()}>
            <RefreshCw size={14} /> Refresh
          </button>
        </div>
        <DataTable
          columns={histCols}
          rows={historyQ.data?.items ?? []}
          rowKey={(r) => r.upload_id}
          status={{
            isLoading: historyQ.isLoading,
            isError: historyQ.isError,
            error: historyQ.error,
          }}
          onRetry={() => historyQ.refetch()}
          emptyLabel="No uploads yet."
          pageSize={10}
        />
      </Card>
    </div>
  );
}
