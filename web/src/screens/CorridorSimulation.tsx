// UC3-005 — NH-348 Corridor Simulation.
//
// There is no real per-truck GPS for the demo window, so this traffic is
// GENERATED. The screen is built so that fact is impossible to miss or to lose:
// a standing SIMULATED banner, a SIMULATED chip on every truck row, and a
// provenance panel that shows the seed and the SHA-256 of the frozen config so a
// viewer can confirm the run was not reseeded after rehearsal.
//
// The calibration block deliberately puts two columns side by side and labels
// them differently: ANCHOR is the real published gate figure for 20-07-2026,
// CURRENT is what this simulation generated. They are never summed, blended or
// presented as one number.
//
// Every value comes from /api/corridor-sim. Nothing is hard-coded.
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { FlaskConical } from "lucide-react";

import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

const PAGE = 25;

function fmt(n: number | undefined | null): string {
  return n == null ? "—" : n.toLocaleString("en-IN");
}

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}

/** The badge that must never be absent from this screen. */
function SimulatedChip({ className }: { className?: string }) {
  return (
    <span
      title="Generated data. Not a measured observation."
      className={cn(
        "inline-block shrink-0 rounded-full bg-amber-500/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-600 ring-1 ring-inset ring-amber-500/30 dark:text-amber-400",
        className,
      )}
    >
      Simulated
    </span>
  );
}

function Stat({ value, label, hint }: { value: string; label: string; hint?: string }) {
  return (
    <div className="min-w-0 rounded-lg border border-border bg-card px-3 py-2">
      <div className="truncate text-lg font-semibold tabular-nums text-foreground">{value}</div>
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
      {hint && (
        <div className="truncate text-[10px] text-muted-foreground" title={hint}>
          {hint}
        </div>
      )}
    </div>
  );
}

function Row({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 py-1">
      <dt className="w-32 shrink-0 text-[10px] uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd
        title={value}
        className={cn("min-w-0 flex-1 break-all text-[12px] text-foreground", mono && "font-mono")}
      >
        {value}
      </dd>
    </div>
  );
}

export default function CorridorSimulation() {
  const [segment, setSegment] = useState<string | null>(null);
  const [direction, setDirection] = useState<"IN" | "OUT" | null>(null);
  const [offset, setOffset] = useState(0);

  const summary = useQuery({
    queryKey: ["corridor-sim-summary"],
    queryFn: () => api.corridorSimSummary(),
  });

  const trucks = useQuery({
    queryKey: ["corridor-sim-trucks", segment, direction, offset],
    queryFn: () =>
      api.corridorSimTrucks({
        segment: segment ?? undefined,
        direction: direction ?? undefined,
        limit: PAGE,
        offset,
      }),
  });

  const d = summary.data;
  const maxSeg = d?.segments.reduce((m, s) => Math.max(m, s.trucks), 0) ?? 0;

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background">
      {/* ---- Header + standing SIMULATED banner --------------------------- */}
      <header className="shrink-0 border-b border-border px-4 py-3">
        <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-base font-semibold tracking-tight">NH-348 Corridor Simulation</h1>
              <SimulatedChip />
            </div>
            <p className="mt-0.5 max-w-3xl text-xs text-muted-foreground">
              Generated corridor traffic for the demo window. No real per-truck GPS exists for this
              period, so every record on this screen is simulated and stored separately from
              measured operational data.
            </p>
          </div>
          {d && (
            <div className="flex flex-wrap items-stretch gap-2">
              <Stat value={fmt(d.trucks_total)} label="Simulated trucks" />
              <Stat value={fmt(d.segment_count)} label="Road segments" />
              <Stat value={fmt(d.inbound)} label="IN" />
              <Stat value={fmt(d.outbound)} label="OUT" />
              <Stat value={d.run.corridor} label="Corridor" />
            </div>
          )}
        </div>

        <div
          className="mt-2 flex flex-wrap items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-1.5 text-[11px] text-muted-foreground"
          role="note"
        >
          <FlaskConical
            className="h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-400"
            aria-hidden
          />
          <span>
            <span className="font-semibold text-amber-600 dark:text-amber-400">
              SIMULATED DATA —{" "}
            </span>
            not measured. These figures must not be read as real corridor throughput.
          </span>
        </div>
      </header>

      {/* ---- Body --------------------------------------------------------- */}
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {summary.isLoading && (
          <p className="p-4 text-sm text-muted-foreground" role="status">
            Loading simulation…
          </p>
        )}
        {summary.isError && (
          <p
            className="rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive"
            role="alert"
          >
            Could not load the corridor simulation.{" "}
            {String((summary.error as Error)?.message ?? "")}
          </p>
        )}

        {d && (
          <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(320px,34%)]">
            {/* -- segments + trucks -- */}
            <div className="min-w-0 space-y-3">
              <section className="rounded-lg border border-border bg-card">
                <header className="flex items-center gap-2 border-b border-border px-3 py-2">
                  <h2 className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                    Segment distribution
                  </h2>
                  <span className="text-[10px] text-muted-foreground">
                    {d.segment_count} segments · {fmt(d.trucks_total)} trucks
                  </span>
                  <SimulatedChip className="ml-auto" />
                </header>
                <ul className="divide-y divide-border">
                  {d.segments.map((s) => (
                    <li key={s.segment_code}>
                      <button
                        type="button"
                        onClick={() => {
                          setSegment(segment === s.segment_code ? null : s.segment_code);
                          setOffset(0);
                        }}
                        aria-pressed={segment === s.segment_code}
                        className={cn(
                          "flex w-full items-center gap-3 px-3 py-1.5 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
                          segment === s.segment_code ? "bg-primary/5" : "hover:bg-muted/40",
                        )}
                      >
                        <span className="w-16 shrink-0 font-mono text-[12px] font-medium text-foreground">
                          {s.segment_code}
                        </span>
                        <span className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-muted">
                          <span
                            className="block h-full rounded-full bg-amber-500/60"
                            style={{ width: `${maxSeg ? (s.trucks / maxSeg) * 100 : 0}%` }}
                          />
                        </span>
                        <span className="w-14 shrink-0 text-right text-[12px] tabular-nums text-foreground">
                          {fmt(s.trucks)}
                        </span>
                        <span className="w-28 shrink-0 text-right text-[10px] tabular-nums text-muted-foreground">
                          {fmt(s.inbound)} in · {fmt(s.outbound)} out
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              </section>

              <section className="rounded-lg border border-border bg-card">
                <header className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
                  <h2 className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                    Trucks
                  </h2>
                  <div className="flex flex-wrap items-center gap-1.5">
                    {(["IN", "OUT"] as const).map((dir) => (
                      <button
                        key={dir}
                        type="button"
                        onClick={() => {
                          setDirection(direction === dir ? null : dir);
                          setOffset(0);
                        }}
                        aria-pressed={direction === dir}
                        className={cn(
                          "rounded border px-2 py-0.5 text-[10px] font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                          direction === dir
                            ? "border-primary bg-primary/5 text-foreground"
                            : "border-border text-muted-foreground hover:bg-muted/40",
                        )}
                      >
                        {dir}
                      </button>
                    ))}
                    {(segment || direction) && (
                      <button
                        type="button"
                        onClick={() => {
                          setSegment(null);
                          setDirection(null);
                          setOffset(0);
                        }}
                        className="rounded px-1.5 py-0.5 text-[10px] text-muted-foreground underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        clear
                      </button>
                    )}
                  </div>
                  <span className="ml-auto text-[10px] text-muted-foreground">
                    {segment ?? "all segments"} · showing {trucks.data?.count ?? 0} of{" "}
                    {fmt(d.trucks_total)}
                  </span>
                </header>

                {trucks.isError ? (
                  <p className="p-4 text-sm text-destructive" role="alert">
                    Could not load trucks.
                  </p>
                ) : trucks.isLoading ? (
                  <p className="p-4 text-sm text-muted-foreground" role="status">
                    Loading trucks…
                  </p>
                ) : (trucks.data?.items.length ?? 0) === 0 ? (
                  <p className="p-6 text-center text-sm text-muted-foreground">
                    No simulated trucks match this filter.
                  </p>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[520px] text-left text-[12px]">
                      <thead className="bg-muted/40 text-[10px] uppercase tracking-wide text-muted-foreground">
                        <tr>
                          <th className="px-3 py-1.5 font-medium">Truck</th>
                          <th className="px-3 py-1.5 font-medium">Segment</th>
                          <th className="px-3 py-1.5 font-medium">Dir</th>
                          <th className="px-3 py-1.5 font-medium">State</th>
                          <th className="px-3 py-1.5 font-medium">Provenance</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-border">
                        {trucks.data?.items.map((t) => (
                          <tr key={t.truck_uid} className="transition hover:bg-muted/30">
                            <td className="px-3 py-1.5 font-mono text-foreground">{t.truck_no}</td>
                            <td className="px-3 py-1.5 font-mono text-muted-foreground">
                              {t.segment_code}
                            </td>
                            <td className="px-3 py-1.5 text-muted-foreground">{t.direction}</td>
                            <td className="px-3 py-1.5 text-muted-foreground">{t.state}</td>
                            <td className="px-3 py-1.5">
                              <SimulatedChip />
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}

                <footer className="flex items-center justify-between gap-2 border-t border-border px-3 py-2">
                  <button
                    type="button"
                    onClick={() => setOffset((o) => Math.max(0, o - PAGE))}
                    disabled={offset === 0}
                    className="rounded-lg border border-border px-2.5 py-1 text-[11px] font-medium transition hover:bg-muted/40 disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    Previous
                  </button>
                  <span className="text-[10px] tabular-nums text-muted-foreground">
                    {offset + 1}–{offset + (trucks.data?.count ?? 0)}
                  </span>
                  <button
                    type="button"
                    onClick={() => setOffset((o) => o + PAGE)}
                    disabled={(trucks.data?.count ?? 0) < PAGE}
                    className="rounded-lg border border-border px-2.5 py-1 text-[11px] font-medium transition hover:bg-muted/40 disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    Next
                  </button>
                </footer>
              </section>
            </div>

            {/* -- provenance + calibration -- */}
            <div className="min-w-0 space-y-3">
              <section className="rounded-lg border border-border bg-card">
                <header className="flex items-center gap-2 border-b border-border px-3 py-2">
                  <h2 className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                    Provenance &amp; reproducibility
                  </h2>
                  <SimulatedChip className="ml-auto" />
                </header>
                <dl className="p-3">
                  <Row label="Data mode" value={d.provenance} />
                  <Row label="Run" value={d.run.run_id} mono />
                  <Row label="Seed" value={d.reproducibility.seed} mono />
                  <Row label="Version" value={d.reproducibility.seed_version} mono />
                  <Row label="SHA-256" value={d.reproducibility.config_sha256} mono />
                  <Row label="Frozen at" value={fmtDate(d.run.frozen_at)} />
                </dl>
              </section>

              <section className="rounded-lg border border-border bg-card">
                <header className="border-b border-border px-3 py-2">
                  <h2 className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                    Calibration
                  </h2>
                  <p className="mt-0.5 text-[10px] text-muted-foreground">
                    {fmtDate(d.calibration.window_from)} → {fmtDate(d.calibration.window_to)}
                  </p>
                </header>
                <div className="grid grid-cols-2 divide-x divide-border">
                  {/* The two columns are deliberately labelled differently. */}
                  <div className="min-w-0 p-3">
                    <div className="text-[10px] font-semibold uppercase tracking-wide text-emerald-600 dark:text-emerald-400">
                      Anchor · real
                    </div>
                    <div className="mt-0.5 text-[10px] text-muted-foreground">
                      Published gate moves, {fmtDate(d.calibration.anchor_date)}
                    </div>
                    <dl className="mt-1.5 space-y-0.5 text-[12px] tabular-nums">
                      <div className="flex justify-between gap-2">
                        <dt className="text-muted-foreground">IN</dt>
                        <dd className="font-medium">{fmt(d.calibration.anchor_in_teu)}</dd>
                      </div>
                      <div className="flex justify-between gap-2">
                        <dt className="text-muted-foreground">OUT</dt>
                        <dd className="font-medium">{fmt(d.calibration.anchor_out_teu)}</dd>
                      </div>
                      <div className="flex justify-between gap-2 border-t border-border pt-0.5">
                        <dt className="text-muted-foreground">Total TEU</dt>
                        <dd className="font-semibold">{fmt(d.calibration.anchor_total_teu)}</dd>
                      </div>
                    </dl>
                  </div>
                  <div className="min-w-0 p-3">
                    <div className="text-[10px] font-semibold uppercase tracking-wide text-amber-600 dark:text-amber-400">
                      Current · simulated
                    </div>
                    <div className="mt-0.5 text-[10px] text-muted-foreground">
                      Generated population, seed {d.reproducibility.seed_version}
                    </div>
                    <dl className="mt-1.5 space-y-0.5 text-[12px] tabular-nums">
                      <div className="flex justify-between gap-2">
                        <dt className="text-muted-foreground">IN</dt>
                        <dd className="font-medium">{fmt(d.inbound)}</dd>
                      </div>
                      <div className="flex justify-between gap-2">
                        <dt className="text-muted-foreground">OUT</dt>
                        <dd className="font-medium">{fmt(d.outbound)}</dd>
                      </div>
                      <div className="flex justify-between gap-2 border-t border-border pt-0.5">
                        <dt className="text-muted-foreground">Total trucks</dt>
                        <dd className="font-semibold">{fmt(d.trucks_total)}</dd>
                      </div>
                    </dl>
                  </div>
                </div>
                {d.calibration.note && (
                  <p className="border-t border-border p-3 text-[11px] leading-relaxed text-muted-foreground">
                    {d.calibration.note}
                  </p>
                )}
              </section>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
