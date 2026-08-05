// Transporters & Drivers — Data Upload (UC-III sub-module). Admin/CONTROL_ROOM/CUSTOMS.
// Mirrors the CFS-ECY / Shipping Lines Data Upload UX exactly: pick type
// (Transporter/Driver) → Download template → pick CSV/XLS/XLSX → Validate (preview +
// errors, no import) → Import (idempotent upsert) → history refresh. Everything flows
// through /api/td-upload/* — nothing mocked; re-uploading the same file is a no-op
// (sha256 dedup), existing rows are updated (upsert on Company ID / Licence Number),
// and invalid rows are skipped with friendly errors.
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Download,
  FileUp,
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
  type Tone,
} from "@/components/ui/dtccc";

const ENTITIES = [
  { value: "TRANSPORTER", label: "Transporter — company master" },
  { value: "DRIVER", label: "Driver — licensed-driver master" },
];
const BTN =
  "inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm font-medium text-foreground transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50";
const BTN_PRIMARY =
  "inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-semibold text-primary-foreground transition-colors hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50";

function statusTone(s?: string): Tone {
  return s === "SUCCESS" || s === "IMPORTED"
    ? "ok"
    : s === "PARTIAL" || s === "VALIDATED"
      ? "warn"
      : s === "SKIPPED_DUPLICATE"
        ? "info"
        : s === "FAILED" || s === "REJECTED"
          ? "critical"
          : "neutral";
}

export default function TransportersDriversUploadPanel() {
  const qc = useQueryClient();
  const [entity, setEntity] = useState("TRANSPORTER");
  const [file, setFile] = useState<File | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [validation, setValidation] = useState<any | null>(null);
  const [importResult, setImportResult] = useState<any | null>(null);

  const historyQ = useQuery({
    queryKey: ["td-uploads"],
    queryFn: () => api.tdUploads({ limit: 25 }),
  });

  const reset = () => {
    setValidation(null);
    setImportResult(null);
  };
  const pickFile = (f: File | null) => {
    setFile(f);
    reset();
  };

  const validateMut = useMutation({
    mutationFn: () => api.tdUploadValidate(entity, file as File),
    onSuccess: (res) => {
      setValidation(res);
      setImportResult(null);
    },
  });
  const importMut = useMutation({
    mutationFn: () => api.tdUpload(entity, file as File),
    onSuccess: (res) => {
      setImportResult(res);
      // refresh history AND the browse-tab lists (transporters / driver-master / fleet)
      qc.invalidateQueries({ queryKey: ["td-uploads"] });
      qc.invalidateQueries({
        predicate: (q) => {
          const k = Array.isArray(q.queryKey) ? String(q.queryKey[0]) : "";
          return (
            k.startsWith("transporter") ||
            k.startsWith("driver") ||
            k.startsWith("drivers-master") ||
            k.startsWith("fleet")
          );
        },
      });
    },
  });

  const errors: any[] = (validation?.errors ?? []).map((e: any, i: number) => ({ _k: i, ...e }));
  const warnings: any[] = (validation?.warnings ?? []).map((w: any, i: number) => ({
    _k: i,
    ...w,
  }));
  const preview: any[] = (validation?.preview ?? []).map((r: any, i: number) => ({ _k: i, ...r }));
  const canImport = !!file && validation?.valid === true && !importResult;
  const busy = validateMut.isPending || importMut.isPending;
  const sum = validation?.summary;

  const previewCols: Column<any>[] =
    preview.length > 0
      ? Object.keys(preview[0])
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
  const warnCols: Column<any>[] = [
    { key: "row_number", header: "Row", align: "right", render: (r) => r.row_number ?? "—" },
    {
      key: "error_code",
      header: "Code",
      render: (r) => <StatusChip label={r.error_code} tone="warn" />,
    },
    { key: "error_detail", header: "Detail", render: (r) => r.error_detail ?? "—" },
  ];
  const histCols: Column<any>[] = [
    {
      key: "created_at",
      header: "When",
      render: (r) => (r.created_at ? String(r.created_at).replace("T", " ").slice(0, 16) : "—"),
    },
    { key: "entity_type", header: "Type", render: (r) => r.entity_type ?? "—" },
    {
      key: "source_file",
      header: "File",
      render: (r) => <span className="font-mono">{r.source_file}</span>,
    },
    {
      key: "import_status",
      header: "Status",
      render: (r) => <StatusChip label={r.import_status} tone={statusTone(r.import_status)} />,
    },
    { key: "record_count", header: "Rows", align: "right", render: (r) => r.record_count ?? 0 },
    {
      key: "imported_count",
      header: "Imported",
      align: "right",
      render: (r) => r.imported_count ?? 0,
    },
    {
      key: "duplicate_count",
      header: "Dupes",
      align: "right",
      render: (r) => r.duplicate_count ?? 0,
    },
    { key: "error_count", header: "Errors", align: "right", render: (r) => r.error_count ?? 0 },
    { key: "uploaded_by", header: "By", render: (r) => r.uploaded_by ?? "—" },
  ];

  const isTransporter = entity === "TRANSPORTER";

  return (
    <div className="flex flex-col gap-4">
      {/* Step 1 — pick type + template + file */}
      <Card className="p-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1">
            <label className="text-[12px] font-medium text-muted-foreground">Upload type</label>
            <FilterSelect
              label="Upload type"
              value={entity}
              onChange={(v) => {
                setEntity(v);
                pickFile(null);
                if (fileRef.current) fileRef.current.value = "";
              }}
              options={ENTITIES}
            />
          </div>
          <button
            type="button"
            className={BTN}
            onClick={() => api.tdUploadDownloadTemplate(entity)}
          >
            <Download size={15} /> Download template
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".csv,.xls,.xlsx"
            className="hidden"
            onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
            aria-label="Choose data file"
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
              Confirm Import
            </button>
          </div>
        </div>
        <p className="mt-2 text-[11.5px] text-muted-foreground">
          Supported: CSV · XLS · XLSX.{" "}
          {isTransporter
            ? "Required columns: Company ID · Company Name (idempotency key: Company ID)."
            : "Required columns: Licence Number · Driver Name (idempotency key: Licence Number)."}{" "}
          Column names are flexible (e.g. “Transporter Name”, “Driver_Name”, “DL Number” all map).
          Re-uploading the same file is safe — duplicates are skipped and existing rows are updated.
        </p>
        {(validateMut.isError || importMut.isError) && (
          <div className="mt-3 flex items-center gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-[13px] text-critical">
            <AlertTriangle size={15} />{" "}
            {String((validateMut.error || importMut.error) as any)?.slice(0, 240)}
          </div>
        )}
      </Card>

      {/* Step 2 — validation result (preview before import) */}
      {validation && (
        <>
          <StatGrid className="lg:grid-cols-3 xl:grid-cols-5">
            <StatCard icon={FileUp} label="Total rows" value={sum?.rows ?? 0} tone="neutral" />
            <StatCard icon={CheckCircle2} label="Valid rows" value={sum?.valid ?? 0} tone="info" />
            <StatCard
              icon={Ban}
              label="Invalid rows"
              value={sum?.invalid ?? 0}
              tone={sum?.invalid ? "critical" : "ok"}
            />
            <StatCard
              icon={Copy}
              label="Duplicate rows"
              value={sum?.duplicates ?? 0}
              tone={sum?.duplicates ? "warn" : "ok"}
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
                Preview — first {preview.length} valid records
              </div>
              <DataTable
                columns={previewCols}
                rows={preview}
                rowKey={(r) => String(r._k)}
                pageSize={10}
              />
            </Card>
          )}
          {warnings.length > 0 && (
            <Card className="p-0">
              <div className="border-b border-border px-3 py-2 text-sm font-semibold text-warn">
                Warnings ({warnings.length}) — these rows still import
              </div>
              <DataTable
                columns={warnCols}
                rows={warnings}
                rowKey={(r) => String(r._k)}
                pageSize={5}
              />
            </Card>
          )}
        </>
      )}

      {/* Step 3 — import result */}
      {importResult && (
        <Card className="p-4">
          <div className="flex flex-wrap items-center gap-2 text-sm font-semibold">
            <StatusChip label={importResult.status} tone={statusTone(importResult.status)} />
            <span className="text-foreground">
              {importResult.status === "SKIPPED_DUPLICATE"
                ? "This exact file was already imported — nothing changed (safe)."
                : `${importResult.imported ?? 0} imported (${importResult.created ?? 0} new · ${importResult.updated ?? 0} updated) · ${importResult.skipped ?? 0} duplicate · ${importResult.invalid ?? 0} invalid skipped`}
              {importResult.status === "PARTIAL" && " — some rows were skipped (see errors above)"}
              {(importResult.status === "SUCCESS" || importResult.status === "PARTIAL") &&
                " — master list updated"}
            </span>
          </div>
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
          rowKey={(r) => String(r.id)}
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
