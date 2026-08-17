// S-06 — Evidence & Audit Explorer ("Vessel Thread").
//
// The one screen where a figure resolves all the way back: hop -> the table it
// came from -> the corpus file family behind that table -> the SQL that produced
// it. JNPA Notice §1(d) requires "the API queries used to obtain the underlying
// data, so the working can be traced", and until now that trace existed only in
// the what-if results.
//
// THE POINT OF THIS SCREEN IS THE EMPTY HOPS. Backed by GET /api/thread/*, which
// visits all 18 lifecycle hops and returns a verdict for every one. Measured on
// the live database, only 17 of 11,957 containers reach a truck through JNPA's
// own gate paperwork, because the manifest set and the gate-document set share no
// containers. A screen that rendered only the hops WITH data would present that
// as a complete chain. So a hop with nothing in it is drawn at the same weight as
// one with everything, and says which table was searched.
//
// Three verdicts, never merged:
//   FOUND          the corpus evidences this step
//   NOT_IN_CORPUS  we looked, and JNPA supplied nothing for it
//   ERROR          we could not look — a real fault, not an absence
//
// Synthetic rows are called out per hop from the API's own `synthetic` flag, so a
// demonstration fixture can never be read as a JNPA document.

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Boxes, Ship, Truck, FileSearch, AlertTriangle, Database } from "lucide-react";

import {
  PageContainer, PageHeader, StatGrid, StatCard, SearchInput, StatusChip,
} from "@/components/ui/dtccc";
import { Card } from "@/components/ui/card";
import { LoadingState, ErrorState, EmptyState } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { usePortFocus, focusStore } from "@/lib/focusStore";
import { fmtDateTimeIST } from "@/lib/utils";
import type { ThreadHop, ThreadResponse } from "@/lib/types";
import { describeVehicleAttribution, verdictLabel, verdictTone } from "@/lib/thread";

/** Worked examples — every one a real corpus container (see 01/02_GOLDEN_THREAD). */
const EXAMPLES: Array<{ no: string; note: string }> = [
  { no: "DPWU9011100", note: "the one complete import chain — manifest → gate-out → truck" },
  { no: "MEDU1777575", note: "export — Form 13 → truck" },
  { no: "NYKU4768188", note: "EIR with driver and turnaround time" },
  { no: "DFSU1691030", note: "stops at the delivery order — no gate record exists" },
  { no: "ONEU2122848", note: "empty cycle — ECY → CFS" },
];



function HopRow({ hop }: { hop: ThreadHop }) {
  const [open, setOpen] = useState(false);
  const found = hop.verdict === "FOUND";
  return (
    <div className="border-b border-border last:border-0">
      <button
        type="button"
        onClick={() => found && setOpen((o) => !o)}
        className="flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-muted/40"
        aria-expanded={open}
      >
        <StatusChip label={verdictLabel(hop.verdict)} tone={verdictTone(hop.verdict)} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className={found ? "font-semibold" : "text-muted-foreground"}>{hop.label}</span>
            <span className="text-xs text-muted-foreground">{hop.stage}</span>
            {hop.synthetic && (
              <StatusChip label="SYNTHETIC — demonstration data" tone="warn" />
            )}
            {hop.vehicles.length > 0 && (
              <span className="inline-flex items-center gap-1 text-xs">
                <Truck className="h-3 w-3" /> {hop.vehicles.join(", ")}
              </span>
            )}
          </div>
          {/* Always name the table, found or not: "we looked here and there was
              nothing" is the answer, and it is only credible if it says where. */}
          <div className="mt-0.5 text-xs text-muted-foreground">
            <code>{hop.source_table}</code>
            {hop.source_files ? <> · {hop.source_files}</> : null}
            {hop.provenance.length > 0 ? <> · {hop.provenance.join(", ")}</> : null}
          </div>
          {!found && hop.note && (
            <div className="mt-1 text-xs text-muted-foreground italic">{hop.note}</div>
          )}
        </div>
        {found && <span className="text-xs text-muted-foreground">{hop.row_count} row(s)</span>}
      </button>
      {open && found && (
        <div className="overflow-x-auto bg-muted/30 px-4 pb-3">
          <table className="w-full text-xs">
            <tbody>
              {hop.rows.map((r, i) => (
                <tr key={i} className="align-top">
                  <td className="py-1 pr-3">
                    {Object.entries(r)
                      .filter(([, v]) => v !== null && v !== undefined && v !== "")
                      .map(([k, v]) => (
                        <span key={k} className="mr-3 inline-block whitespace-nowrap">
                          <span className="text-muted-foreground">{k}</span>{" "}
                          <span className="font-mono">{String(v)}</span>
                        </span>
                      ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function VesselThread() {
  const focus = usePortFocus();
  const [term, setTerm] = useState(focus.containerNo ?? "");
  const [submitted, setSubmitted] = useState(focus.containerNo ?? "");
  const [showSql, setShowSql] = useState(false);

  const q = useQuery<ThreadResponse>({
    queryKey: ["thread-container", submitted],
    queryFn: () => api.containerThread(submitted),
    enabled: Boolean(submitted),
  });

  const run = (value: string) => {
    const v = value.trim().toUpperCase();
    setTerm(v);
    setSubmitted(v);
    // Publishing the box focuses the rest of the estate on it too.
    if (v) focusStore.refine({ containerNo: v }, "UC-3");
  };

  const s = q.data?.summary;

  return (
    <PageContainer>
      <PageHeader
        icon={FileSearch}
        title="Evidence & Audit Explorer"
        subtitle="Every recorded step of one container's life, the table behind it, and the query that produced it"
        updatedAt={q.dataUpdatedAt}
        isFetching={q.isFetching}
        onRefresh={() => q.refetch()}
      />

      <div className="flex flex-col gap-4 p-4">
        <Card className="p-4">
          {/* A form so Enter submits — SearchInput is a plain controlled input. */}
          <form
            className="flex items-center gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              run(term);
            }}
          >
            <SearchInput
              value={term}
              onChange={setTerm}
              placeholder="Container number, e.g. DPWU9011100"
              className="flex-1"
            />
            <button
              type="submit"
              className="rounded bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:opacity-90"
            >
              Trace
            </button>
          </form>
          <div className="mt-3 flex flex-wrap gap-2">
            {EXAMPLES.map((e) => (
              <button
                key={e.no}
                type="button"
                onClick={() => run(e.no)}
                title={e.note}
                className="rounded border border-border px-2 py-1 text-xs hover:bg-muted"
              >
                <code>{e.no}</code> — {e.note}
              </button>
            ))}
          </div>
        </Card>

        {!submitted && (
          <EmptyState>
            Search a container — every one of the 18 lifecycle steps is checked,
            including the ones with nothing in them.
          </EmptyState>
        )}
        {submitted && q.isLoading && <LoadingState />}
        {submitted && q.isError && (
          <ErrorState
            onRetry={() => q.refetch()}
            detail="The thread service did not answer. Start the gateway to trace a container."
          />
        )}

        {s && (
          <>
            <StatGrid>
              <StatCard icon={Boxes} label="Steps evidenced" value={`${s.hops_found} / ${s.hops_total}`} tone="ok" />
              <StatCard icon={Ship} label="Not in corpus" value={s.hops_not_in_corpus} tone="neutral"
                        sub="JNPA supplied nothing for these" />
              <StatCard icon={Truck} label="Reaches a truck" value={s.reaches_a_vehicle ? "Yes" : "No"}
                        tone={s.reaches_a_vehicle ? "ok" : "neutral"}
                        sub={s.vehicle_count ? `${s.vehicle_count} vehicle(s)` : undefined} />
              <StatCard icon={AlertTriangle} label="Could not read" value={s.hops_errored}
                        tone={s.hops_errored ? "warn" : "neutral"}
                        sub="a fault, not an absence" />
            </StatGrid>

            {s.has_synthetic_hops && (
              <Card className="border-l-4 p-3 text-sm" style={{ borderLeftColor: "var(--warn, #b45309)" }}>
                <b>This chain includes demonstration data.</b> The steps marked SYNTHETIC
                below ({s.synthetic_hops.join(", ")}) were generated so the flow could be shown
                end to end. They are not JNPA documents and must not be quoted as operational fact.
              </Card>
            )}

            <Card>
              <div className="border-b border-border px-4 py-2 text-sm font-semibold">
                Lifecycle — all {s.hops_total} steps
              </div>
              {q.data!.hops.map((h) => <HopRow key={h.hop} hop={h} />)}
            </Card>

            {q.data!.vehicles.length > 0 && (
              <Card>
                <div className="border-b border-border px-4 py-2 text-sm font-semibold">Trucks</div>
                {q.data!.vehicles.map((v, i) => {
                  // The PLATE is always real — it is read off a gate document or a
                  // CODECO message. What may be assumed is the TRANSPORTER behind
                  // it, because `11-Transport Data` has no vehicle-registration
                  // column to resolve one (defect B1). Label the bridge, not the truck.
                  const attribution = describeVehicleAttribution(v);
                  return (
                    <div key={i} className="flex flex-wrap items-center gap-3 px-4 py-2 text-sm">
                      <code className="font-semibold">{v.plate}</code>
                      <span className={v.transporter ? undefined : "text-muted-foreground italic"}>
                        {v.transporter ?? "no transporter resolves"}
                      </span>
                      {v.driver_name ? (
                        <span>
                          driver {v.driver_name}
                          {v.driver_licence ? <> · {v.driver_licence}</> : null}
                        </span>
                      ) : null}
                      <StatusChip label={attribution.label} tone={attribution.tone} />
                    </div>
                  );
                })}
                <p className="px-4 pb-3 text-xs text-muted-foreground">
                  Plate numbers are read from the gate document or CODECO message and are
                  always JNPA data. The chip describes the transporter attribution only.
                </p>
              </Card>
            )}

            {/* Notice §1(d): the working must be traceable to the queries used. */}
            <Card>
              <button
                type="button"
                onClick={() => setShowSql((v) => !v)}
                className="flex w-full items-center gap-2 px-4 py-2 text-left text-sm font-semibold hover:bg-muted/40"
              >
                <Database className="h-4 w-4" />
                Show the working — {q.data!.queries.length} queries
              </button>
              {showSql && (
                <div className="space-y-2 px-4 pb-3">
                  {q.data!.queries.map((qq, i) => (
                    <div key={i} className="rounded bg-muted/40 p-2 text-xs">
                      <div className="mb-1 flex flex-wrap gap-2 text-muted-foreground">
                        <b>{qq.hop}</b>
                        <span>{qq.row_count} row(s)</span>
                        {qq.error ? <span className="text-amber-700">error: {qq.error}</span> : null}
                      </div>
                      <pre className="overflow-x-auto whitespace-pre-wrap font-mono">{qq.sql}</pre>
                      <div className="mt-1 text-muted-foreground">
                        params {JSON.stringify(qq.params)}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Card>

            <p className="text-xs text-muted-foreground">
              Read at {fmtDateTimeIST(new Date(q.dataUpdatedAt).toISOString())}. Steps marked
              “Not in corpus” are an answer, not a fault: JNPA’s manifest files and gate-document
              files describe different containers, so most boxes have no gate paperwork at all.
            </p>
          </>
        )}
      </div>
    </PageContainer>
  );
}
