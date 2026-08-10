// Query trace panel — JNPA Notice §1.d.
//
//   "The API queries used to obtain the underlying data, so the working can be
//    traced."
//
// Each entry shows the purpose, the API route that issued it, the row count, the
// SQL and its bound parameters — enough for a reviewer to re-run the query
// themselves.
//
// The `error` case is rendered in the critical colour and expanded by default.
// That is deliberate: a failed query and an empty table both return zero rows,
// and during the 06-Aug review a failure was reported as "no calls in this
// window" for a window that held 132 calls. A trace that hides its own failure
// is worse than no trace.

import { useState } from "react";
import { AlertTriangle, ChevronDown, ChevronRight, Database } from "lucide-react";
import type { SimQueryTrace } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

function TraceRow({ trace, index }: { trace: SimQueryTrace; index: number }) {
  const failed = Boolean(trace.error);
  // Failures start expanded — the operator must not have to go looking for them.
  const [open, setOpen] = useState(failed);

  return (
    <div
      className={cn(
        "rounded-md border",
        failed ? "border-severity-critical/50 bg-severity-critical/5" : "border-border",
      )}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-2.5 py-2 text-left"
      >
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        )}
        {failed ? (
          <AlertTriangle className="h-4 w-4 shrink-0 text-severity-critical" />
        ) : (
          <Database className="h-4 w-4 shrink-0 text-muted-foreground" />
        )}
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[12px] font-medium text-foreground">
            {index + 1}. {trace.purpose}
          </span>
          {trace.api && (
            <span className="block truncate font-mono text-[10.5px] text-muted-foreground">
              {trace.api}
            </span>
          )}
        </span>
        <span
          className={cn(
            "shrink-0 rounded px-1.5 py-0.5 text-[10.5px] font-semibold",
            failed
              ? "bg-severity-critical/15 text-severity-critical"
              : "bg-muted text-muted-foreground",
          )}
        >
          {failed ? "FAILED" : `${trace.row_count ?? 0} rows`}
        </span>
      </button>

      {open && (
        <div className="flex flex-col gap-2 border-t border-border/60 px-2.5 py-2">
          {failed && (
            <div className="rounded bg-severity-critical/10 p-2">
              <div className="text-[11px] font-semibold text-severity-critical">
                This query did not run
              </div>
              <div className="mt-0.5 font-mono text-[10.5px] leading-snug text-foreground/80">
                {trace.error}
              </div>
            </div>
          )}
          <div>
            <div className="mb-1 text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">
              SQL
            </div>
            <pre className="overflow-x-auto rounded bg-muted/60 p-2 font-mono text-[10.5px] leading-relaxed text-foreground">
              {trace.sql}
            </pre>
          </div>
          {Object.keys(trace.params || {}).length > 0 && (
            <div>
              <div className="mb-1 text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">
                Bound parameters
              </div>
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(trace.params).map(([k, v]) => (
                  <span
                    key={k}
                    className="rounded border border-border bg-background px-1.5 py-0.5 font-mono text-[10.5px] text-foreground"
                  >
                    {k} = {v === null || v === undefined ? "null" : String(v)}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function QueryTracePanel({ queries }: { queries: SimQueryTrace[] }) {
  if (!queries.length) return null;
  const failed = queries.filter((q) => q.error).length;

  return (
    <Card className="p-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-[13px] font-semibold text-foreground">
          Query trace <span className="text-muted-foreground">({queries.length})</span>
        </h3>
        {failed > 0 && (
          <span className="text-[10.5px] font-semibold text-severity-critical">
            {failed} quer{failed === 1 ? "y" : "ies"} failed — figures are incomplete
          </span>
        )}
      </div>
      <div className="flex flex-col gap-1.5">
        {queries.map((q, i) => (
          <TraceRow key={`${q.purpose}-${i}`} trace={q} index={i} />
        ))}
      </div>
      <p className="mt-2 border-t border-border pt-2 text-[10.5px] leading-snug text-muted-foreground">
        Every figure above rests on these queries. They are shown with their bound parameters so the
        working can be re-run and checked (JNPA Notice §1.d).
      </p>
    </Card>
  );
}
