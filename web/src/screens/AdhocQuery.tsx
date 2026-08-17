// S-08 — Ad-hoc Query.
//
// A query BUILDER, not a SQL box, and the difference is the point. A free-SQL
// endpoint would have one path, and RBAC in this gateway is enforced per path
// prefix — so a customs token and a transporter token would reach identical
// data through it, silently undoing every scoping rule in auth.py. `SELECT *
// FROM core.driver` is also a read, and a DPDP-sensitive dump of 31,846 licence
// records. So the caller picks a whitelisted dataset, its declared filters and a
// date window; the SERVER composes the statement.
//
// Nothing is lost for the evaluator, because the composed SQL comes back with
// the results and is shown verbatim. Notice §1(d) asks for the working to be
// traceable — it does not ask for the user to write it.

import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Database, Table2, Play } from "lucide-react";

import {
  PageContainer, PageHeader, StatGrid, StatCard, FilterSelect, StatusChip,
} from "@/components/ui/dtccc";
import { Card } from "@/components/ui/card";
import { LoadingState, ErrorState, EmptyState } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { usePortFocus } from "@/lib/focusStore";
import type { QueryDataset, QueryResult } from "@/lib/types";

export default function AdhocQuery() {
  const focus = usePortFocus();
  const [dataset, setDataset] = useState("cargo");
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [from, setFrom] = useState(focus.fromDate ?? "");
  const [to, setTo] = useState(focus.toDate ?? "");
  const [runToken, setRunToken] = useState(0);
  const [showSql, setShowSql] = useState(false);

  const cat = useQuery({
    queryKey: ["query-datasets"],
    queryFn: () => api.queryDatasets(),
  });

  const spec: QueryDataset | undefined = useMemo(
    () => cat.data?.datasets.find((d) => d.key === dataset),
    [cat.data, dataset],
  );

  // Changing dataset clears the filters. Carrying `vessel_name` into a dataset
  // that has no such column would send a request the API rejects, and carrying
  // it into one that DOES would silently re-scope a different question.
  useEffect(() => setFilters({}), [dataset]);

  // A focused vessel or container pre-fills the matching filter, if this
  // dataset exposes one — the same courtesy the rest of the estate gives.
  useEffect(() => {
    if (!spec) return;
    const seed: Record<string, string> = {};
    if (focus.vesselName && spec.filters.includes("vessel_name")) {
      seed.vessel_name = focus.vesselName;
    }
    if (focus.containerNo) {
      const col = spec.filters.find((f) => f === "container_no" || f === "container_number");
      if (col) seed[col] = focus.containerNo;
    }
    if (Object.keys(seed).length) setFilters((prev) => ({ ...seed, ...prev }));
  }, [spec, focus.vesselName, focus.containerNo, focus.nonce]);

  const filterParam = Object.entries(filters)
    .filter(([, v]) => v.trim())
    .map(([k, v]) => `${k}=${v.trim()}`)
    .join(",");

  const res = useQuery<QueryResult>({
    queryKey: ["adhoc-query", dataset, filterParam, from, to, runToken],
    queryFn: () => api.runQuery(dataset, {
      filters: filterParam || undefined,
      from_date: from || undefined,
      to_date: to || undefined,
    }),
    enabled: runToken > 0,
  });

  const cols = res.data?.rows.length ? Object.keys(res.data.rows[0]) : spec?.columns ?? [];

  return (
    <PageContainer>
      <PageHeader
        icon={Database}
        title="Ad-hoc Query"
        subtitle="Question any dataset in the canonical model, and see the exact SQL that answered it"
        updatedAt={res.dataUpdatedAt}
        isFetching={res.isFetching}
        onRefresh={() => setRunToken((n) => n + 1)}
      />

      <div className="flex flex-col gap-4 p-4">
        <Card className="p-4">
          {cat.isLoading && <LoadingState />}
          {cat.isError && <ErrorState onRetry={() => cat.refetch()}
                                      detail="The query catalogue is unavailable." />}
          {cat.data && (
            <>
              <div className="flex flex-wrap items-end gap-3">
                <label className="text-xs">
                  <div className="mb-1 text-muted-foreground">Dataset</div>
                  <FilterSelect
                    value={dataset}
                    onChange={setDataset}
                    options={cat.data.datasets.map((d) => ({ value: d.key, label: d.label }))}
                  />
                </label>
                <label className="text-xs">
                  <div className="mb-1 text-muted-foreground">From</div>
                  <input type="date" value={from} onChange={(e) => setFrom(e.target.value)}
                         disabled={!spec?.date_column}
                         className="rounded border border-border bg-transparent px-2 py-1.5 text-sm disabled:opacity-40" />
                </label>
                <label className="text-xs">
                  <div className="mb-1 text-muted-foreground">To</div>
                  <input type="date" value={to} onChange={(e) => setTo(e.target.value)}
                         disabled={!spec?.date_column}
                         className="rounded border border-border bg-transparent px-2 py-1.5 text-sm disabled:opacity-40" />
                </label>
                <button
                  type="button"
                  onClick={() => setRunToken((n) => n + 1)}
                  className="inline-flex items-center gap-1.5 rounded bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:opacity-90"
                >
                  <Play className="h-3.5 w-3.5" /> Run
                </button>
              </div>

              {/* Only the columns this dataset declares filterable. A box for a
                  column the API will reject is worse than no box. */}
              {spec && spec.filters.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-2">
                  {spec.filters.map((f) => (
                    <label key={f} className="text-xs">
                      <div className="mb-1 text-muted-foreground">{f}</div>
                      <input
                        value={filters[f] ?? ""}
                        onChange={(e) => setFilters((p) => ({ ...p, [f]: e.target.value }))}
                        placeholder="any"
                        className="w-44 rounded border border-border bg-transparent px-2 py-1.5 text-sm"
                      />
                    </label>
                  ))}
                </div>
              )}

              {spec?.note && (
                <p className="mt-3 text-xs text-muted-foreground">{spec.note}</p>
              )}
              {spec && !spec.date_column && (
                <p className="mt-1 text-xs text-muted-foreground italic">
                  This dataset carries no timestamp, so a date range cannot be applied to it.
                </p>
              )}
            </>
          )}
        </Card>

        {runToken === 0 && (
          <EmptyState>Choose a dataset and press Run.</EmptyState>
        )}
        {runToken > 0 && res.isLoading && <LoadingState />}
        {runToken > 0 && res.isError && (
          <ErrorState onRetry={() => res.refetch()} detail="The query did not run." />
        )}

        {res.data && (
          <>
            <StatGrid>
              <StatCard icon={Table2} label="Rows returned" value={res.data.count}
                        sub={res.data.truncated ? "capped — narrow the filters" : undefined}
                        tone={res.data.truncated ? "warn" : "ok"} />
              <StatCard icon={Database} label="Table" value={res.data.table} />
              <StatCard icon={Table2} label="Date window"
                        value={res.data.window_applied ? "applied" : "not applied"}
                        tone={res.data.window_applied ? "ok" : "neutral"} />
            </StatGrid>

            {res.data.error && (
              <Card className="border-l-4 p-3 text-sm" style={{ borderLeftColor: "var(--warn, #b45309)" }}>
                The query could not run: <code>{res.data.error}</code>
              </Card>
            )}

            <Card>
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-border text-left">
                      {cols.map((c) => (
                        <th key={c} className="whitespace-nowrap px-3 py-2 font-semibold">{c}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {res.data.rows.map((r, i) => (
                      <tr key={i} className="border-b border-border last:border-0">
                        {cols.map((c) => (
                          <td key={c} className="whitespace-nowrap px-3 py-1.5 font-mono">
                            {r[c] === null || r[c] === undefined
                              ? <span className="text-muted-foreground">—</span>
                              : String(r[c])}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {res.data.rows.length === 0 && !res.data.error && (
                <EmptyState>
                  No rows match. Over this corpus that is frequently the correct
                  answer rather than a fault — the datasets describe different
                  container sets.
                </EmptyState>
              )}
            </Card>

            {/* Notice §1(d): the working, exactly as the server composed it. */}
            <Card>
              <button
                type="button"
                onClick={() => setShowSql((v) => !v)}
                className="flex w-full items-center gap-2 px-4 py-2 text-left text-sm font-semibold hover:bg-muted/40"
              >
                <Database className="h-4 w-4" /> Show the working
                <StatusChip label="server-composed" tone="ok" />
              </button>
              {showSql && (
                <div className="space-y-2 px-4 pb-3 text-xs">
                  <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-muted/40 p-2 font-mono">
                    {res.data.sql}
                  </pre>
                  <div className="text-muted-foreground">
                    params {JSON.stringify(res.data.params)}
                  </div>
                </div>
              )}
            </Card>
          </>
        )}
      </div>
    </PageContainer>
  );
}
