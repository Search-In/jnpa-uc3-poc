// CFS-ECY CODECO Movements — UC-III module 13 (additive, read-only).
//
// Off-dock container gate-movement console over /api/cfs-ecy. Composes the DTCCC
// kit (PageHeader / StatGrid / SegmentedTabs / StatusChip) like DriverMaster, and
// calls the typed `api.cfsEcy*` helpers directly. The list is FULLY SERVER-DRIVEN:
// container search, facility, mode and date-range filters, and pagination are all
// resolved by the backend (GET /api/cfs-ecy/movements), so search/filter span the
// entire dataset — never just a loaded page. Row click opens a container timeline
// dialog that also surfaces the EXISTING Container Lifecycle status.

import { useEffect, useState } from "react";
import { useQuery, keepPreviousData } from "@tanstack/react-query";
import {
  Boxes,
  ArrowDownToLine,
  ArrowUpFromLine,
  Container,
  Timer,
  GaugeCircle,
  Search,
  ChevronLeft,
  ChevronRight,
  Inbox,
  RotateCcw,
  LayoutList,
  UploadCloud,
} from "lucide-react";

import {
  PageContainer,
  PageHeader,
  StatGrid,
  StatCard,
  SegmentedTabs,
  FilterSelect,
  StatusChip,
  type Tone,
} from "@/components/ui/dtccc";
import { Card } from "@/components/ui/card";
import { LoadingState, ErrorState } from "@/components/ui/misc";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { authEnabled, getRole } from "@/lib/auth";
import CfsEcyUploadPanel from "@/screens/cfs/UploadPanel";

// Data Upload is a WRITE surface — show it only to control-room / customs / admin
// (the gateway enforces the same policy on /api/cfs-ecy).
const UPLOAD_ROLES = ["JNPA_TRAFFIC", "DTCCC_ADMIN", "TERMINAL_OPS", "CUSTOMS"];
const CAN_UPLOAD = !authEnabled() || UPLOAD_ROLES.includes(getRole() ?? "");
type TopTab = "browse" | "upload";

type Facility = "all" | "CFS" | "ECY";

const PAGE_SIZE = 15;

const inputCls =
  "h-9 rounded-md border border-border bg-background px-2 text-[13px] font-medium text-foreground outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary/20";

interface Movement {
  id: number;
  facility_type: string;
  container_number: string;
  iso_valid: boolean;
  event_ts: string;
  mode: string;
  source: string;
  source_file: string;
}

const facilityTone = (f: string): Tone => (f === "CFS" ? "info" : "warn");
const modeTone = (m: string): Tone => (m === "IN" ? "ok" : "neutral");

function useDebounced<T>(value: T, delay = 350): T {
  const [d, setD] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setD(value), delay);
    return () => clearTimeout(id);
  }, [value, delay]);
  return d;
}

function fmtTs(ts?: string): string {
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

function fmtDwell(h?: number | null): string {
  if (h === null || h === undefined) return "—";
  if (h < 48) return `${h.toFixed(1)} h`;
  return `${(h / 24).toFixed(1)} d`;
}

/** Map a thrown fetch/HTTP error to a friendly, non-technical message. */
function friendlyError(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err ?? "");
  if (/failed to fetch|networkerror|load failed/i.test(msg))
    return "Network error — check your connection and try again.";
  if (/\b(502|503|504)\b|service unavailable|gateway/i.test(msg))
    return "Server unavailable — the backend is not responding. Please retry shortly.";
  if (/\b(500)\b/.test(msg)) return "Something went wrong on the server. Please retry.";
  return "Unable to load data. Please retry.";
}

export default function CfsEcyMovements() {
  const [topTab, setTopTab] = useState<TopTab>("browse");
  const [tab, setTab] = useState<Facility>("all");
  const [mode, setMode] = useState<string>("");
  const [searchInput, setSearchInput] = useState("");
  const search = useDebounced(searchInput, 350);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);

  const facilityParam = tab === "all" ? undefined : tab;
  const fromParam = dateFrom || undefined;
  // Make the "to" bound inclusive of the whole selected day.
  const toParam = dateTo ? `${dateTo}T23:59:59` : undefined;
  const containerParam = search.trim() || undefined;

  // Reset pagination whenever any filter/search changes.
  useEffect(() => {
    setOffset(0);
  }, [tab, mode, search, dateFrom, dateTo]);

  const listQ = useQuery({
    queryKey: [
      "cfs-ecy-movements",
      facilityParam,
      mode,
      containerParam,
      fromParam,
      toParam,
      offset,
    ],
    queryFn: () =>
      api.cfsEcyMovements({
        facility: facilityParam,
        mode: mode || undefined,
        container: containerParam,
        from: fromParam,
        to: toParam,
        sort: "event_ts",
        direction: "desc",
        limit: PAGE_SIZE,
        offset,
      }),
    placeholderData: keepPreviousData, // keep prior rows visible while the next page/filter loads
  });

  // KPIs scope by the dimensions the stats endpoint aggregates on: facility + date
  // range. (Mode / container are table-level drill-downs the backend stats API does
  // not aggregate by; the backend is intentionally left unchanged.)
  const statsQ = useQuery({
    queryKey: ["cfs-ecy-stats", facilityParam, fromParam, toParam],
    queryFn: () => api.cfsEcyStats({ facility: facilityParam, from: fromParam, to: toParam }),
    placeholderData: keepPreviousData,
  });

  const rows: Movement[] = listQ.data?.items ?? [];
  const total = listQ.data?.total ?? 0;
  const s = statsQ.data;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const page = Math.floor(offset / PAGE_SIZE);
  const showingFrom = total ? offset + 1 : 0;
  const showingTo = offset + rows.length;

  const filtersActive = !!(mode || dateFrom || dateTo || searchInput);
  const resetFilters = () => {
    setMode("");
    setDateFrom("");
    setDateTo("");
    setSearchInput("");
  };

  return (
    <PageContainer>
      <PageHeader
        icon={Boxes}
        title="CFS / ECY Movements"
        subtitle="Off-dock container gate-in / gate-out (CODECO) — CFS & Empty Container Yard"
        updatedAt={statsQ.dataUpdatedAt}
        isFetching={statsQ.isFetching || listQ.isFetching}
        onRefresh={() => {
          statsQ.refetch();
          listQ.refetch();
        }}
      />

      {/* Top-level tabs: Browse the movements vs upload new CODECO files (module 13 sub-module). */}
      <div className="px-4 pt-3">
        <SegmentedTabs<TopTab>
          tabs={[
            { key: "browse", label: "Browse", icon: LayoutList },
            ...(CAN_UPLOAD
              ? [{ key: "upload" as TopTab, label: "Data Upload", icon: UploadCloud }]
              : []),
          ]}
          value={topTab}
          onChange={setTopTab}
        />
      </div>

      {topTab === "upload" && CAN_UPLOAD && (
        <div className="p-4">
          <CfsEcyUploadPanel />
        </div>
      )}

      <div className={`flex flex-col gap-4 p-4 ${topTab === "browse" ? "" : "hidden"}`}>
        {/* KPIs — scoped by facility + date range */}
        <StatGrid>
          <StatCard
            icon={ArrowDownToLine}
            label="Total Gate-IN"
            value={s?.total_in ?? "—"}
            tone="ok"
            loading={statsQ.isLoading}
          />
          <StatCard
            icon={ArrowUpFromLine}
            label="Total Gate-OUT"
            value={s?.total_out ?? "—"}
            tone="info"
            loading={statsQ.isLoading}
          />
          <StatCard
            icon={Container}
            label="Active in facility"
            value={s?.active_containers ?? "—"}
            tone="warn"
            sub="net IN (still inside)"
            loading={statsQ.isLoading}
          />
          <StatCard
            icon={GaugeCircle}
            label="Avg CFS dwell"
            value={fmtDwell(s?.average_dwell_hours)}
            tone="neutral"
            sub={s?.dwell_count ? `${s.dwell_count} cycles` : undefined}
            loading={statsQ.isLoading}
          />
          <StatCard
            icon={Timer}
            label="Median CFS dwell"
            value={fmtDwell(s?.median_dwell_hours)}
            tone="neutral"
            loading={statsQ.isLoading}
          />
          <StatCard
            icon={Boxes}
            label="Distinct containers"
            value={s?.container_count ?? "—"}
            tone="info"
            loading={statsQ.isLoading}
          />
        </StatGrid>

        {/* Facility tabs */}
        <SegmentedTabs<Facility>
          tabs={[
            { key: "all", label: "All", count: s?.total_events },
            { key: "CFS", label: "CFS" },
            { key: "ECY", label: "ECY" },
          ]}
          value={tab}
          onChange={setTab}
        />

        {/* Movements table (server-driven) */}
        <Card className="p-0">
          {/* Toolbar: server-side search + mode + date range + reset */}
          <div className="flex flex-wrap items-center gap-2 border-b border-border p-3">
            <div className="relative w-full max-w-xs">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <input
                type="search"
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                placeholder="Search container no… (all records)"
                className="h-9 w-full rounded-md border border-border bg-background py-1.5 pl-8 pr-3 text-[13px] outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary/20"
              />
            </div>
            <FilterSelect
              label="Mode"
              value={mode}
              onChange={setMode}
              options={[
                { value: "", label: "All modes" },
                { value: "IN", label: "Gate-IN" },
                { value: "OUT", label: "Gate-OUT" },
              ]}
            />
            <label className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
              From
              <input
                type="date"
                value={dateFrom}
                max={dateTo || undefined}
                onChange={(e) => setDateFrom(e.target.value)}
                className={inputCls}
                aria-label="From date"
              />
            </label>
            <label className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
              To
              <input
                type="date"
                value={dateTo}
                min={dateFrom || undefined}
                onChange={(e) => setDateTo(e.target.value)}
                className={inputCls}
                aria-label="To date"
              />
            </label>
            {filtersActive && (
              <button
                type="button"
                onClick={resetFilters}
                className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-[12px] font-medium text-foreground hover:bg-muted"
              >
                <RotateCcw className="h-3.5 w-3.5" /> Reset
              </button>
            )}
            {/* subtle inline fetching indicator (no unmount) */}
            {listQ.isFetching && !listQ.isLoading && (
              <span className="ml-auto text-[11px] text-muted-foreground">Updating…</span>
            )}
          </div>

          {listQ.isError ? (
            <ErrorState onRetry={() => listQ.refetch()} detail={friendlyError(listQ.error)} />
          ) : listQ.isLoading ? (
            <div className="p-6">
              <LoadingState />
            </div>
          ) : rows.length === 0 ? (
            <div className="flex flex-col items-center justify-center gap-2 py-12 text-center">
              <span className="flex h-12 w-12 items-center justify-center rounded-full bg-muted text-muted-foreground">
                <Inbox size={22} />
              </span>
              <div className="text-sm font-medium">No data found</div>
              <div className="max-w-xs text-[12px] text-muted-foreground">
                No CODECO movements match the current filters. Try clearing the search or date
                range.
              </div>
            </div>
          ) : (
            <>
              <div
                className={`overflow-x-auto transition-opacity ${listQ.isFetching ? "opacity-60" : ""}`}
              >
                <table className="w-full min-w-[640px] text-left text-[13px]">
                  <thead className="bg-muted/60 text-[11px] uppercase tracking-wide text-muted-foreground">
                    <tr>
                      <th className="px-3 py-2 font-semibold">Container No.</th>
                      <th className="px-3 py-2 font-semibold">Facility</th>
                      <th className="px-3 py-2 font-semibold">Mode</th>
                      <th className="px-3 py-2 font-semibold">Timestamp (IST)</th>
                      <th className="px-3 py-2 text-center font-semibold">ISO 6346</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {rows.map((r) => (
                      <tr
                        key={r.id}
                        onClick={() => setSelected(r.container_number)}
                        className="cursor-pointer hover:bg-muted/40"
                      >
                        <td className="px-3 py-2 font-mono font-semibold text-foreground">
                          {r.container_number}
                        </td>
                        <td className="px-3 py-2">
                          <StatusChip
                            label={r.facility_type}
                            tone={facilityTone(r.facility_type)}
                          />
                        </td>
                        <td className="px-3 py-2">
                          <StatusChip
                            label={
                              <span className="inline-flex items-center gap-1">
                                {r.mode === "IN" ? (
                                  <ArrowDownToLine className="h-3 w-3" />
                                ) : (
                                  <ArrowUpFromLine className="h-3 w-3" />
                                )}
                                {r.mode}
                              </span>
                            }
                            tone={modeTone(r.mode)}
                          />
                        </td>
                        <td className="px-3 py-2 tabular-nums">{fmtTs(r.event_ts)}</td>
                        <td className="px-3 py-2 text-center">
                          {r.iso_valid ? (
                            <StatusChip label="valid" tone="ok" />
                          ) : (
                            <StatusChip label="invalid" tone="critical" />
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {/* Server-side pagination */}
              <div className="flex flex-wrap items-center gap-2 border-t border-border px-3 py-2 text-[11.5px] text-muted-foreground">
                <span>
                  Showing{" "}
                  <span className="font-semibold text-foreground">
                    {showingFrom}–{showingTo}
                  </span>{" "}
                  of <span className="font-semibold text-foreground">{total.toLocaleString()}</span>{" "}
                  movements
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

      <ContainerTimelineDialog containerNumber={selected} onClose={() => setSelected(null)} />
    </PageContainer>
  );
}

// --- Container timeline detail --------------------------------------------------
function ContainerTimelineDialog({
  containerNumber,
  onClose,
}: {
  containerNumber: string | null;
  onClose: () => void;
}) {
  const q = useQuery({
    queryKey: ["cfs-ecy-container", containerNumber],
    queryFn: () => api.cfsEcyContainer(containerNumber as string),
    enabled: !!containerNumber,
  });

  const data = q.data;
  const events: Movement[] = data?.events ?? [];

  return (
    <Dialog open={!!containerNumber} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="font-mono">{containerNumber}</DialogTitle>
        </DialogHeader>

        {q.isLoading ? (
          <div className="py-8">
            <LoadingState />
          </div>
        ) : q.isError ? (
          <div className="py-8 text-center text-sm text-destructive">{friendlyError(q.error)}</div>
        ) : (
          <div className="flex flex-col gap-4">
            {/* Summary row */}
            <div className="flex flex-wrap gap-2">
              <StatusChip
                label={data?.iso_valid ? "ISO 6346 valid" : "ISO 6346 invalid"}
                tone={data?.iso_valid ? "ok" : "critical"}
              />
              {data?.dwell_hours !== null && data?.dwell_hours !== undefined && (
                <StatusChip label={`CFS dwell ${fmtDwell(data.dwell_hours)}`} tone="info" />
              )}
              {data?.in_lifecycle ? (
                <StatusChip
                  label={`Lifecycle: ${data?.cargo?.lifecycle_status ?? "tracked"}`}
                  tone="ok"
                />
              ) : (
                <StatusChip label="Not in Container Lifecycle" tone="neutral" />
              )}
            </div>

            {/* Cargo lifecycle context (soft link to jnpa.cargo) */}
            {data?.cargo && (
              <Card className="p-3 text-[13px]">
                <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                  Container Lifecycle (cargo)
                </div>
                <dl className="grid grid-cols-2 gap-x-4 gap-y-1">
                  <Field label="Lifecycle status" value={data.cargo.lifecycle_status} />
                  <Field label="Customs status" value={data.cargo.customs_status} />
                  <Field label="Yard block" value={data.cargo.yard_block} />
                  <Field label="Released" value={data.cargo.is_released ? "Yes" : "No"} />
                  <Field label="Vessel" value={data.cargo.vessel_name} />
                  <Field label="Haulage vehicle" value={data.cargo.vehicle_number} />
                </dl>
              </Card>
            )}

            {/* CODECO timeline */}
            <div>
              <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                CODECO gate timeline ({events.length})
              </div>
              <ol className="flex flex-col gap-2">
                {events.map((e) => (
                  <li
                    key={e.id}
                    className="flex items-center gap-3 rounded-md border border-border px-3 py-2 text-[13px]"
                  >
                    <StatusChip label={e.facility_type} tone={facilityTone(e.facility_type)} />
                    <StatusChip label={e.mode} tone={modeTone(e.mode)} />
                    <span className="ml-auto tabular-nums text-muted-foreground">
                      {fmtTs(e.event_ts)}
                    </span>
                  </li>
                ))}
                {events.length === 0 && (
                  <li className="text-sm text-muted-foreground">No gate events.</li>
                )}
              </ol>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function Field({ label, value }: { label: string; value?: React.ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-right font-medium text-foreground">{value ?? "—"}</dd>
    </>
  );
}
