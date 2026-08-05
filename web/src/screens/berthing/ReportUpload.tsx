// Berthing — Report Upload (module 7). Admin/CONTROL_ROOM/CUSTOMS.
// ONE unified upload experience that auto-detects the file type and routes to the right
// engine — no separate "Data Upload" vs "Full Extract" screens:
//   • PDF          → Full PDF Extraction engine (/api/berthing/extract → /extract/import):
//                    auto-detects terminal, captures every table verbatim, multi-table preview.
//   • CSV/XLS/XLSX → structured upload parser (/api/berthing/validate → /upload):
//                    validates the normalised columns, row preview + errors.
// Single flow: choose file → detect format → correct parser → validate → preview → import.
// All backend APIs + database logic are unchanged; only the frontend is merged.
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Download,
  FileUp,
  FileText,
  FileSpreadsheet,
  CheckCircle2,
  AlertTriangle,
  Loader2,
  RefreshCw,
  Copy,
  Ban,
} from "lucide-react";
import { api } from "@/lib/api";
import { Card } from "@/components/ui/card";
import {
  StatGrid,
  StatCard,
  StatusChip,
  FilterSelect,
  DataTable,
  type Column,
} from "@/components/ui/dtccc";
import BerthingUploadHistory, { statusTone } from "@/screens/berthing/History";
import { TablePanel } from "@/screens/berthing/FullExtract";

const TERMINALS = [
  { value: "ALL", label: "All terminals (Terminal column required per row)" },
  { value: "APMT", label: "APMT — APM Terminals" },
  { value: "BMCT", label: "BMCT — BMCT PSA" },
  { value: "NSFT", label: "NSFT — Nhava Sheva Freeport" },
  { value: "NSICT", label: "NSICT — DP World" },
  { value: "NSIGT", label: "NSIGT — DP World" },
];
const BTN =
  "inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm font-medium text-foreground transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50";
const BTN_PRIMARY =
  "inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-semibold text-primary-foreground transition-colors hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50";

type Kind = "pdf" | "sheet" | null;
function detectKind(name?: string): Kind {
  const ext = (name || "").toLowerCase().split(".").pop();
  if (ext === "pdf") return "pdf";
  if (ext === "csv" || ext === "xls" || ext === "xlsx") return "sheet";
  return null;
}

export default function BerthingReportUpload({
  onImported,
}: {
  // Fired after a successful import so the host can open Report Details on the new document.
  onImported?: (documentId: number, kind: "pdf" | "sheet") => void;
}) {
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [terminal, setTerminal] = useState("ALL");
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [validation, setValidation] = useState<any | null>(null); // sheet dry-run result
  const [preview, setPreview] = useState<any | null>(null); // pdf extract result
  const [importResult, setImportResult] = useState<any | null>(null);

  const kind = detectKind(file?.name);
  const termParam = terminal === "ALL" ? "" : terminal;

  // Structured-upload history is rendered by <BerthingUploadHistory/> (owns its own query);
  // this drives only the PDF full-extract documents table below.
  const docsQ = useQuery({
    queryKey: ["berthing-documents"],
    queryFn: () => api.berthingDocuments({ limit: 25 }),
  });

  const reset = () => {
    setValidation(null);
    setPreview(null);
    setImportResult(null);
  };
  const pickFile = (f: File | null) => {
    setFile(f);
    reset();
  };

  // ---- Validate / Preview (routes by file kind) ----
  const validateMut = useMutation({
    mutationFn: async () => {
      if (kind === "pdf") return { kind: "pdf", res: await api.berthingExtract(file as File) };
      return { kind: "sheet", res: await api.berthingUploadValidate(termParam, file as File) };
    },
    onSuccess: ({ kind: k, res }) => {
      setImportResult(null);
      if (k === "pdf") {
        setPreview(res);
        setValidation(null);
      } else {
        setValidation(res);
        setPreview(null);
      }
    },
  });

  // ---- Confirm Import (routes by file kind) ----
  const importMut = useMutation({
    mutationFn: async () => {
      if (kind === "pdf") return await api.berthingExtractImport(file as File);
      return await api.berthingUpload(termParam, file as File);
    },
    onSuccess: (res) => {
      setImportResult(res);
      qc.invalidateQueries({
        predicate: (q) => Array.isArray(q.queryKey) && String(q.queryKey[0]).startsWith("berthing"),
      });
      // A PDF import yields a document_id → let the host open Report Details on it.
      if (kind === "pdf" && res?.document_id) onImported?.(res.document_id, "pdf");
    },
  });

  const busy = validateMut.isPending || importMut.isPending;
  const canValidate = !!file && !!kind && !busy;
  const canImport =
    !!file && !importResult && !busy && (kind === "pdf" ? !!preview : validation?.valid === true);

  // ---- sheet preview cells ----
  const sErrors: any[] = (validation?.errors ?? []).map((e: any, i: number) => ({ _k: i, ...e }));
  const sWarnings: any[] = (validation?.warnings ?? []).map((w: any, i: number) => ({
    _k: i,
    ...w,
  }));
  const sPreview: any[] = (validation?.preview ?? []).map((r: any, i: number) => ({ _k: i, ...r }));
  const sSum = validation?.summary;
  const previewCols: Column<any>[] =
    sPreview.length > 0
      ? Object.keys(sPreview[0])
          .filter((k) => k !== "_k")
          .slice(0, 9)
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

  const pdfTables: any[] = preview?.tables ?? [];
  const FmtIcon = kind === "pdf" ? FileText : FileSpreadsheet;

  return (
    <div className="flex flex-col gap-4">
      {/* Step 1 — one picker for every format */}
      <Card className="p-4">
        <div className="flex flex-wrap items-end gap-3">
          {/* Terminal selector only matters for spreadsheets (PDF auto-detects the terminal) */}
          {kind !== "pdf" && (
            <div className="flex flex-col gap-1">
              <label className="text-[12px] font-medium text-muted-foreground">Terminal</label>
              <FilterSelect
                label="Terminal"
                value={terminal}
                onChange={(v) => {
                  setTerminal(v);
                  reset();
                }}
                options={TERMINALS}
              />
            </div>
          )}
          <button
            type="button"
            className={BTN}
            onClick={() => api.berthingDownloadTemplate(termParam)}
          >
            <Download size={15} /> Download template (CSV)
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".pdf,.csv,.xls,.xlsx"
            className="hidden"
            onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
            aria-label="Choose report file"
          />
          <button type="button" className={BTN} onClick={() => fileRef.current?.click()}>
            <FileUp size={15} /> {file ? "Change file" : "Choose file"}
          </button>
          {file && (
            <span className="inline-flex items-center gap-1.5 text-[12px] text-muted-foreground">
              <FmtIcon size={14} />
              {file.name} ({(file.size / 1024).toFixed(1)} KB)
              {kind && (
                <StatusChip
                  label={kind === "pdf" ? "PDF · full extract" : "Spreadsheet · structured"}
                  tone={kind === "pdf" ? "info" : "neutral"}
                />
              )}
              {file && !kind && <StatusChip label="unsupported type" tone="critical" />}
            </span>
          )}
          <div className="ml-auto flex items-center gap-2">
            <button
              type="button"
              className={BTN}
              disabled={!canValidate}
              onClick={() => validateMut.mutate()}
            >
              {validateMut.isPending ? (
                <Loader2 size={15} className="animate-spin" />
              ) : (
                <CheckCircle2 size={15} />
              )}{" "}
              Validate &amp; Preview
            </button>
            <button
              type="button"
              className={BTN_PRIMARY}
              disabled={!canImport}
              onClick={() => importMut.mutate()}
            >
              {importMut.isPending ? (
                <Loader2 size={15} className="animate-spin" />
              ) : (
                <FileUp size={15} />
              )}{" "}
              Confirm Import
            </button>
          </div>
        </div>
        <p className="mt-2 text-[11.5px] text-muted-foreground">
          Supported: <span className="font-medium text-foreground">PDF · CSV · XLS · XLSX</span>.
          The format is detected automatically — a PDF is run through full extraction (terminal
          auto-detected, every table captured verbatim); a CSV/XLS/XLSX is validated against the
          normalised columns (Terminal · Vessel Name · Voyage Number). Re-uploading the same file is
          safe (skipped).
        </p>
        {(validateMut.isError || importMut.isError) && (
          <div className="mt-3 flex items-center gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-[13px] text-critical">
            <AlertTriangle size={15} />{" "}
            {String((validateMut.error || importMut.error) as any)?.slice(0, 240)}
          </div>
        )}
      </Card>

      {/* Step 2a — PDF extraction preview */}
      {preview && (
        <>
          <StatGrid className="lg:grid-cols-3 xl:grid-cols-5">
            <StatCard label="Detected terminal" value={preview.terminal ?? "—"} tone="info" />
            <StatCard label="Report date" value={preview.report_date ?? "—"} tone="neutral" />
            <StatCard label="Tables found" value={preview.table_count ?? 0} tone="ok" />
            <StatCard label="Total rows" value={preview.total_rows ?? 0} tone="neutral" />
            <StatCard
              label="Missing sections"
              value={preview.missing_sections?.length ?? 0}
              tone={preview.missing_sections?.length ? "warn" : "ok"}
            />
          </StatGrid>
          <div className="text-[12px] text-muted-foreground">
            Pages {preview.page_count} · tables {preview.table_count} · rows {preview.total_rows} ·
            uncaptured lines {preview.uncaptured_lines ?? 0}
            {preview.missing_sections?.length
              ? ` · missing: ${preview.missing_sections.join(", ")}`
              : " · missing: none"}
          </div>
          <div className="flex flex-col gap-2">
            {pdfTables.map((t, i) => (
              <TablePanel key={i} t={t} />
            ))}
          </div>
        </>
      )}

      {/* Step 2b — spreadsheet validation preview */}
      {validation && (
        <>
          <StatGrid className="lg:grid-cols-3 xl:grid-cols-5">
            <StatCard icon={FileUp} label="Total rows" value={sSum?.rows ?? 0} tone="neutral" />
            <StatCard icon={CheckCircle2} label="Valid rows" value={sSum?.valid ?? 0} tone="info" />
            <StatCard
              icon={Ban}
              label="Invalid rows"
              value={sSum?.invalid ?? 0}
              tone={sSum?.invalid ? "critical" : "ok"}
            />
            <StatCard
              icon={Copy}
              label="Duplicate rows"
              value={sSum?.duplicates ?? 0}
              tone={sSum?.duplicates ? "warn" : "ok"}
            />
            <StatCard
              icon={validation.valid ? CheckCircle2 : AlertTriangle}
              label="Verdict"
              value={validation.valid ? "READY" : "REJECTED"}
              tone={validation.valid ? "ok" : "critical"}
            />
          </StatGrid>
          {sErrors.length > 0 && (
            <Card className="p-0">
              <div className="border-b border-border px-3 py-2 text-sm font-semibold text-critical">
                Validation errors ({sErrors.length})
              </div>
              <DataTable
                columns={errorCols}
                rows={sErrors}
                rowKey={(r) => String(r._k)}
                pageSize={10}
              />
            </Card>
          )}
          {sPreview.length > 0 && (
            <Card className="p-0">
              <div className="border-b border-border px-3 py-2 text-sm font-semibold text-foreground">
                Preview — first {sPreview.length} valid records
              </div>
              <DataTable
                columns={previewCols}
                rows={sPreview}
                rowKey={(r) => String(r._k)}
                pageSize={10}
              />
            </Card>
          )}
          {sWarnings.length > 0 && (
            <Card className="p-0">
              <div className="border-b border-border px-3 py-2 text-sm font-semibold text-warn">
                Warnings ({sWarnings.length}) — these rows still import
              </div>
              <DataTable
                columns={[
                  {
                    key: "row_number",
                    header: "Row",
                    align: "right",
                    render: (r) => r.row_number ?? "—",
                  },
                  { key: "error_detail", header: "Detail", render: (r) => r.error_detail ?? "—" },
                ]}
                rows={sWarnings}
                rowKey={(r) => String(r._k)}
                pageSize={5}
              />
            </Card>
          )}
        </>
      )}

      {/* Step 3 — import result (adapts to whichever engine ran) */}
      {importResult && (
        <Card className="p-4">
          <div className="flex flex-wrap items-center gap-2 text-sm font-semibold">
            <StatusChip label={importResult.status} tone={statusTone(importResult.status)} />
            <span className="text-foreground">
              {importResult.status === "SKIPPED_DUPLICATE"
                ? "This exact file was already imported — nothing changed (safe)."
                : kind === "pdf"
                  ? `Document #${importResult.document_id} · ${importResult.table_count} tables · ${importResult.row_count} rows stored verbatim`
                  : `${importResult.imported ?? 0} new · ${importResult.updated ?? 0} updated · ${importResult.skipped ?? 0} in-file dupes · ${importResult.invalid ?? 0} invalid`}
            </span>
          </div>
        </Card>
      )}

      {/* History — both stores, one place */}
      <BerthingUploadHistory />
      <Card className="p-0">
        <div className="flex items-center justify-between border-b border-border px-3 py-2">
          <span className="text-sm font-semibold text-foreground">
            Full-extract documents (PDF)
          </span>
          <button type="button" className={BTN} onClick={() => docsQ.refetch()}>
            <RefreshCw size={14} /> Refresh
          </button>
        </div>
        <DataTable
          columns={[
            {
              key: "created_at",
              header: "When",
              render: (r) =>
                r.created_at ? String(r.created_at).replace("T", " ").slice(0, 16) : "—",
            },
            { key: "terminal", header: "Terminal", render: (r) => r.terminal ?? "—" },
            {
              key: "file_name",
              header: "File",
              render: (r) => <span className="font-mono">{r.file_name}</span>,
            },
            { key: "report_date", header: "Report date", render: (r) => r.report_date ?? "—" },
            {
              key: "table_count",
              header: "Tables",
              align: "right",
              render: (r) => r.table_count ?? 0,
            },
            { key: "row_count", header: "Rows", align: "right", render: (r) => r.row_count ?? 0 },
            { key: "uploaded_by", header: "By", render: (r) => r.uploaded_by ?? "—" },
          ]}
          rows={docsQ.data?.items ?? []}
          rowKey={(r) => String(r.id)}
          status={{ isLoading: docsQ.isLoading, isError: docsQ.isError, error: docsQ.error }}
          onRetry={() => docsQ.refetch()}
          emptyLabel="No PDF documents extracted yet."
          pageSize={10}
        />
      </Card>
    </div>
  );
}
