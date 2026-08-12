// UC3 Email Processing.
//
// Lists emails read from the admin mailbox whose SUBJECT STARTS WITH "JNPA",
// and drives the two-step import: Process (dry-run preview showing the target
// master table) -> Confirm Import.
//
// Nothing here ever sees a credential: /api/email/health returns a masked
// address plus a connected flag, and no endpoint returns EMAIL_PASSWORD.
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type EmailMessage, type EmailProcessResult } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { EmptyState, LoadingState } from "@/components/ui/misc";
import { fmtDateTimeIST } from "@/lib/utils";

const STATUS_STYLE: Record<string, string> = {
  UNPROCESSED: "bg-muted text-foreground",
  PROCESSING: "bg-severity-info/15 text-severity-info",
  PROCESSED: "bg-severity-ok/15 text-severity-ok",
  FAILED: "bg-severity-critical/15 text-severity-critical",
  NEEDS_REVIEW: "bg-severity-warn/15 text-severity-warn",
  PREVIEWED: "bg-severity-info/15 text-severity-info",
};

function StatusChip({ status }: { status: string }) {
  return (
    <span
      className={`inline-flex rounded-full px-2 py-0.5 text-[11px] font-medium ${
        STATUS_STYLE[status] ?? "bg-muted text-foreground"
      }`}
    >
      {status.replace(/_/g, " ")}
    </span>
  );
}

function bytes(n: number): string {
  if (!n) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export default function EmailProcessing() {
  const qc = useQueryClient();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [result, setResult] = useState<EmailProcessResult | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [banner, setBanner] = useState<string>("");

  const health = useQuery({ queryKey: ["email-health"], queryFn: api.emailHealth });
  const list = useQuery({
    queryKey: ["email-messages", statusFilter],
    queryFn: () => api.emailMessages(statusFilter || undefined),
  });
  const detail = useQuery({
    queryKey: ["email-message", selectedId],
    queryFn: () => api.emailMessage(selectedId as number),
    enabled: selectedId !== null,
  });

  const failMessage = (e: unknown) => {
    // Surface only the server's user-facing sentence; never a stack/technical body.
    const raw = String((e as Error)?.message ?? "");
    const m = raw.match(/"message"\s*:\s*"([^"]+)"/);
    return m ? m[1] : "The request failed. Please try again.";
  };

  const sync = useMutation({
    mutationFn: api.emailSync,
    onSuccess: (r) => {
      setBanner(`Mailbox checked — ${r.stored} email(s) matching "${r.subject_prefix}".`);
      qc.invalidateQueries({ queryKey: ["email-messages"] });
    },
    onError: (e) => setBanner(failMessage(e)),
  });

  const preview = useMutation({
    mutationFn: (id: number) => api.emailPreview(id),
    onSuccess: (r) => {
      setResult(r);
      qc.invalidateQueries({ queryKey: ["email-messages"] });
    },
    onError: (e) => setBanner(failMessage(e)),
  });

  const doImport = useMutation({
    mutationFn: (id: number) => api.emailImport(id),
    onSuccess: (r) => {
      setResult(r);
      qc.invalidateQueries({ queryKey: ["email-messages"] });
      qc.invalidateQueries({ queryKey: ["email-message", selectedId] });
    },
    onError: (e) => setBanner(failMessage(e)),
  });

  const rows: EmailMessage[] = useMemo(() => list.data?.items ?? [], [list.data]);
  const busyId = preview.isPending
    ? (preview.variables as number)
    : doImport.isPending
      ? (doImport.variables as number)
      : null;

  return (
    <div className="flex flex-col gap-4 p-4">
      {/* ---- header + mailbox posture ------------------------------------ */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">Email Processing</h1>
          <p className="text-sm text-muted-foreground">
            Emails whose subject starts with{" "}
            <code className="rounded bg-muted px-1">
              {health.data?.mailbox?.subject_prefix ?? "JNPA"}
            </code>
            . Attachments are routed into the existing UC3 master tables.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select
            aria-label="Filter by status"
            className="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
          >
            <option value="">All statuses</option>
            {["UNPROCESSED", "PROCESSING", "PROCESSED", "NEEDS_REVIEW", "FAILED"].map((s) => (
              <option key={s} value={s}>
                {s.replace(/_/g, " ")}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="rounded-md bg-severity-info px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
            onClick={() => sync.mutate()}
            disabled={sync.isPending || !health.data?.connected}
          >
            {sync.isPending ? "Checking…" : "Check mailbox"}
          </button>
        </div>
      </div>

      {health.data && !health.data.connected && (
        <Card className="border-severity-warn/40 bg-severity-warn/5 p-3 text-sm">
          <span className="font-medium">Mailbox unavailable — </span>
          {health.data.message}
        </Card>
      )}
      {banner && (
        <Card className="p-3 text-sm">
          {banner}{" "}
          <button className="underline" onClick={() => setBanner("")} type="button">
            dismiss
          </button>
        </Card>
      )}

      {/* ---- inbox table -------------------------------------------------- */}
      <Card className="overflow-hidden p-0">
        {list.isLoading ? (
          <LoadingState label="Loading emails…" />
        ) : rows.length === 0 ? (
          <EmptyState>No matching emails yet. Use “Check mailbox” to read the inbox.</EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-muted/50 text-left text-xs uppercase text-muted-foreground">
                <tr>
                  <th className="px-3 py-2">Subject</th>
                  <th className="px-3 py-2">Sender</th>
                  <th className="px-3 py-2">Received</th>
                  <th className="px-3 py-2">Preview</th>
                  <th className="px-3 py-2 text-center">Att.</th>
                  <th className="px-3 py-2">Status</th>
                  <th className="px-3 py-2">Target master</th>
                  <th className="px-3 py-2 text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((m) => (
                  <tr key={m.id} className="border-t border-border align-top">
                    <td className="px-3 py-2">
                      <button
                        type="button"
                        className="text-left font-medium underline-offset-2 hover:underline"
                        onClick={() => {
                          setSelectedId(m.id);
                          setResult(null);
                        }}
                      >
                        {m.subject || "(no subject)"}
                      </button>
                    </td>
                    <td className="px-3 py-2 text-muted-foreground">{m.sender}</td>
                    <td className="px-3 py-2 text-muted-foreground">
                      {m.received_at ? fmtDateTimeIST(m.received_at) : "—"}
                    </td>
                    <td className="max-w-[22rem] px-3 py-2 text-muted-foreground">
                      <span className="line-clamp-2">{m.body_preview || "—"}</span>
                    </td>
                    <td className="px-3 py-2 text-center">{m.attachment_count}</td>
                    <td className="px-3 py-2">
                      <StatusChip status={m.processing_status} />
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                      {m.target_master_table ?? "—"}
                    </td>
                    <td className="px-3 py-2 text-right">
                      <button
                        type="button"
                        className="rounded-md border border-border px-2.5 py-1 text-xs font-medium hover:bg-muted disabled:opacity-50"
                        onClick={() => {
                          setSelectedId(m.id);
                          setResult(null);
                          preview.mutate(m.id);
                        }}
                        disabled={busyId === m.id}
                      >
                        {busyId === m.id ? "Working…" : "Process"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* ---- process result: preview -> confirm import --------------------- */}
      {result && (
        <Card className="p-4">
          <div className="mb-2 flex items-center gap-2">
            <StatusChip status={result.status} />
            <span className="text-sm font-medium">{result.message}</span>
          </div>

          {result.status === "NEEDS_REVIEW" ? (
            <div className="text-sm">
              <p className="text-muted-foreground">
                {result.reason ??
                  "The content could not be matched to a single master table with confidence."}
              </p>
              {!!result.candidates?.length && (
                <p className="mt-2">
                  <span className="text-muted-foreground">Possible targets: </span>
                  <span className="font-mono text-xs">{result.candidates.join(", ")}</span>
                </p>
              )}
            </div>
          ) : (
            <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-4">
              <div>
                <dt className="text-muted-foreground">Detected</dt>
                <dd className="font-medium">{result.detected_type ?? "—"}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Target master table</dt>
                <dd className="font-mono text-xs font-medium">
                  {result.target_master_table ?? "—"}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Records detected</dt>
                <dd className="font-medium">{result.records_detected}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">
                  {result.committed ? "Imported / rejected" : "Would import / reject"}
                </dt>
                <dd className="font-medium">
                  {result.committed
                    ? result.records_imported
                    : result.records_detected - result.records_failed}{" "}
                  / {result.records_failed}
                </dd>
              </div>
            </dl>
          )}

          {/* per-attachment routing */}
          {!!result.attachments?.length && (
            <div className="mt-3 overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-left text-muted-foreground">
                  <tr>
                    <th className="py-1 pr-3">Attachment</th>
                    <th className="py-1 pr-3">Format</th>
                    <th className="py-1 pr-3">Document type</th>
                    <th className="py-1 pr-3">Master table</th>
                    <th className="py-1">Note</th>
                  </tr>
                </thead>
                <tbody>
                  {result.attachments.map((a) => (
                    <tr key={a.filename} className="border-t border-border">
                      <td className="py-1 pr-3 font-mono">{a.filename}</td>
                      <td className="py-1 pr-3">{a.detected_format ?? "—"}</td>
                      <td className="py-1 pr-3">{a.document_type ?? "—"}</td>
                      <td className="py-1 pr-3 font-mono">{a.master_table ?? "—"}</td>
                      <td className="py-1 text-muted-foreground">
                        {a.reason ?? a.error ?? (a.confident ? "OK" : "—")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* validation errors for rejected records */}
          {!!result.errors?.length && (
            <details className="mt-3 text-xs">
              <summary className="cursor-pointer text-muted-foreground">
                {result.errors.length} validation issue(s)
              </summary>
              <ul className="mt-1 list-disc pl-5">
                {result.errors.slice(0, 50).map((e, i) => (
                  <li key={i}>
                    <span className="font-mono">{e.record_ref ?? "—"}</span>:{" "}
                    {e.error_detail ?? e.error_code}
                  </li>
                ))}
              </ul>
            </details>
          )}

          {/* Confirm step: only offered for a successful, uncommitted preview. */}
          {!result.committed && result.status !== "NEEDS_REVIEW" && selectedId !== null && (
            <div className="mt-4 flex items-center gap-2">
              <button
                type="button"
                className="rounded-md bg-severity-ok px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                onClick={() => doImport.mutate(selectedId)}
                disabled={doImport.isPending}
              >
                {doImport.isPending ? "Importing…" : "Confirm import"}
              </button>
              <button
                type="button"
                className="rounded-md border border-border px-3 py-1.5 text-sm"
                onClick={() => setResult(null)}
              >
                Cancel
              </button>
            </div>
          )}
        </Card>
      )}

      {/* ---- email detail -------------------------------------------------- */}
      {selectedId !== null && (
        <Card className="p-4">
          {detail.isLoading ? (
            <LoadingState label="Loading email…" />
          ) : !detail.data ? (
            <EmptyState>Email not found.</EmptyState>
          ) : (
            <div className="flex flex-col gap-3 text-sm">
              <div className="flex items-start justify-between gap-3">
                <h2 className="text-base font-semibold">{detail.data.subject || "(no subject)"}</h2>
                <button
                  type="button"
                  className="text-xs underline"
                  onClick={() => {
                    setSelectedId(null);
                    setResult(null);
                  }}
                >
                  close
                </button>
              </div>
              <dl className="grid grid-cols-1 gap-x-6 gap-y-1 sm:grid-cols-2">
                <div className="flex gap-2">
                  <dt className="text-muted-foreground">From</dt>
                  <dd>{detail.data.sender ?? "—"}</dd>
                </div>
                <div className="flex gap-2">
                  <dt className="text-muted-foreground">To</dt>
                  <dd>{detail.data.recipients ?? "—"}</dd>
                </div>
                <div className="flex gap-2">
                  <dt className="text-muted-foreground">CC</dt>
                  <dd>{detail.data.cc || "—"}</dd>
                </div>
                <div className="flex gap-2">
                  <dt className="text-muted-foreground">Received</dt>
                  <dd>{detail.data.received_at ? fmtDateTimeIST(detail.data.received_at) : "—"}</dd>
                </div>
              </dl>
              <div>
                <div className="mb-1 text-xs uppercase text-muted-foreground">Body</div>
                <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-md bg-muted/40 p-3 text-xs">
                  {detail.data.body_text || "(empty)"}
                </pre>
              </div>
              {!!detail.data.attachments?.length && (
                <div>
                  <div className="mb-1 text-xs uppercase text-muted-foreground">
                    Attachments ({detail.data.attachments.length})
                  </div>
                  <ul className="divide-y divide-border rounded-md border border-border">
                    {detail.data.attachments.map((a) => (
                      <li key={a.id} className="flex flex-wrap items-center gap-3 px-3 py-2">
                        <span className="font-mono text-xs">{a.filename}</span>
                        <span className="text-xs text-muted-foreground">
                          {a.content_type ?? "unknown"} · {bytes(a.size_bytes)}
                        </span>
                        {a.target_master_table && (
                          <span className="font-mono text-[11px] text-muted-foreground">
                            → {a.target_master_table}
                          </span>
                        )}
                        <StatusChip status={a.process_status} />
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
