// Berthing — Full Extract (module 7 sub-module). Admin/CONTROL_ROOM/CUSTOMS.
// Uploads the ORIGINAL terminal berthing-report PDF and captures EVERY table on the page
// verbatim (see docs/BERTHING_PDF_DATA_AUDIT.md) via /api/berthing/extract (dry-run preview,
// no write) → /api/berthing/extract/import (persist). Shows detected terminal, table count,
// row count, and a preview of every extracted table before import. Runs alongside — never
// touching — the normalised Vessel List / Dashboard / Timeline tabs.
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  FileUp,
  CheckCircle2,
  AlertTriangle,
  Loader2,
  RefreshCw,
  ChevronRight,
  ChevronDown,
  Table2,
} from "lucide-react";
import { api } from "@/lib/api";
import { Card } from "@/components/ui/card";
import {
  StatGrid,
  StatCard,
  StatusChip,
  DataTable,
  type Column,
  type Tone,
} from "@/components/ui/dtccc";

const BTN =
  "inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm font-medium text-foreground transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50";
const BTN_PRIMARY =
  "inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-semibold text-primary-foreground transition-colors hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50";

function statusTone(s?: string): Tone {
  return s === "IMPORTED"
    ? "ok"
    : s === "SKIPPED_DUPLICATE"
      ? "info"
      : s === "FAILED" || s === "REJECTED"
        ? "critical"
        : "neutral";
}

// One extracted table with an expandable verbatim preview.
export function TablePanel({ t }: { t: any }) {
  const [open, setOpen] = useState(false);
  const cols: string[] = t.original_columns ?? [];
  const rows: any[] = (t.rows ?? []).map((r: any, i: number) => ({ _k: i, ...r }));
  const previewCols: Column<any>[] = (cols.length ? cols : ["_raw"]).slice(0, 12).map((c) => ({
    key: c,
    header: c,
    render: (r) => <span className="whitespace-nowrap">{String(r[c] ?? "")}</span>,
  }));
  const isRaw = t.table_name === "UNCAPTURED_TEXT";
  return (
    <Card className="p-0">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 border-b border-border px-3 py-2 text-left text-sm font-semibold hover:bg-muted/40"
      >
        {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
        <Table2 size={14} className="text-muted-foreground" />
        <span className={isRaw ? "text-warn" : "text-foreground"}>{t.table_name}</span>
        <StatusChip label={`${t.row_count} rows`} tone={isRaw ? "warn" : "neutral"} />
        {t.extraction_note && t.extraction_note !== "empty" && (
          <span className="text-[11px] text-muted-foreground">· {t.extraction_note}</span>
        )}
      </button>
      {open && rows.length > 0 && (
        <div className="overflow-x-auto">
          <DataTable columns={previewCols} rows={rows} rowKey={(r) => String(r._k)} pageSize={8} />
        </div>
      )}
      {open && rows.length === 0 && (
        <div className="px-3 py-3 text-[12px] text-muted-foreground">No rows in this panel.</div>
      )}
    </Card>
  );
}

export default function BerthingFullExtract() {
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [preview, setPreview] = useState<any | null>(null);
  const [importResult, setImportResult] = useState<any | null>(null);

  const docsQ = useQuery({
    queryKey: ["berthing-documents"],
    queryFn: () => api.berthingDocuments({ limit: 25 }),
  });

  const pickFile = (f: File | null) => {
    setFile(f);
    setPreview(null);
    setImportResult(null);
  };
  const extractMut = useMutation({
    mutationFn: () => api.berthingExtract(file as File),
    onSuccess: (res) => {
      setPreview(res);
      setImportResult(null);
    },
  });
  const importMut = useMutation({
    mutationFn: () => api.berthingExtractImport(file as File),
    onSuccess: (res) => {
      setImportResult(res);
      qc.invalidateQueries({ queryKey: ["berthing-documents"] });
    },
  });

  const busy = extractMut.isPending || importMut.isPending;
  const canImport = !!file && !!preview && !importResult;
  const tables: any[] = preview?.tables ?? [];

  const histCols: Column<any>[] = [
    {
      key: "created_at",
      header: "When",
      render: (r) => (r.created_at ? String(r.created_at).replace("T", " ").slice(0, 16) : "—"),
    },
    { key: "terminal", header: "Terminal", render: (r) => r.terminal ?? "—" },
    {
      key: "file_name",
      header: "File",
      render: (r) => <span className="font-mono">{r.file_name}</span>,
    },
    { key: "report_date", header: "Report date", render: (r) => r.report_date ?? "—" },
    { key: "table_count", header: "Tables", align: "right", render: (r) => r.table_count ?? 0 },
    { key: "row_count", header: "Rows", align: "right", render: (r) => r.row_count ?? 0 },
    { key: "uploaded_by", header: "By", render: (r) => r.uploaded_by ?? "—" },
  ];

  return (
    <div className="flex flex-col gap-4">
      {/* Step 1 — pick PDF + extract */}
      <Card className="p-4">
        <div className="flex flex-wrap items-center gap-3">
          <input
            ref={fileRef}
            type="file"
            accept=".pdf"
            className="hidden"
            onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
            aria-label="Choose berthing report PDF"
          />
          <button type="button" className={BTN} onClick={() => fileRef.current?.click()}>
            <FileUp size={15} /> {file ? "Change PDF" : "Choose PDF"}
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
              onClick={() => extractMut.mutate()}
            >
              {extractMut.isPending ? (
                <Loader2 size={15} className="animate-spin" />
              ) : (
                <CheckCircle2 size={15} />
              )}{" "}
              Extract preview
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
          Upload an original terminal berthing-report PDF (APMT / BMCT / NSFT / NSICT / NSIGT).
          Every table on the page is captured verbatim — the terminal is auto-detected. Re-uploading
          the same PDF is safe (skipped).
        </p>
        {(extractMut.isError || importMut.isError) && (
          <div className="mt-3 flex items-center gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-[13px] text-critical">
            <AlertTriangle size={15} />{" "}
            {String((extractMut.error || importMut.error) as any)?.slice(0, 240)}
          </div>
        )}
      </Card>

      {/* Step 2 — extraction preview */}
      {preview && (
        <>
          <StatGrid className="lg:grid-cols-3 xl:grid-cols-5">
            <StatCard
              icon={Table2}
              label="Detected terminal"
              value={preview.terminal ?? "—"}
              tone="info"
            />
            <StatCard
              icon={CheckCircle2}
              label="Report date"
              value={preview.report_date ?? "—"}
              tone="neutral"
            />
            <StatCard
              icon={Table2}
              label="Tables found"
              value={preview.table_count ?? 0}
              tone="ok"
            />
            <StatCard
              icon={Table2}
              label="Total rows"
              value={preview.total_rows ?? 0}
              tone="neutral"
            />
            <StatCard
              icon={preview.missing_sections?.length ? AlertTriangle : CheckCircle2}
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
            {tables.map((t, i) => (
              <TablePanel key={i} t={t} />
            ))}
          </div>
        </>
      )}

      {/* Step 3 — import result */}
      {importResult && (
        <Card className="p-4">
          <div className="flex flex-wrap items-center gap-2 text-sm font-semibold">
            <StatusChip label={importResult.status} tone={statusTone(importResult.status)} />
            <span className="text-foreground">
              {importResult.status === "SKIPPED_DUPLICATE"
                ? "This exact PDF was already extracted — nothing changed (safe)."
                : `Document #${importResult.document_id} · ${importResult.table_count} tables · ${importResult.row_count} rows stored verbatim`}
            </span>
          </div>
        </Card>
      )}

      {/* Document history */}
      <Card className="p-0">
        <div className="flex items-center justify-between border-b border-border px-3 py-2">
          <span className="text-sm font-semibold text-foreground">Extracted documents</span>
          <button type="button" className={BTN} onClick={() => docsQ.refetch()}>
            <RefreshCw size={14} /> Refresh
          </button>
        </div>
        <DataTable
          columns={histCols}
          rows={docsQ.data?.items ?? []}
          rowKey={(r) => String(r.id)}
          status={{ isLoading: docsQ.isLoading, isError: docsQ.isError, error: docsQ.error }}
          onRetry={() => docsQ.refetch()}
          emptyLabel="No documents extracted yet."
          pageSize={10}
        />
      </Card>
    </div>
  );
}
