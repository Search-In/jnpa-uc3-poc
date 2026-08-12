// UC3-004 — Vehicle & Transporter Registry.
//
// Gap G6: neither TransporterDetails.xlsx nor PDP Details.xlsx carries a vehicle
// number, so the vehicle->transporter relationship cannot be loaded from the
// customer's master data. Only the REAL gate-document corpus evidences it, and
// only on the slips that actually print a transporter name.
//
// The whole point of this screen is that you can always tell which half you are
// looking at. A DOCUMENT_EVIDENCED row names the gate document it was read from;
// a SYNTHETIC row carries assumption A-G6 and the seed that generated it, and is
// never styled or worded as though it were evidence. Nothing here infers a
// transporter: an unknown plate renders an empty state, not a guess.
//
// Every figure comes from /api/vehicle-registry. Nothing is hard-coded.
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { FileText, Info, Search } from "lucide-react";

import { api, type VehicleMapping, type VehicleProvenance } from "@/lib/api";
import { cn } from "@/lib/utils";

type Filter = "ALL" | VehicleProvenance;

const FILTERS: { key: Filter; label: string }[] = [
  { key: "ALL", label: "All" },
  { key: "DOCUMENT_EVIDENCED", label: "Document evidenced" },
  { key: "SYNTHETIC", label: "Synthetic" },
];

function norm(plate: string): string {
  return plate.toUpperCase().replace(/[^A-Z0-9]/g, "");
}

/** The one visual rule this screen exists to enforce. */
function ProvenanceBadge({ provenance }: { provenance: VehicleProvenance }) {
  const evidenced = provenance === "DOCUMENT_EVIDENCED";
  return (
    <span
      title={
        evidenced
          ? "Read from a REAL gate document — see the source reference"
          : "Generated to fill gap A-G6. This is an assumption, not evidence of ownership."
      }
      className={cn(
        "inline-block shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ring-1 ring-inset",
        evidenced
          ? "bg-emerald-500/10 text-emerald-600 ring-emerald-500/30 dark:text-emerald-400"
          : "bg-amber-500/10 text-amber-600 ring-amber-500/30 dark:text-amber-400",
      )}
    >
      {evidenced ? "Document evidenced" : "Synthetic"}
    </span>
  );
}

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
          mono ? "break-all font-mono" : "break-words",
          empty ? "text-muted-foreground/60" : "font-medium text-foreground",
        )}
      >
        {text}
      </dd>
    </div>
  );
}

/** Source (evidenced) or assumption + seed (synthetic) — never both. */
function Attribution({ m }: { m: VehicleMapping }) {
  if (m.provenance === "DOCUMENT_EVIDENCED") {
    return (
      <span className="inline-flex min-w-0 items-center gap-1 text-[11px] text-muted-foreground">
        <FileText className="h-3 w-3 shrink-0" aria-hidden />
        <span className="truncate font-mono" title={m.source_ref ?? undefined}>
          {m.source_ref ?? "—"}
        </span>
      </span>
    );
  }
  return (
    <span className="min-w-0 text-[11px] text-muted-foreground">
      <span className="font-semibold text-amber-600 dark:text-amber-400">
        {m.assumption_ref ?? "A-G6"}
      </span>
      {m.seed && (
        <span className="ml-1 truncate font-mono" title={m.seed}>
          · {m.seed}
        </span>
      )}
    </span>
  );
}

/** Full detail for one searched plate — the hero view. */
function MappingDetail({ m }: { m: VehicleMapping }) {
  return (
    <section className="rounded-lg border border-border bg-card">
      <header className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        <h2 className="min-w-0 truncate font-mono text-sm font-semibold text-foreground">
          {m.vehicle_no}
        </h2>
        <ProvenanceBadge provenance={m.provenance} />
        <span className="ml-auto min-w-0">
          <Attribution m={m} />
        </span>
      </header>
      <div className="p-3">
        <dl className="grid grid-cols-1 gap-x-4 sm:grid-cols-2 lg:grid-cols-3">
          <Field label="Vehicle" value={m.vehicle_no} mono />
          <Field label="Transporter" value={m.transporter} />
          <Field label="Company ID" value={m.company_id} mono />
          <Field label="Contact" value={m.transporter_contact} />
          <Field label="Driver" value={m.driver_id} mono />
          <Field label="Provenance" value={m.provenance.replace("_", " ")} />
          {m.provenance === "DOCUMENT_EVIDENCED" ? (
            <Field label="Source document" value={m.source_ref} mono />
          ) : (
            <>
              <Field label="Assumption" value={m.assumption_ref} />
              <Field label="Seed" value={m.seed} mono />
            </>
          )}
        </dl>
        {m.is_synthetic && m.assumption_text && (
          <p className="mt-2 rounded-lg border border-amber-500/30 bg-amber-500/5 p-2.5 text-[11px] leading-relaxed text-muted-foreground">
            <span className="font-semibold text-amber-600 dark:text-amber-400">
              Why synthetic?{" "}
            </span>
            {m.assumption_text}
          </p>
        )}
      </div>
    </section>
  );
}

export default function VehicleRegistry() {
  const [input, setInput] = useState("");
  const [plate, setPlate] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("ALL");
  const [showAssumption, setShowAssumption] = useState(false);

  const summary = useQuery({
    queryKey: ["vehicle-registry-summary"],
    queryFn: () => api.vehicleRegistrySummary(),
  });

  const list = useQuery({
    queryKey: ["vehicle-mappings", filter],
    queryFn: () =>
      api.vehicleMappings({
        provenance: filter === "ALL" ? undefined : filter,
        limit: 200,
      }),
  });

  // A 404 from the API is a legitimate "no mapping", not an error to shout about.
  const lookup = useQuery({
    queryKey: ["vehicle-mapping", plate],
    queryFn: () => api.vehicleMapping(plate as string),
    enabled: !!plate,
    retry: false,
  });

  const rows = list.data?.items ?? [];

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background">
      {/* ---- Header band ------------------------------------------------- */}
      <header className="shrink-0 border-b border-border px-4 py-3">
        <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
          <div className="min-w-0">
            <h1 className="text-base font-semibold tracking-tight">
              Vehicle &amp; Transporter Registry
            </h1>
            <p className="mt-0.5 max-w-3xl text-xs text-muted-foreground">
              Mappings combine gate-document evidence with explicitly labelled assumptions. A
              mapping is shown as evidenced only when a real gate document names the transporter;
              everything else is marked synthetic under {summary.data?.assumption_ref ?? "A-G6"}.
            </p>
          </div>

          <div className="flex flex-1 flex-wrap items-center gap-2 lg:justify-end">
            <form
              role="search"
              className="flex items-stretch overflow-hidden rounded-lg border border-border bg-background focus-within:ring-2 focus-within:ring-ring"
              onSubmit={(e) => {
                e.preventDefault();
                setPlate(norm(input) || null);
              }}
            >
              <span className="flex items-center pl-2.5 text-muted-foreground" aria-hidden>
                <Search className="h-3.5 w-3.5" />
              </span>
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                spellCheck={false}
                aria-label="Vehicle number"
                placeholder="Search vehicle number (e.g. MH43BX1488)"
                className="w-60 bg-transparent px-2 py-1.5 font-mono text-sm outline-none placeholder:font-sans placeholder:text-muted-foreground"
              />
              <button
                type="submit"
                className="border-l border-border bg-primary px-3 text-sm font-medium text-primary-foreground transition hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                Search
              </button>
            </form>

            {summary.data && (
              <div className="flex flex-wrap items-stretch gap-2">
                <Stat label="Mappings" value={String(summary.data.total)} />
                <Stat label="Evidenced" value={String(summary.data.document_evidenced)} />
                <Stat label="Synthetic" value={String(summary.data.synthetic)} />
                <Stat label="Assumption" value={summary.data.assumption_ref} />
              </div>
            )}
          </div>
        </div>

        {/* Why synthetic? — A-G6, discoverable without leaving the screen. */}
        {summary.data && (
          <div className="mt-2">
            <button
              type="button"
              onClick={() => setShowAssumption((v) => !v)}
              aria-expanded={showAssumption}
              className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground transition hover:bg-muted/60 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <Info className="h-3 w-3" aria-hidden />
              Why are some mappings synthetic? ({summary.data.assumption_ref})
            </button>
            {showAssumption && (
              <p className="mt-1.5 max-w-4xl rounded-lg border border-border bg-card p-2.5 text-[11px] leading-relaxed text-muted-foreground">
                {summary.data.assumption_text}{" "}
                <span className="text-foreground">
                  Seed <span className="font-mono">{summary.data.seed}</span>.
                </span>
              </p>
            )}
          </div>
        )}
      </header>

      {/* ---- Body --------------------------------------------------------- */}
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {/* Searched plate */}
        {plate && (
          <div className="mb-3">
            {lookup.isLoading && (
              <p className="text-sm text-muted-foreground" role="status">
                Looking up <span className="font-mono">{plate}</span>…
              </p>
            )}
            {lookup.isError && (
              <div className="rounded-lg border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
                No vehicle mapping found for <span className="font-mono">{plate}</span>.
                <div className="mt-1 text-[11px]">
                  The registry does not guess ownership — a plate with no evidence and no seeded
                  mapping simply has none.
                </div>
              </div>
            )}
            {lookup.data && <MappingDetail m={lookup.data} />}
          </div>
        )}

        {/* Filters */}
        <div className="mb-2 flex flex-wrap items-center gap-1.5">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              type="button"
              onClick={() => setFilter(f.key)}
              aria-pressed={filter === f.key}
              className={cn(
                "rounded-lg border px-2.5 py-1 text-[11px] font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                filter === f.key
                  ? "border-primary bg-primary/5 text-foreground"
                  : "border-border text-muted-foreground hover:bg-muted/40",
              )}
            >
              {f.label}
            </button>
          ))}
          {list.data && (
            <span className="ml-1 text-[11px] text-muted-foreground">
              {list.data.total} mapping{list.data.total === 1 ? "" : "s"}
            </span>
          )}
        </div>

        {list.isLoading && (
          <p className="p-4 text-sm text-muted-foreground" role="status">
            Loading registry…
          </p>
        )}
        {list.isError && (
          <p
            className="rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive"
            role="alert"
          >
            Could not load the vehicle registry. {String((list.error as Error)?.message ?? "")}
          </p>
        )}
        {!list.isLoading && !list.isError && rows.length === 0 && (
          <p className="rounded-lg border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
            No mappings for this filter.
          </p>
        )}

        {rows.length > 0 && (
          <>
            {/* Desktop: table. */}
            <div className="hidden overflow-hidden rounded-lg border border-border md:block">
              <table className="w-full table-fixed text-left text-[12px]">
                <thead className="bg-muted/40 text-[10px] uppercase tracking-wide text-muted-foreground">
                  <tr>
                    <th className="w-[13%] px-3 py-2 font-medium">Vehicle</th>
                    <th className="w-[26%] px-3 py-2 font-medium">Transporter</th>
                    <th className="w-[9%] px-3 py-2 font-medium">Company</th>
                    <th className="w-[10%] px-3 py-2 font-medium">Driver</th>
                    <th className="w-[16%] px-3 py-2 font-medium">Provenance</th>
                    <th className="w-[26%] px-3 py-2 font-medium">Source / assumption</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {rows.map((m) => (
                    <tr key={m.id} className="align-top transition hover:bg-muted/30">
                      <td className="px-3 py-2 font-mono font-medium text-foreground">
                        {m.vehicle_no}
                      </td>
                      <td className="truncate px-3 py-2 text-foreground" title={m.transporter}>
                        {m.transporter}
                      </td>
                      <td className="px-3 py-2 font-mono tabular-nums text-muted-foreground">
                        {m.company_id}
                      </td>
                      <td className="px-3 py-2 font-mono text-muted-foreground">
                        {m.driver_id ?? "—"}
                      </td>
                      <td className="px-3 py-2">
                        <ProvenanceBadge provenance={m.provenance} />
                      </td>
                      <td className="min-w-0 px-3 py-2">
                        <Attribution m={m} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Mobile: stacked cards. */}
            <ul className="space-y-2 md:hidden">
              {rows.map((m) => (
                <li key={m.id} className="rounded-lg border border-border bg-card p-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate font-mono text-[13px] font-semibold text-foreground">
                      {m.vehicle_no}
                    </span>
                    <ProvenanceBadge provenance={m.provenance} />
                  </div>
                  <div className="mt-1 truncate text-[12px] text-foreground" title={m.transporter}>
                    {m.transporter}
                  </div>
                  <div className="mt-0.5 text-[11px] text-muted-foreground">
                    Company <span className="font-mono">{m.company_id}</span>
                    {m.driver_id && (
                      <>
                        {" · "}Driver <span className="font-mono">{m.driver_id}</span>
                      </>
                    )}
                  </div>
                  <div className="mt-1">
                    <Attribution m={m} />
                  </div>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </div>
  );
}
