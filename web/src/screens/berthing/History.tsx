// Berthing — Upload history table (module 7 sub-module). Reads /api/berthing/uploads
// via the typed api helper. Rendered inside the Data Upload tab (mirrors the CFS-ECY
// upload panel's history block, extracted here as its own component per the module spec).
import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { StatusChip, DataTable, type Column, type Tone } from "@/components/ui/dtccc";

export function statusTone(s?: string): Tone {
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

const BTN =
  "inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm font-medium text-foreground transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50";

export default function BerthingUploadHistory() {
  const historyQ = useQuery({
    queryKey: ["berthing-uploads"],
    queryFn: () => api.berthingUploads({ limit: 25 }),
  });

  const cols: Column<any>[] = [
    {
      key: "created_at",
      header: "When",
      render: (r) => (r.created_at ? String(r.created_at).replace("T", " ").slice(0, 16) : "—"),
    },
    { key: "terminal", header: "Terminal", render: (r) => r.terminal ?? "—" },
    {
      key: "filename",
      header: "File",
      render: (r) => <span className="font-mono">{r.filename}</span>,
    },
    {
      key: "physical_format",
      header: "Fmt",
      render: (r) => <StatusChip label={r.physical_format} tone="neutral" />,
    },
    {
      key: "status",
      header: "Status",
      render: (r) => <StatusChip label={r.status} tone={statusTone(r.status)} />,
    },
    { key: "total_rows", header: "Rows", align: "right", render: (r) => r.total_rows ?? 0 },
    { key: "success_rows", header: "Imported", align: "right", render: (r) => r.success_rows ?? 0 },
    {
      key: "duplicate_rows",
      header: "Dupes",
      align: "right",
      render: (r) => r.duplicate_rows ?? 0,
    },
    { key: "failed_rows", header: "Errors", align: "right", render: (r) => r.failed_rows ?? 0 },
    { key: "uploaded_by", header: "By", render: (r) => r.uploaded_by ?? "—" },
  ];

  return (
    <Card className="p-0">
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <span className="text-sm font-semibold text-foreground">Upload history</span>
        <button type="button" className={BTN} onClick={() => historyQ.refetch()}>
          <RefreshCw size={14} /> Refresh
        </button>
      </div>
      <DataTable
        columns={cols}
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
  );
}
