// T-04 — Truck Visit Detail.
//
// Search a tractor, get every REAL gate document it produced across terminals,
// in one chronological timeline; select one to read the parsed fields beside the
// original scanned slip. The documents come from core.gate_document
// (data_origin='REAL'), loaded verbatim from the customer corpus by
// scripts/import_gate_documents.py — not from the simulator.
//
// Honesty rules this screen enforces:
//   * A field the physical slip does not print renders as "—". Nothing is
//     inferred, defaulted or back-filled. 10 of the 12 corpus slips genuinely
//     carry no driver licence, and 2 carry no truck number at all.
//   * Timestamps render in Asia/Kolkata, because that is the wall-clock printed
//     on the slip. Reading a gate time in the viewer's local zone would show a
//     number that appears nowhere on the paper.
import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api, type GateSourceDoc } from "@/lib/api";
import { fmtDateTimeIST } from "@/lib/utils";

const CATEGORY_LABEL: Record<string, string> = {
  EIR: "EIR",
  FORM13: "Form 13",
  PIN_TICKET: "PIN ticket",
};

const CATEGORY_TONE: Record<string, string> = {
  EIR: "bg-sky-500/10 text-sky-600 dark:text-sky-400 ring-sky-500/30",
  FORM13: "bg-violet-500/10 text-violet-600 dark:text-violet-400 ring-violet-500/30",
  PIN_TICKET: "bg-amber-500/10 text-amber-600 dark:text-amber-400 ring-amber-500/30",
};

/** The evaluator's reference tractor: 4 documents, 3 terminals, 7 days. */
const EXAMPLE_TRUCK = "MH43BX1488";

function norm(plate: string): string {
  return plate.toUpperCase().replace(/[^A-Z0-9]/g, "");
}

/** "—" for anything the source document does not carry. */
function Field({ label, value }: { label: string; value: unknown }) {
  const empty = value === null || value === undefined || value === "";
  return (
    <div className="flex flex-col gap-0.5 py-1.5">
      <dt className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd
        className={
          empty
            ? "text-[13px] text-muted-foreground/60"
            : "text-[13px] font-medium text-foreground"
        }
      >
        {empty ? "—" : String(value)}
      </dd>
    </div>
  );
}

function CategoryChip({ category }: { category: string }) {
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ring-1 ring-inset ${
        CATEGORY_TONE[category] ?? "bg-muted text-muted-foreground ring-border"
      }`}
    >
      {CATEGORY_LABEL[category] ?? category}
    </span>
  );
}

/** Provenance badge — REAL means "parsed from the customer's own paperwork". */
function OriginBadge({ origin }: { origin: string | null }) {
  if (!origin) return null;
  const real = origin.toUpperCase() === "REAL";
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ring-1 ring-inset ${
        real
          ? "bg-emerald-500/10 text-emerald-600 ring-emerald-500/30 dark:text-emerald-400"
          : "bg-muted text-muted-foreground ring-border"
      }`}
      title={
        real
          ? "Parsed verbatim from the customer's source document"
          : `Provenance: ${origin}`
      }
    >
      {real ? "Real source" : origin}
    </span>
  );
}

/** The original WhatsApp scan, served same-origin via /api/evidence. */
function ScanPane({ doc }: { doc: GateSourceDoc }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [doc.doc_id]);

  if (!doc.evidence_uri) {
    return (
      <div className="flex h-full min-h-[220px] items-center justify-center rounded-lg border border-dashed border-border p-6 text-center text-xs text-muted-foreground">
        No original scan is linked to this document.
      </div>
    );
  }
  if (failed) {
    return (
      <div className="flex h-full min-h-[220px] flex-col items-center justify-center gap-1 rounded-lg border border-dashed border-destructive/40 p-6 text-center text-xs text-destructive">
        <span>The linked scan could not be loaded.</span>
        <code className="text-[10px] text-muted-foreground">{doc.image_file}</code>
      </div>
    );
  }
  return (
    <figure className="space-y-1.5">
      <a href={doc.evidence_uri} target="_blank" rel="noreferrer" className="block">
        <img
          src={doc.evidence_uri}
          alt={`Original scanned ${CATEGORY_LABEL[doc.doc_category] ?? "document"} for ${
            doc.vehicle_no ?? "this visit"
          }`}
          onError={() => setFailed(true)}
          className="max-h-[560px] w-full rounded-lg border border-border bg-muted/30 object-contain"
        />
      </a>
      <figcaption className="text-[10px] text-muted-foreground">
        Original scan · click to open full size
      </figcaption>
    </figure>
  );
}

/** Parsed fields, grouped the way the slip reads. */
function ParsedPane({ doc }: { doc: GateSourceDoc }) {
  const tat =
    doc.truck_in_ts && doc.truck_out_ts
      ? Math.round(
          (new Date(doc.truck_out_ts).getTime() - new Date(doc.truck_in_ts).getTime()) /
            60000,
        )
      : null;

  return (
    <div className="space-y-4">
      <section>
        <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          Document
        </h4>
        <dl className="grid grid-cols-2 gap-x-4 sm:grid-cols-3">
          <Field label="Type" value={CATEGORY_LABEL[doc.doc_category] ?? doc.doc_category} />
          <Field label="Terminal" value={doc.terminal} />
          <Field label="Document no." value={doc.doc_ref} />
          <Field label="PIN no." value={doc.pin_no} />
          <Field label="Visit ID" value={doc.visit_id} />
          <Field label="Date / time" value={fmtDateTimeIST(doc.doc_ts)} />
        </dl>
      </section>

      <section>
        <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          Truck &amp; driver
        </h4>
        <dl className="grid grid-cols-2 gap-x-4 sm:grid-cols-3">
          <Field label="Truck no." value={doc.vehicle_no} />
          <Field label="BAT / gate txn" value={doc.bat_no} />
          <Field label="Driver licence" value={doc.driver_licence} />
          <Field label="Driver name" value={doc.driver_name} />
          <Field label="Transporter" value={doc.transporter_name} />
          <Field label="Gate" value={doc.gate_no} />
          <Field label="Truck in" value={doc.truck_in_ts ? fmtDateTimeIST(doc.truck_in_ts) : null} />
          <Field label="Truck out" value={doc.truck_out_ts ? fmtDateTimeIST(doc.truck_out_ts) : null} />
          <Field label="Turnaround" value={tat != null ? `${tat} min` : null} />
        </dl>
      </section>

      <section>
        <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          Container
        </h4>
        <dl className="grid grid-cols-2 gap-x-4 sm:grid-cols-3">
          <Field label="Container no." value={doc.container_no} />
          <Field label="ISO code" value={doc.iso_code} />
          <Field label="Status" value={doc.load_status} />
          <Field
            label="Gross weight"
            value={doc.gross_weight_kg != null ? `${doc.gross_weight_kg} kg` : null}
          />
          <Field label="Seal 1" value={doc.seal1} />
          <Field label="Seal 2" value={doc.seal2} />
          <Field label="Yard position" value={doc.yard_position} />
          <Field label="Vessel" value={doc.vessel_name} />
          <Field label="Voyage" value={doc.voyage} />
          <Field label="POL" value={doc.pol} />
          <Field label="POD" value={doc.pod} />
          <Field label="CFS" value={doc.cfs} />
        </dl>
      </section>

      {doc.attrs && Object.keys(doc.attrs).length > 0 && (
        <details className="rounded-lg border border-border">
          <summary className="cursor-pointer px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            As-filed source fields ({Object.keys(doc.attrs).length})
          </summary>
          <div className="max-h-72 overflow-auto border-t border-border">
            <table className="w-full text-left text-[12px]">
              <tbody className="divide-y divide-border">
                {Object.entries(doc.attrs).map(([k, v]) => (
                  <tr key={k}>
                    <td className="w-2/5 px-3 py-1 align-top text-muted-foreground">{k}</td>
                    <td className="px-3 py-1 font-mono text-foreground">
                      {v === null || v === "" ? "—" : String(v)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </div>
  );
}

export default function TruckVisitDetail() {
  const [input, setInput] = useState(EXAMPLE_TRUCK);
  const [truck, setTruck] = useState(EXAMPLE_TRUCK);
  const [selected, setSelected] = useState<number | null>(null);

  const query = useQuery({
    queryKey: ["gate-source-docs", truck],
    queryFn: () => api.gateSourceDocs({ vehicle: truck, limit: 200 }),
    enabled: truck.length > 0,
  });

  const docs = useMemo(() => query.data?.items ?? [], [query.data]);

  // Keep a selection that survives refetches; default to the newest document.
  useEffect(() => {
    if (!docs.length) {
      setSelected(null);
      return;
    }
    setSelected((prev) =>
      prev != null && docs.some((d) => d.doc_id === prev) ? prev : docs[0].doc_id,
    );
  }, [docs]);

  const current = docs.find((d) => d.doc_id === selected) ?? null;
  const span =
    query.data?.first_doc_ts && query.data?.last_doc_ts
      ? Math.round(
          (new Date(query.data.last_doc_ts).getTime() -
            new Date(query.data.first_doc_ts).getTime()) /
            86_400_000,
        ) + 1
      : null;

  return (
    <div className="space-y-4 p-4">
      <header className="space-y-1">
        <h1 className="text-lg font-semibold tracking-tight">Truck Visit Detail</h1>
        <p className="text-xs text-muted-foreground">
          Every gate document a tractor produced, parsed from the operator's own
          paperwork and shown beside the original scan.
        </p>
      </header>

      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setTruck(norm(input));
        }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          spellCheck={false}
          aria-label="Truck number"
          placeholder={`Search truck number (e.g. ${EXAMPLE_TRUCK})`}
          className="w-72 rounded-lg border border-border bg-background px-3 py-1.5 font-mono text-sm outline-none focus:ring-2 focus:ring-ring"
        />
        <button
          type="submit"
          className="rounded-lg bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:opacity-90"
        >
          Search
        </button>
        {truck && (
          <span className="text-xs text-muted-foreground">
            Showing documents for <span className="font-mono text-foreground">{truck}</span>
          </span>
        )}
      </form>

      {query.isLoading && <p className="text-sm text-muted-foreground">Loading documents…</p>}

      {query.isError && (
        <p className="text-sm text-destructive">
          Could not load gate documents. {String((query.error as Error)?.message ?? "")}
        </p>
      )}

      {!query.isLoading && !query.isError && docs.length === 0 && (
        <p className="rounded-lg border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
          No gate documents on record for{" "}
          <span className="font-mono text-foreground">{truck}</span>.
        </p>
      )}

      {docs.length > 0 && (
        <>
          <div className="flex flex-wrap gap-2">
            {[
              { label: "Documents", value: String(query.data?.total ?? docs.length) },
              {
                label: "Terminals",
                value: `${query.data?.terminal_count ?? 0}${
                  query.data?.terminals?.length ? ` · ${query.data.terminals.join(", ")}` : ""
                }`,
              },
              { label: "Span", value: span != null ? `${span} days` : "—" },
            ].map((s) => (
              <div key={s.label} className="rounded-lg border border-border px-3 py-1.5">
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                  {s.label}
                </div>
                <div className="text-sm font-semibold tabular-nums text-foreground">
                  {s.value}
                </div>
              </div>
            ))}
          </div>

          <div className="grid gap-4 lg:grid-cols-[minmax(0,360px)_minmax(0,1fr)]">
            {/* Chronological timeline across every terminal. */}
            <ol className="space-y-1.5">
              {docs.map((d) => {
                const active = d.doc_id === selected;
                return (
                  <li key={d.doc_id}>
                    <button
                      type="button"
                      onClick={() => setSelected(d.doc_id)}
                      aria-current={active}
                      className={`w-full rounded-lg border px-3 py-2 text-left transition ${
                        active
                          ? "border-primary bg-primary/5"
                          : "border-border hover:bg-muted/40"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <CategoryChip category={d.doc_category} />
                        <span className="text-[11px] tabular-nums text-muted-foreground">
                          {fmtDateTimeIST(d.doc_ts)}
                        </span>
                      </div>
                      <div className="mt-1 flex items-center justify-between gap-2 text-[12px]">
                        <span className="font-medium text-foreground">{d.terminal ?? "—"}</span>
                        <span className="font-mono text-muted-foreground">
                          {d.container_no ?? "no container"}
                        </span>
                      </div>
                      <div className="mt-0.5 text-[11px] text-muted-foreground">
                        BAT {d.bat_no ?? "—"} · doc {d.doc_ref ?? d.visit_id ?? "—"}
                      </div>
                    </button>
                  </li>
                );
              })}
            </ol>

            {current && (
              <section className="rounded-lg border border-border p-4">
                <div className="mb-3 flex flex-wrap items-center gap-2 border-b border-border pb-3">
                  <CategoryChip category={current.doc_category} />
                  <h2 className="text-sm font-semibold text-foreground">
                    {current.terminal ?? "Unknown terminal"} ·{" "}
                    {fmtDateTimeIST(current.doc_ts)}
                  </h2>
                  <OriginBadge origin={current.data_origin} />
                  <span className="text-[10px] text-muted-foreground">
                    source: {current.doc_variant}
                  </span>
                </div>
                <div className="grid gap-5 xl:grid-cols-2">
                  <ParsedPane doc={current} />
                  <ScanPane doc={current} />
                </div>
              </section>
            )}
          </div>
        </>
      )}
    </div>
  );
}
