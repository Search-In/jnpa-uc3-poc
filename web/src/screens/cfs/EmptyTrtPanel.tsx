// UC3-003 — Empty Container TRT (KPI 3, "TRT for empty containers from ECD").
//
// Everything on this panel is read from GET /api/cfs-ecy/empty-trt, which scores
// the REAL CFS/ECY CODECO gate log imported into core.container_event. Nothing is
// computed in the browser and nothing is hard-coded: the headline TRT, the valid
// container count, the 529/432 event inventory and every anomaly figure come
// back from the API, so what the evaluator sees is what the database holds.
//
// The panel deliberately shows the *excluded* records next to the KPI. The ECY
// feed is unpaired by design (529 gate-OUT vs 432 gate-IN, in two date-disjoint
// blocks) and that anomaly was recorded in the Data Quality ledger rather than
// repaired — the "Source & anomalies" and "Data Quality" sections are the proof.

import { useState } from "react";
import { useQuery, keepPreviousData } from "@tanstack/react-query";
import {
  Timer,
  Target,
  Boxes,
  Container as ContainerIcon,
  AlertTriangle,
  ShieldCheck,
  FileSpreadsheet,
  Search,
  ChevronLeft,
  ChevronRight,
  ArrowRight,
  Inbox,
  ClipboardList,
} from "lucide-react";

import {
  StatGrid,
  StatCard,
  SegmentedTabs,
  StatusChip,
  type Tone,
} from "@/components/ui/dtccc";
import { Card } from "@/components/ui/card";
import { LoadingState, ErrorState } from "@/components/ui/misc";
import { api } from "@/lib/api";
import type { EmptyTrtChain, EmptyTrtResponse } from "@/lib/types";

const PAGE_SIZE = 15;

type ChainTab = "COMPLETE" | "PARTIAL" | "ORPHAN" | "all";

const inputCls =
  "h-9 rounded-md border border-border bg-background px-2 text-[13px] font-medium text-foreground outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary/20";

/** Minutes as the tender asks — minutes below 2 h, hours (or days) above. */
function fmtMin(v?: number | null): string {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  const m = Number(v);
  if (Math.abs(m) < 120) return `${m.toFixed(m % 1 ? 1 : 0)} min`;
  const h = m / 60;
  if (Math.abs(h) < 48) return `${h.toFixed(1)} h`;
  return `${(h / 24).toFixed(1)} d`;
}

/** Signed variance, always spelled out in minutes so it reads against the target. */
function fmtDelta(v?: number | null): string {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  return `${n >= 0 ? "+" : ""}${n.toFixed(1)} min`;
}

function fmtTs(ts?: string | null): string {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString("en-IN", {
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  } catch {
    return ts;
  }
}

function fmtDate(ts?: string | null): string {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleDateString("en-IN", {
      day: "2-digit",
      month: "short",
      year: "numeric",
    });
  } catch {
    return ts;
  }
}

const severityTone = (s: string): Tone =>
  s === "error" ? "critical" : s === "warn" ? "warn" : "neutral";

const statusTone = (s?: string | null): Tone =>
  s === "COMPLETE" ? "ok" : s === "PARTIAL" ? "warn" : "neutral";

function friendlyError(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err ?? "");
  if (/failed to fetch|networkerror|load failed/i.test(msg))
    return "Network error — check your connection and try again.";
  if (/\b(502|503|504)\b|service unavailable|gateway/i.test(msg))
    return "Server unavailable — the backend is not responding. Please retry shortly.";
  return "Unable to load the empty-container TRT. Please retry.";
}

export default function EmptyTrtPanel() {
  const [chainTab, setChainTab] = useState<ChainTab>("COMPLETE");
  const [searchInput, setSearchInput] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [offset, setOffset] = useState(0);
  const [anomalyCode, setAnomalyCode] = useState<string | null>(null);

  const kpiQ = useQuery({
    queryKey: ["empty-trt"],
    queryFn: () => api.emptyTrt(),
  });

  const chainsQ = useQuery({
    queryKey: ["empty-trt-chains", chainTab, submitted, offset],
    queryFn: () =>
      api.emptyTrtChains({
        chain_status: chainTab === "all" ? undefined : chainTab,
        container: submitted || undefined,
        sort: "ecy_out_ts",
        order: "asc",
        limit: PAGE_SIZE,
        offset,
      }),
    placeholderData: keepPreviousData,
  });

  const lookupQ = useQuery({
    queryKey: ["empty-trt-container", submitted],
    queryFn: () => api.emptyTrtContainer(submitted),
    enabled: submitted.length >= 4,
    retry: false,
  });

  const anomalyQ = useQuery({
    queryKey: ["empty-trt-anomaly", anomalyCode],
    queryFn: () => api.emptyTrtAnomaly(anomalyCode as string, { limit: 100 }),
    enabled: !!anomalyCode,
  });

  if (kpiQ.isLoading) {
    return (
      <div className="p-6">
        <LoadingState />
      </div>
    );
  }
  if (kpiQ.isError || !kpiQ.data) {
    return <ErrorState onRetry={() => kpiQ.refetch()} detail={friendlyError(kpiQ.error)} />;
  }

  const d: EmptyTrtResponse = kpiQ.data;
  const k = d.kpi;
  const dist = d.distribution;
  const src = d.source;
  const live = k.source !== "baseline";
  const onTarget = k.onTarget;

  const chains: EmptyTrtChain[] = chainsQ.data?.items ?? [];
  const chainTotal = chainsQ.data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(chainTotal / PAGE_SIZE));
  const page = Math.floor(offset / PAGE_SIZE);

  function runSearch(value: string) {
    setSubmitted(value.trim().toUpperCase());
    setOffset(0);
  }

  return (
    <div className="flex flex-col gap-4">
      {/* ---------------------------------------------------------- KPI hero */}
      <Card className="p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              KPI 3 · Appendix C
            </div>
            <h3 className="text-base font-bold text-foreground">
              TRT for empty containers from ECD
            </h3>
            <p className="mt-1 max-w-2xl text-[12px] text-muted-foreground">
              {d.definition.measure}, averaged over every container with a complete{" "}
              <span className="font-medium text-foreground">
                ECY gate-out → CFS gate-in → CFS gate-out
              </span>{" "}
              chain in the CODECO corpus. {d.definition.eligible}.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <StatusChip
              label={live ? "Live · real CODECO corpus" : "Baseline placeholder — no data"}
              tone={live ? "ok" : "neutral"}
            />
            <StatusChip
              label={onTarget ? "Meeting target" : "Above target"}
              tone={onTarget ? "ok" : "critical"}
            />
          </div>
        </div>

        <div className="mt-4 flex flex-wrap items-end gap-6">
          <div>
            <div className="text-4xl font-bold tabular-nums leading-none text-foreground">
              {dist.avg_trt_min === null ? "—" : dist.avg_trt_min.toFixed(2)}
              <span className="ml-1 text-lg font-semibold text-muted-foreground">min</span>
            </div>
            <div className="mt-1 text-[11px] text-muted-foreground">
              current mean TRT · {fmtMin(dist.avg_trt_min)}
            </div>
          </div>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-[12px] sm:grid-cols-4">
            <Metric label="Target" value={`${d.definition.target} min`} />
            <Metric label="Baseline" value={`${d.definition.baseline} min`} />
            <Metric label="vs target" value={fmtDelta(dist.vs_target_min)} strong={!onTarget} />
            <Metric label="vs baseline" value={fmtDelta(dist.vs_baseline_min)} />
            <Metric label="Median" value={fmtMin(dist.median_trt_min)} />
            <Metric label="Fastest" value={fmtMin(dist.min_trt_min)} />
            <Metric label="Slowest" value={fmtMin(dist.max_trt_min)} />
            <Metric label="Valid containers" value={String(dist.valid_containers)} />
          </dl>
        </div>

        <div className="mt-3 border-t border-border pt-2 text-[11px] text-muted-foreground">
          Measured over {fmtDate(dist.window_from)} – {fmtDate(dist.window_to)} · mean CFS dwell{" "}
          {fmtMin(dist.avg_dwell_min)} · mean full cycle {fmtMin(dist.avg_cycle_min)}
        </div>
      </Card>

      {/* --------------------------------------------------------- headline stats */}
      <StatGrid>
        <StatCard
          icon={Timer}
          label="Current TRT"
          value={fmtMin(dist.avg_trt_min)}
          tone={onTarget ? "ok" : "critical"}
          sub={`${dist.valid_containers} containers`}
        />
        <StatCard
          icon={Target}
          label="Target"
          value={`${d.definition.target} min`}
          tone="info"
          sub={`baseline ${d.definition.baseline} min`}
        />
        <StatCard
          icon={ContainerIcon}
          label="Valid lifecycle chains"
          value={d.chains.complete}
          tone="ok"
          sub="ECY-Out → CFS-In → CFS-Out"
        />
        <StatCard
          icon={Boxes}
          label="Incomplete chains"
          value={d.chains.partial}
          tone="warn"
          sub="excluded from the KPI"
        />
        <StatCard
          icon={AlertTriangle}
          label="ECY pairing gap"
          value={src.ecy_pairing_gap}
          tone="critical"
          sub={`${src.ecy_out_events} OUT vs ${src.ecy_in_events} IN`}
        />
        <StatCard
          icon={FileSpreadsheet}
          label="Gate events imported"
          value={src.total_events.toLocaleString()}
          tone="neutral"
          sub={`${src.files.length} source workbooks`}
        />
      </StatGrid>

      {/* -------------------------------------------------- source & anomalies */}
      <Card className="p-4">
        <div className="mb-3 flex items-center gap-2">
          <ShieldCheck className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-semibold text-foreground">
            Source data &amp; detected anomalies
          </h3>
          <StatusChip label="Detected · not patched" tone="warn" />
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          {/* Provenance */}
          <div>
            <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              Imported source files
            </div>
            <ul className="flex flex-col gap-2">
              {src.files.map((f) => (
                <li
                  key={f.file_id}
                  className="rounded-md border border-border px-3 py-2 text-[12px]"
                >
                  <div className="font-mono font-medium text-foreground">{f.path}</div>
                  <div className="mt-0.5 text-muted-foreground">
                    {f.row_count?.toLocaleString() ?? "—"} source rows ·{" "}
                    {f.imported_events.toLocaleString()} events stored · loaded{" "}
                    {fmtTs(f.loaded_at)}
                  </div>
                </li>
              ))}
            </ul>

            <div className="mt-3 grid grid-cols-2 gap-2 text-[12px]">
              <LegCount label="ECY gate-OUT" value={src.ecy_out_events} tone="warn" />
              <LegCount label="ECY gate-IN" value={src.ecy_in_events} tone="warn" />
              <LegCount label="CFS gate-IN" value={src.cfs_in_events} tone="info" />
              <LegCount label="CFS gate-OUT" value={src.cfs_out_events} tone="info" />
            </div>
            <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
              The ECY log carries{" "}
              <span className="font-semibold text-foreground">{src.ecy_out_events} gate-OUT</span>{" "}
              against{" "}
              <span className="font-semibold text-foreground">{src.ecy_in_events} gate-IN</span>{" "}
              events — a gap of{" "}
              <span className="font-semibold text-foreground">{src.ecy_pairing_gap}</span>. Every
              source row was imported verbatim; none was deleted, re-dated or matched to an
              invented partner. The CFS log{" "}
              {src.cfs_paired ? "is perfectly paired" : "is unpaired"}.
            </p>
          </div>

          {/* Anomaly codes */}
          <div>
            <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              Anomalies by type (click to list the containers)
            </div>
            <ul className="flex flex-col gap-1.5">
              {d.anomalies.map((a) => (
                <li key={a.code}>
                  <button
                    type="button"
                    onClick={() => setAnomalyCode(anomalyCode === a.code ? null : a.code)}
                    className={`flex w-full items-center gap-2 rounded-md border px-3 py-2 text-left text-[12px] transition-colors hover:bg-muted/50 ${
                      anomalyCode === a.code ? "border-primary bg-muted/40" : "border-border"
                    }`}
                  >
                    <span className="font-mono font-semibold text-foreground">{a.code}</span>
                    <span className="truncate text-muted-foreground">{a.label}</span>
                    <span className="ml-auto shrink-0 font-semibold tabular-nums text-foreground">
                      {a.containers}
                    </span>
                  </button>
                </li>
              ))}
              {d.anomalies.length === 0 && (
                <li className="text-[12px] text-muted-foreground">No anomalies detected.</li>
              )}
            </ul>

            {anomalyCode && (
              <div className="mt-3 rounded-md border border-border p-2">
                <div className="mb-1 text-[11px] font-semibold text-foreground">
                  {anomalyQ.data?.total ?? 0} container(s) · {anomalyCode}
                </div>
                {anomalyQ.isLoading ? (
                  <div className="py-2 text-[12px] text-muted-foreground">Loading…</div>
                ) : (
                  <div className="max-h-40 overflow-y-auto">
                    <div className="flex flex-wrap gap-1">
                      {(anomalyQ.data?.items ?? []).map((c: { container_no: string }) => (
                        <button
                          key={c.container_no}
                          type="button"
                          onClick={() => {
                            setSearchInput(c.container_no);
                            runSearch(c.container_no);
                          }}
                          className="rounded border border-border px-1.5 py-0.5 font-mono text-[11px] text-foreground hover:bg-muted"
                        >
                          {c.container_no}
                        </button>
                      ))}
                    </div>
                    {(anomalyQ.data?.total ?? 0) > (anomalyQ.data?.items?.length ?? 0) && (
                      <div className="mt-1 text-[11px] text-muted-foreground">
                        showing first {anomalyQ.data?.items?.length} of {anomalyQ.data?.total}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </Card>

      {/* ------------------------------------------------------- DQ ledger */}
      <Card className="p-4">
        <div className="mb-3 flex items-center gap-2">
          <ClipboardList className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-semibold text-foreground">
            Data Quality ledger — core.dq_issue
          </h3>
          <StatusChip label={`${d.data_quality.length} findings`} tone="info" />
        </div>
        {d.data_quality.length === 0 ? (
          <p className="text-[12px] text-muted-foreground">
            No findings recorded for this feed yet — run the UC3-003 importer.
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {d.data_quality.map((i) => (
              <li key={i.issue_id} className="rounded-md border border-border p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusChip label={i.severity} tone={severityTone(i.severity)} />
                  <span className="font-mono text-[12px] font-semibold text-foreground">
                    {i.issue_type}
                  </span>
                  {i.record_ref && (
                    <span className="font-mono text-[11px] text-muted-foreground">
                      {i.record_ref}
                    </span>
                  )}
                  <span className="ml-auto text-[11px] text-muted-foreground">
                    {fmtTs(i.detected_at)}
                  </span>
                </div>
                <p className="mt-1.5 text-[12px] leading-relaxed text-muted-foreground">
                  {i.description}
                </p>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {/* --------------------------------------------------- container lookup */}
      <Card className="p-4">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-semibold text-foreground">Container lifecycle lookup</h3>
          <form
            className="ml-auto flex items-center gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              runSearch(searchInput);
            }}
          >
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <input
                type="search"
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                placeholder="Container no. e.g. ONEU2122848"
                aria-label="Container number"
                className={`${inputCls} w-64 pl-8 font-mono`}
              />
            </div>
            <button
              type="submit"
              className="h-9 rounded-md bg-primary px-3 text-[13px] font-medium text-primary-foreground hover:opacity-90"
            >
              Search
            </button>
          </form>
        </div>

        {!submitted ? (
          <p className="text-[12px] text-muted-foreground">
            Search a container to see its ECY gate-out → CFS gate-in → CFS gate-out chain and the
            raw CODECO events behind it.
          </p>
        ) : lookupQ.isLoading ? (
          <LoadingState />
        ) : lookupQ.isError ? (
          <p className="text-[12px] text-muted-foreground">
            No CODECO gate events found for{" "}
            <span className="font-mono font-semibold text-foreground">{submitted}</span>.
          </p>
        ) : (
          <ContainerChain data={lookupQ.data} />
        )}
      </Card>

      {/* ---------------------------------------------------------- chains table */}
      <Card className="p-0">
        <div className="flex flex-wrap items-center gap-2 border-b border-border p-3">
          <SegmentedTabs<ChainTab>
            tabs={[
              { key: "COMPLETE", label: "Complete", count: d.chains.complete },
              { key: "PARTIAL", label: "Partial", count: d.chains.partial },
              { key: "ORPHAN", label: "Orphan", count: d.chains.orphan },
              { key: "all", label: "All", count: d.chains.total },
            ]}
            value={chainTab}
            onChange={(v) => {
              setChainTab(v);
              setOffset(0);
            }}
          />
          {submitted && (
            <span className="text-[11px] text-muted-foreground">
              filtered by “{submitted}”
              <button
                type="button"
                className="ml-1 underline"
                onClick={() => {
                  setSearchInput("");
                  runSearch("");
                }}
              >
                clear
              </button>
            </span>
          )}
        </div>

        {chainsQ.isError ? (
          <ErrorState onRetry={() => chainsQ.refetch()} detail={friendlyError(chainsQ.error)} />
        ) : chainsQ.isLoading ? (
          <div className="p-6">
            <LoadingState />
          </div>
        ) : chains.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-2 py-12 text-center">
            <span className="flex h-12 w-12 items-center justify-center rounded-full bg-muted text-muted-foreground">
              <Inbox size={22} />
            </span>
            <div className="text-sm font-medium">No chains match</div>
          </div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[880px] text-left text-[13px]">
                <thead className="bg-muted/60 text-[11px] uppercase tracking-wide text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2 font-semibold">Container No.</th>
                    <th className="px-3 py-2 font-semibold">ECY gate-out</th>
                    <th className="px-3 py-2 font-semibold">CFS gate-in</th>
                    <th className="px-3 py-2 font-semibold">CFS gate-out</th>
                    <th className="px-3 py-2 text-right font-semibold">TRT</th>
                    <th className="px-3 py-2 font-semibold">Chain</th>
                    <th className="px-3 py-2 font-semibold">Anomalies</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {chains.map((c) => (
                    <tr
                      key={c.container_no}
                      className="cursor-pointer hover:bg-muted/40"
                      onClick={() => {
                        setSearchInput(c.container_no);
                        runSearch(c.container_no);
                      }}
                    >
                      <td className="px-3 py-2 font-mono font-semibold text-foreground">
                        {c.container_no}
                      </td>
                      <td className="px-3 py-2 tabular-nums">{fmtTs(c.ecy_out_ts)}</td>
                      <td className="px-3 py-2 tabular-nums">{fmtTs(c.cfs_in_ts)}</td>
                      <td className="px-3 py-2 tabular-nums">{fmtTs(c.cfs_out_ts)}</td>
                      <td className="px-3 py-2 text-right font-semibold tabular-nums">
                        {c.trt_min === null ? "—" : `${Number(c.trt_min).toFixed(0)} min`}
                      </td>
                      <td className="px-3 py-2">
                        <StatusChip label={c.chain_status} tone={statusTone(c.chain_status)} />
                      </td>
                      <td className="px-3 py-2">
                        <div className="flex flex-wrap gap-1">
                          {(c.anomaly_codes ?? []).map((code) => (
                            <span
                              key={code}
                              className="rounded border border-border px-1.5 py-0.5 font-mono text-[10.5px] text-muted-foreground"
                            >
                              {code}
                            </span>
                          ))}
                          {(c.anomaly_codes ?? []).length === 0 && (
                            <span className="text-[11px] text-muted-foreground">—</span>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="flex flex-wrap items-center gap-2 border-t border-border px-3 py-2 text-[11.5px] text-muted-foreground">
              <span>
                Showing{" "}
                <span className="font-semibold text-foreground">
                  {chainTotal ? offset + 1 : 0}–{offset + chains.length}
                </span>{" "}
                of <span className="font-semibold text-foreground">{chainTotal}</span> chains
              </span>
              <div className="ml-auto flex items-center gap-1">
                <button
                  type="button"
                  disabled={page <= 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                  className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-border hover:bg-muted disabled:opacity-40"
                  aria-label="Previous page"
                >
                  <ChevronLeft size={14} />
                </button>
                <span className="px-1 tabular-nums">
                  {page + 1} / {pageCount}
                </span>
                <button
                  type="button"
                  disabled={page >= pageCount - 1}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                  className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-border hover:bg-muted disabled:opacity-40"
                  aria-label="Next page"
                >
                  <ChevronRight size={14} />
                </button>
              </div>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}

// --- pieces -----------------------------------------------------------------
function Metric({
  label,
  value,
  strong,
}: {
  label: string;
  value: string;
  strong?: boolean;
}) {
  return (
    <div>
      <dt className="text-[10.5px] uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd
        className={`tabular-nums ${strong ? "font-bold text-foreground" : "font-semibold text-foreground"}`}
      >
        {value}
      </dd>
    </div>
  );
}

function LegCount({ label, value, tone }: { label: string; value: number; tone: Tone }) {
  return (
    <div className="flex items-center justify-between rounded-md border border-border px-2 py-1.5">
      <span className="text-muted-foreground">{label}</span>
      <StatusChip label={value.toLocaleString()} tone={tone} />
    </div>
  );
}

/** One container's chain: the three legs, the durations, and the raw events. */
function ContainerChain({ data }: { data: any }) {
  if (!data) return null;
  const legs: { seq: number; leg: string; label: string; ts: string | null; present: boolean }[] =
    data.legs ?? [];
  const events: {
    event_id: number;
    event_type: string;
    location_type: string;
    event_ts: string;
    direction: string;
    details?: Record<string, unknown> | null;
  }[] = data.events ?? [];

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-base font-bold text-foreground">{data.container_no}</span>
        <StatusChip label={data.chain_status} tone={statusTone(data.chain_status)} />
        <StatusChip
          label={data.counts_toward_kpi ? "Counted in the TRT KPI" : "Excluded from the TRT KPI"}
          tone={data.counts_toward_kpi ? "ok" : "warn"}
        />
        {(data.chain?.anomaly_labels ?? []).map((l: string) => (
          <StatusChip key={l} label={l} tone="warn" />
        ))}
      </div>

      {/* Legs */}
      <div className="flex flex-wrap items-center gap-2">
        {legs.map((l, i) => (
          <div key={l.leg} className="flex items-center gap-2">
            <div
              className={`rounded-md border px-3 py-2 text-[12px] ${
                l.present ? "border-border" : "border-dashed border-border opacity-60"
              }`}
            >
              <div className="text-[10.5px] uppercase tracking-wide text-muted-foreground">
                {l.leg.replace(/_/g, " ")}
              </div>
              <div className="font-medium tabular-nums text-foreground">
                {l.present ? fmtTs(l.ts) : "not in the corpus"}
              </div>
            </div>
            {i < legs.length - 1 && <ArrowRight className="h-4 w-4 text-muted-foreground" />}
          </div>
        ))}
      </div>

      <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-[12px] sm:grid-cols-3">
        <Metric label="TRT (ECY-out → CFS-in)" value={fmtMin(data.trt_min)} strong />
        <Metric label="CFS dwell" value={fmtMin(data.dwell_min)} />
        <Metric label="Full cycle" value={fmtMin(data.cycle_min)} />
      </dl>

      {/* Raw events */}
      <div>
        <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          CODECO gate events ({events.length})
        </div>
        <ol className="flex flex-col gap-1.5">
          {events.map((e) => (
            <li
              key={e.event_id}
              className="flex flex-wrap items-center gap-2 rounded-md border border-border px-3 py-1.5 text-[12px]"
            >
              <StatusChip label={e.location_type} tone={e.location_type === "CFS" ? "info" : "warn"} />
              <span className="font-mono font-semibold text-foreground">{e.event_type}</span>
              {typeof e.details?.source_file === "string" && (
                <span className="text-[11px] text-muted-foreground">
                  {e.details.source_file as string}
                  {typeof e.details?.source_row === "number"
                    ? ` row ${e.details.source_row as number}`
                    : ""}
                </span>
              )}
              <span className="ml-auto tabular-nums text-muted-foreground">
                {fmtTs(e.event_ts)}
              </span>
            </li>
          ))}
          {events.length === 0 && (
            <li className="text-[12px] text-muted-foreground">No gate events.</li>
          )}
        </ol>
      </div>
    </div>
  );
}
