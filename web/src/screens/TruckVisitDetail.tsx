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
//     inferred, defaulted or back-filled. Most corpus slips genuinely carry no
//     driver licence, and some carry no truck number at all.
//   * Timestamps render in Asia/Kolkata, because that is the wall-clock printed
//     on the slip. Reading a gate time in the viewer's local zone would show a
//     number that appears nowhere on the paper.
//   * Every count on screen (documents, terminals, span) is derived from the API
//     response. Nothing about a particular tractor is hard-coded.
//
// Layout: the screen fills the shell's content box (which is `overflow-hidden`)
// and scrolls *inside* its panes rather than scrolling the page. On desktop that
// is three independent columns — document list │ parsed detail │ original scan —
// so the scan stays visible while you read the fields. Below `lg` the panes
// collapse to one column and the body scrolls as a whole, in the reading order
// list → detail → scan.
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { ExternalLink, Search } from "lucide-react";

import { api, type GateSourceDoc } from "@/lib/api";
import { cn, fmtDateTimeIST } from "@/lib/utils";

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

/** Seed for the search box only — every figure on screen comes from the API. */
const EXAMPLE_TRUCK = "MH43BX1488";

/** Display-only IST splitters for the timeline, matching lib/utils' timezone. */
const IST = "Asia/Kolkata";

function fmtDayIST(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-IN", { timeZone: IST, day: "2-digit", month: "short" });
}

function fmtHmIST(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString("en-IN", {
    timeZone: IST,
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
  });
}

function norm(plate: string): string {
  return plate.toUpperCase().replace(/[^A-Z0-9]/g, "");
}

/**
 * "—" for anything the source document does not carry.
 *
 * `mono` is for identifiers (container, licence, document numbers): they get a
 * tabular face and wrap on any character, so a long reference lengthens the card
 * instead of widening it and forcing the page to scroll sideways.
 */
function Field({ label, value, mono = false }: { label: string; value: unknown; mono?: boolean }) {
  const empty = value === null || value === undefined || value === "";
  const text = empty ? "—" : String(value);
  return (
    <div className="min-w-0 py-1.5">
      <dt className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd
        title={empty ? undefined : text}
        className={cn(
          "mt-0.5 text-[13px] leading-snug",
          mono && "font-mono break-all",
          !mono && "break-words",
          empty ? "text-muted-foreground/60" : "font-medium text-foreground",
        )}
      >
        {text}
      </dd>
    </div>
  );
}

/** One titled block of fields inside the detail pane. */
function FieldGroup({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="min-w-0 rounded-lg border border-border bg-card p-3">
      <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </h3>
      <dl className="grid grid-cols-1 gap-x-4 sm:grid-cols-2">{children}</dl>
    </section>
  );
}

function CategoryChip({ category, className }: { category: string; className?: string }) {
  return (
    <span
      className={cn(
        "shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ring-1 ring-inset",
        CATEGORY_TONE[category] ?? "bg-muted text-muted-foreground ring-border",
        className,
      )}
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
      className={cn(
        "shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ring-1 ring-inset",
        real
          ? "bg-emerald-500/10 text-emerald-600 ring-emerald-500/30 dark:text-emerald-400"
          : "bg-muted text-muted-foreground ring-border",
      )}
      title={real ? "Parsed verbatim from the customer's source document" : `Provenance: ${origin}`}
    >
      {real ? "Real source" : origin}
    </span>
  );
}

/** Compact stat for the header band. */
function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="min-w-0 rounded-lg border border-border bg-card px-3 py-1.5">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="truncate text-sm font-semibold tabular-nums text-foreground">{value}</div>
      {hint && (
        <div className="truncate text-[10px] text-muted-foreground" title={hint}>
          {hint}
        </div>
      )}
    </div>
  );
}

/** The original scan, served same-origin via /api/evidence. */
function ScanPane({ doc }: { doc: GateSourceDoc }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [doc.doc_id]);

  return (
    <section className="flex min-h-0 min-w-0 flex-col rounded-lg border border-border bg-card">
      <header className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        <h2 className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          Original Scan
        </h2>
        <OriginBadge origin={doc.data_origin} />
        {doc.evidence_uri && !failed && (
          <a
            href={doc.evidence_uri}
            target="_blank"
            rel="noreferrer"
            className="ml-auto inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground transition hover:bg-muted/60 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ExternalLink className="h-3 w-3" aria-hidden />
            Open full size
          </a>
        )}
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {!doc.evidence_uri ? (
          <div className="flex h-full min-h-[180px] items-center justify-center rounded-lg border border-dashed border-border p-6 text-center text-xs text-muted-foreground">
            No original scan is linked to this document.
          </div>
        ) : failed ? (
          <div className="flex h-full min-h-[180px] flex-col items-center justify-center gap-1 rounded-lg border border-dashed border-destructive/40 p-6 text-center text-xs text-destructive">
            <span>The linked scan could not be loaded.</span>
            <code className="break-all text-[10px] text-muted-foreground">{doc.image_file}</code>
          </div>
        ) : (
          <figure className="space-y-1.5">
            <a
              href={doc.evidence_uri}
              target="_blank"
              rel="noreferrer"
              title="Open the original scan full size"
              className="block rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <img
                src={doc.evidence_uri}
                alt={`Original scanned ${CATEGORY_LABEL[doc.doc_category] ?? "document"} for ${
                  doc.vehicle_no ?? "this visit"
                }`}
                onError={() => setFailed(true)}
                className="max-h-[62vh] w-full rounded-lg border border-border bg-muted/30 object-contain"
              />
            </a>
            <figcaption className="text-[10px] text-muted-foreground">
              Click the scan to open it full size.
            </figcaption>
          </figure>
        )}
      </div>
    </section>
  );
}

/** Parsed fields, grouped the way the slip reads. */
function ParsedPane({ doc }: { doc: GateSourceDoc }) {
  const tat =
    doc.truck_in_ts && doc.truck_out_ts
      ? Math.round(
          (new Date(doc.truck_out_ts).getTime() - new Date(doc.truck_in_ts).getTime()) / 60000,
        )
      : null;

  const attrCount = doc.attrs ? Object.keys(doc.attrs).length : 0;

  return (
    <div className="space-y-3">
      <div className="grid gap-3 lg:grid-cols-2">
        <FieldGroup title="Document">
          <Field label="Type" value={CATEGORY_LABEL[doc.doc_category] ?? doc.doc_category} />
          <Field label="Terminal" value={doc.terminal} />
          <Field label="Document no." value={doc.doc_ref} mono />
          <Field label="PIN no." value={doc.pin_no} mono />
          <Field label="Visit ID" value={doc.visit_id} mono />
          <Field label="Date / time" value={fmtDateTimeIST(doc.doc_ts)} />
        </FieldGroup>

        <FieldGroup title="Truck &amp; driver">
          <Field label="Truck no." value={doc.vehicle_no} mono />
          <Field label="BAT / gate txn" value={doc.bat_no} mono />
          <Field label="Driver licence" value={doc.driver_licence} mono />
          <Field label="Driver name" value={doc.driver_name} />
          <Field label="Transporter" value={doc.transporter_name} />
        </FieldGroup>

        <FieldGroup title="Gate &amp; visit">
          <Field label="Gate" value={doc.gate_no} />
          <Field label="Turnaround" value={tat != null ? `${tat} min` : null} />
          <Field
            label="Truck in"
            value={doc.truck_in_ts ? fmtDateTimeIST(doc.truck_in_ts) : null}
          />
          <Field
            label="Truck out"
            value={doc.truck_out_ts ? fmtDateTimeIST(doc.truck_out_ts) : null}
          />
        </FieldGroup>

        <FieldGroup title="Container">
          <Field label="Container no." value={doc.container_no} mono />
          <Field label="ISO code" value={doc.iso_code} mono />
          <Field label="Status" value={doc.load_status} />
          <Field
            label="Gross weight"
            value={doc.gross_weight_kg != null ? `${doc.gross_weight_kg} kg` : null}
          />
          <Field label="Seal 1" value={doc.seal1} mono />
          <Field label="Seal 2" value={doc.seal2} mono />
          <Field label="Yard position" value={doc.yard_position} mono />
          <Field label="Group code" value={doc.group_code} mono />
        </FieldGroup>

        <FieldGroup title="Vessel &amp; voyage">
          <Field label="Vessel" value={doc.vessel_name} />
          <Field label="Voyage" value={doc.voyage} mono />
          <Field label="POL" value={doc.pol} />
          <Field label="POD" value={doc.pod} />
          <Field label="CFS" value={doc.cfs} />
          <Field label="Booking no." value={doc.booking_no} mono />
        </FieldGroup>
      </div>

      {attrCount > 0 && doc.attrs && (
        <details className="group rounded-lg border border-border bg-card">
          <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground transition hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <span className="text-muted-foreground transition-transform group-open:rotate-90">
              ›
            </span>
            As-filed source fields ({attrCount})
            <span className="ml-auto text-[10px] font-normal normal-case tracking-normal text-muted-foreground/70">
              verbatim from the source file
            </span>
          </summary>
          <div className="max-h-[280px] overflow-y-auto border-t border-border">
            <table className="w-full table-fixed text-left text-[12px]">
              <tbody className="divide-y divide-border">
                {Object.entries(doc.attrs).map(([k, v]) => (
                  <tr key={k} className="align-top">
                    <th
                      scope="row"
                      className="w-2/5 break-words px-3 py-1.5 text-left font-normal text-muted-foreground"
                    >
                      {k}
                    </th>
                    <td className="break-words px-3 py-1.5 font-mono text-foreground">
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

/** Compact horizontal rail of the visit, oldest → newest. */
function VisitTimeline({
  docs,
  selected,
  onSelect,
}: {
  docs: GateSourceDoc[];
  selected: number | null;
  onSelect: (id: number) => void;
}) {
  return (
    <div className="flex items-center gap-3">
      <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
        Visit timeline
      </span>
      <ol
        aria-label="Gate documents in chronological order"
        className="flex min-w-0 flex-1 items-stretch gap-2 overflow-x-auto pb-1 pt-0.5"
      >
        {docs.map((d, i) => {
          const active = d.doc_id === selected;
          const first = i === 0;
          const last = i === docs.length - 1;
          return (
            <li key={d.doc_id} className="relative flex shrink-0 flex-col items-center pt-1.5">
              {/* Rail: continuous across items, capped at the first and last dot. */}
              <span
                aria-hidden
                className={cn(
                  "absolute top-[7px] h-px bg-border",
                  first ? "left-1/2" : "left-0",
                  last ? "right-1/2" : "right-0",
                )}
              />
              <span
                aria-hidden
                className={cn(
                  "relative z-10 h-2.5 w-2.5 rounded-full ring-2 ring-background",
                  active ? "bg-primary" : "bg-muted-foreground/40",
                )}
              />
              <button
                type="button"
                onClick={() => onSelect(d.doc_id)}
                aria-current={active ? "true" : undefined}
                className={cn(
                  "mt-1.5 w-[132px] rounded-md border px-2 py-1.5 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  active
                    ? "border-primary bg-primary/5"
                    : "border-border hover:border-muted-foreground/30 hover:bg-muted/40",
                )}
              >
                <span className="block truncate text-[11px] font-semibold text-foreground">
                  {CATEGORY_LABEL[d.doc_category] ?? d.doc_category}
                </span>
                <span className="block truncate text-[11px] text-muted-foreground">
                  {d.terminal ?? "—"}
                </span>
                <span className="mt-0.5 block text-[10px] tabular-nums text-muted-foreground">
                  {fmtDayIST(d.doc_ts)} · {fmtHmIST(d.doc_ts)}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
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

  // The API returns newest-first (doc_ts DESC). The list keeps that order; the
  // timeline rail reads left-to-right oldest → newest. Display-only, no refetch.
  const chronological = useMemo(() => docs.slice().reverse(), [docs]);

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

  const terminals = query.data?.terminals ?? [];
  const hasDocs = docs.length > 0;

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background">
      {/* ---- Header band: identity, search and the three summary figures ---- */}
      <header className="shrink-0 border-b border-border px-4 py-3">
        <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
          <div className="min-w-0">
            <h1 className="text-base font-semibold tracking-tight">Truck Visit Detail</h1>
            <p className="mt-0.5 max-w-2xl text-xs text-muted-foreground">
              Every gate document a tractor produced, parsed from the operator's own paperwork and
              shown beside the original scan.
            </p>
          </div>

          <div className="flex flex-1 flex-wrap items-center justify-start gap-2 lg:justify-end">
            <form
              role="search"
              className="flex items-stretch overflow-hidden rounded-lg border border-border bg-background focus-within:ring-2 focus-within:ring-ring"
              onSubmit={(e) => {
                e.preventDefault();
                setTruck(norm(input));
              }}
            >
              <span className="flex items-center pl-2.5 text-muted-foreground" aria-hidden>
                <Search className="h-3.5 w-3.5" />
              </span>
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                spellCheck={false}
                aria-label="Truck number"
                placeholder={`Search truck number (e.g. ${EXAMPLE_TRUCK})`}
                className="w-56 bg-transparent px-2 py-1.5 font-mono text-sm outline-none placeholder:font-sans placeholder:text-muted-foreground"
              />
              <button
                type="submit"
                className="border-l border-border bg-primary px-3 text-sm font-medium text-primary-foreground transition hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                Search
              </button>
            </form>

            {/* Which tractor the results actually belong to — the input may have
                been edited since the last submit. */}
            {truck && (
              <span className="text-[11px] text-muted-foreground">
                Showing <span className="font-mono text-foreground">{truck}</span>
              </span>
            )}

            {hasDocs && (
              <div className="flex flex-wrap items-stretch gap-2">
                <Stat label="Documents" value={String(query.data?.total ?? docs.length)} />
                <Stat
                  label="Terminals"
                  value={String(query.data?.terminal_count ?? terminals.length)}
                  hint={terminals.length ? terminals.join(", ") : undefined}
                />
                <Stat label="Span" value={span != null ? `${span} days` : "—"} />
              </div>
            )}
          </div>
        </div>
      </header>

      {/* ---- Timeline rail ------------------------------------------------- */}
      {hasDocs && (
        <div className="shrink-0 border-b border-border px-4 py-2">
          <VisitTimeline docs={chronological} selected={selected} onSelect={setSelected} />
        </div>
      )}

      {/* ---- Body ---------------------------------------------------------- */}
      {/* Below `xl` the body scrolls as a whole and the panes take their natural
          height, stacking list → detail → scan. At `xl` the three panes become
          independent scroll areas that together fill the viewport exactly. */}
      <div className="min-h-0 flex-1 overflow-y-auto xl:overflow-hidden">
        {query.isLoading && (
          <p className="p-4 text-sm text-muted-foreground" role="status">
            Loading documents…
          </p>
        )}

        {query.isError && (
          <p
            className="m-4 rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive"
            role="alert"
          >
            Could not load gate documents. {String((query.error as Error)?.message ?? "")}
          </p>
        )}

        {!query.isLoading && !query.isError && !hasDocs && (
          <p className="m-4 rounded-lg border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
            No gate documents on record for{" "}
            <span className="font-mono text-foreground">{truck}</span>.
          </p>
        )}

        {hasDocs && (
          <div className="grid gap-3 p-3 lg:grid-cols-[minmax(240px,280px)_minmax(0,1fr)] xl:h-full xl:min-h-0 xl:grid-cols-[minmax(240px,290px)_minmax(0,1fr)_minmax(300px,30%)]">
            {/* Document list — capped so a long list never drives page height. */}
            <aside className="flex min-h-0 min-w-0 flex-col rounded-lg border border-border bg-card">
              <h2 className="shrink-0 border-b border-border px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                Documents ({docs.length})
              </h2>
              <ol className="min-h-0 max-h-[45vh] flex-1 space-y-1.5 overflow-y-auto p-2 xl:max-h-none">
                {docs.map((d) => {
                  const active = d.doc_id === selected;
                  return (
                    <li key={d.doc_id}>
                      <button
                        type="button"
                        onClick={() => setSelected(d.doc_id)}
                        aria-current={active ? "true" : undefined}
                        className={cn(
                          "w-full rounded-lg border px-2.5 py-2 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                          active
                            ? "border-primary bg-primary/5 ring-1 ring-inset ring-primary/20"
                            : "border-border hover:border-muted-foreground/30 hover:bg-muted/40",
                        )}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <CategoryChip category={d.doc_category} />
                          <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground">
                            {fmtDayIST(d.doc_ts)} · {fmtHmIST(d.doc_ts)}
                          </span>
                        </div>
                        <div className="mt-1 truncate text-[12px] font-medium text-foreground">
                          {d.terminal ?? "—"}
                        </div>
                        <div
                          className="truncate font-mono text-[11px] text-muted-foreground"
                          title={d.container_no ?? undefined}
                        >
                          {d.container_no ?? "no container"}
                        </div>
                        <div
                          className="truncate text-[10px] text-muted-foreground/80"
                          title={`BAT ${d.bat_no ?? "—"} · doc ${d.doc_ref ?? d.visit_id ?? "—"}`}
                        >
                          BAT {d.bat_no ?? "—"} · doc {d.doc_ref ?? d.visit_id ?? "—"}
                        </div>
                      </button>
                    </li>
                  );
                })}
              </ol>
            </aside>

            {/* Parsed detail — the primary focus. */}
            {current && (
              <section className="flex min-h-0 min-w-0 flex-col rounded-lg border border-border bg-card">
                <header className="flex shrink-0 flex-wrap items-center gap-x-2 gap-y-1 border-b border-border px-3 py-2">
                  <CategoryChip category={current.doc_category} />
                  <h2 className="min-w-0 truncate text-sm font-semibold text-foreground">
                    {current.terminal ?? "Unknown terminal"} · {fmtDateTimeIST(current.doc_ts)}
                  </h2>
                  <OriginBadge origin={current.data_origin} />
                  <span
                    className="ml-auto truncate text-[10px] text-muted-foreground"
                    title={current.doc_variant}
                  >
                    source: {current.doc_variant}
                  </span>
                </header>
                <div className="min-h-0 flex-1 overflow-y-auto p-3">
                  <ParsedPane doc={current} />
                </div>
              </section>
            )}

            {/* Original scan. One instance only: grid order puts it beside the
                data at xl and directly under it on narrower screens, so the
                image is never mounted (or fetched) twice. */}
            {current && (
              <div className="flex min-h-0 min-w-0 flex-col lg:col-span-2 xl:col-span-1">
                <ScanPane doc={current} />
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
