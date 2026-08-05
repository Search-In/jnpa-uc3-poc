// Berthing — Report Details (module 7). Read-only viewer of a fully-extracted PDF report.
// Renders EVERY table stored in jnpa.berthing_report_tables for one document, exactly as
// extracted (GET /api/berthing/documents/{id}/full-view). Columns are generated DYNAMICALLY
// from the response `columns[]` — nothing is hardcoded, so all five terminals
// (APMT/BMCT/NSFT/NSICT/NSIGT) render unchanged. No filtering, no column dropping.
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Table2,
  FileText,
  RefreshCw,
  ChevronRight,
  ChevronDown,
  Inbox,
  Download,
} from "lucide-react";
import { api } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { LoadingState, ErrorState } from "@/components/ui/misc";
import { StatGrid, StatCard, StatusChip, DataTable, type Column } from "@/components/ui/dtccc";

const inputCls =
  "h-9 rounded-md border border-border bg-background px-2 text-[13px] font-medium text-foreground outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary/20";
const BTN =
  "inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-[12px] font-medium text-foreground hover:bg-muted";

// RFC-4180 CSV escaping — preserves empty cells and commas/quotes/newlines verbatim.
function csvCell(v: any): string {
  const s = String(v ?? "");
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}
function tableToCsv(t: any): string {
  const names = (t.columns ?? []).map((c: any) => c.name);
  const header = names.map(csvCell).join(",");
  const body = (t.rows ?? []).map((r: any) => (r.values ?? []).map(csvCell).join(",")).join("\n");
  return `${header}\n${body}\n`;
}
function download(filename: string, text: string) {
  const blob = new Blob([text], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// One extracted table → a PDF-like DataTable. Columns come from `columns[].name` in the
// SAME order as the PDF; rows are positional `values[]` so empty cells are preserved.
function DynamicTable({ t, filePrefix }: { t: any; filePrefix: string }) {
  const [open, setOpen] = useState(true);
  const cols: any[] = Array.isArray(t.columns) && t.columns.length ? t.columns : [{ name: "_raw" }];
  const rows: any[] = (t.rows ?? []).map((r: any, i: number) => ({
    _k: i,
    values: r.values ?? [],
  }));
  const isRaw = t.table_name === "UNCAPTURED_TEXT";
  // Columns generated dynamically from the response — never hardcoded per terminal.
  const columns: Column<any>[] = cols.map((c: any, ci: number) => ({
    key: String(ci),
    header: c.name,
    render: (r) => <span className="whitespace-nowrap">{String(r.values?.[ci] ?? "")}</span>,
  }));

  return (
    <Card className="p-0">
      <div className="flex w-full items-center gap-2 border-b border-border px-3 py-2 text-sm font-semibold">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="flex flex-1 items-center gap-2 text-left hover:opacity-80"
        >
          {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
          <Table2 size={14} className="text-muted-foreground" />
          <span className={isRaw ? "text-warn" : "text-foreground"}>{t.table_name}</span>
          <StatusChip label={`${t.row_count} rows`} tone={isRaw ? "warn" : "neutral"} />
          <StatusChip label={`${cols.length} cols`} tone="neutral" />
          {t.extraction_note && t.extraction_note !== "empty" && (
            <span className="text-[11px] text-muted-foreground">· {t.extraction_note}</span>
          )}
        </button>
        <button
          type="button"
          className={BTN}
          onClick={() => download(`${filePrefix}_${t.table_name}.csv`, tableToCsv(t))}
          title="Export this table as CSV"
        >
          <Download size={13} /> CSV
        </button>
      </div>
      {open &&
        (rows.length > 0 ? (
          <div className="overflow-x-auto">
            <DataTable columns={columns} rows={rows} rowKey={(r) => String(r._k)} pageSize={25} />
          </div>
        ) : (
          <div className="px-3 py-3 text-[12px] text-muted-foreground">
            No rows extracted for this section.
          </div>
        ))}
    </Card>
  );
}

export default function BerthingReportDetails({ docId }: { docId?: number | null }) {
  const docsQ = useQuery({
    queryKey: ["berthing-documents"],
    queryFn: () => api.berthingDocuments({ limit: 50 }),
  });
  const documents: any[] = docsQ.data?.items ?? [];
  const [selected, setSelected] = useState<number | null>(null);

  // Default selection: the doc passed in (just-uploaded), else the most recent.
  useEffect(() => {
    if (docId) setSelected(docId);
    else if (selected == null && documents.length) setSelected(documents[0].id);
  }, [docId, documents, selected]);

  const viewQ = useQuery({
    queryKey: ["berthing-fullview", selected],
    queryFn: () => api.berthingDocumentFullView(selected as number),
    enabled: !!selected,
  });
  const view = viewQ.data;

  return (
    <div className="flex flex-col gap-4">
      {/* Document picker */}
      <Card className="p-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1">
            <label className="text-[12px] font-medium text-muted-foreground">Report document</label>
            <select
              value={selected ?? ""}
              onChange={(e) => setSelected(e.target.value ? Number(e.target.value) : null)}
              className={inputCls}
              aria-label="Select report document"
            >
              {documents.length === 0 && <option value="">No documents yet</option>}
              {documents.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.terminal} · {d.file_name} {d.report_date ? `· ${d.report_date}` : ""}
                </option>
              ))}
            </select>
          </div>
          <button
            type="button"
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm font-medium text-foreground hover:bg-muted"
            onClick={() => {
              docsQ.refetch();
              viewQ.refetch();
            }}
          >
            <RefreshCw size={14} /> Refresh
          </button>
          <p className="text-[11.5px] text-muted-foreground">
            Upload a PDF in <span className="font-medium text-foreground">Report Upload</span> — it
            appears here automatically. Every extracted table is shown verbatim with dynamic
            columns.
          </p>
        </div>
      </Card>

      {/* Empty / loading / error */}
      {!selected ? (
        <Card className="p-0">
          <div className="flex flex-col items-center justify-center gap-2 py-12 text-center">
            <span className="flex h-12 w-12 items-center justify-center rounded-full bg-muted text-muted-foreground">
              <Inbox size={22} />
            </span>
            <div className="text-sm font-medium">No report selected</div>
            <div className="max-w-xs text-[12px] text-muted-foreground">
              Upload a berthing report PDF, then pick it above to see every extracted table.
            </div>
          </div>
        </Card>
      ) : viewQ.isLoading ? (
        <div className="p-6">
          <LoadingState />
        </div>
      ) : viewQ.isError ? (
        <ErrorState onRetry={() => viewQ.refetch()} detail="Unable to load report details." />
      ) : view ? (
        <>
          {/* Header summary */}
          <StatGrid className="lg:grid-cols-4 xl:grid-cols-5">
            <StatCard
              icon={FileText}
              label="Terminal detected"
              value={view.terminal ?? "—"}
              tone="info"
            />
            <StatCard
              icon={FileText}
              label="Report date"
              value={view.report_date ?? "—"}
              tone="neutral"
            />
            <StatCard icon={Table2} label="Tables" value={view.table_count ?? 0} tone="ok" />
            <StatCard icon={Table2} label="Total rows" value={view.row_count ?? 0} tone="neutral" />
            <StatCard icon={FileText} label="Pages" value={view.page_count ?? 1} tone="neutral" />
          </StatGrid>
          <div className="flex flex-wrap items-center gap-2 text-[12px] text-muted-foreground">
            <span className="font-mono">{view.file_name}</span> — {view.tables.length} sections
            <button
              type="button"
              className={`${BTN} ml-auto`}
              onClick={() => {
                const prefix = `${view.terminal}_${view.report_date ?? view.document_id}`;
                const all = view.tables
                  .map((t: any) => `# ${t.table_name}\n${tableToCsv(t)}`)
                  .join("\n");
                download(`${prefix}_full_report.csv`, all);
              }}
              title="Export every table as one CSV"
            >
              <Download size={13} /> Export all (CSV)
            </button>
          </div>

          {/* Every table, dynamic columns, same order + names as the PDF */}
          <div className="flex flex-col gap-2">
            {view.tables.map((t: any, i: number) => (
              <DynamicTable
                key={i}
                t={t}
                filePrefix={`${view.terminal}_${view.report_date ?? view.document_id}`}
              />
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
}
