// Berthing Reports — UC-III module 7 (additive). Per-terminal vessel-call console over
// /api/berthing. Composes the DTCCC kit (PageHeader / StatGrid / SegmentedTabs /
// StatusChip / FilterSelect) like CfsEcyMovements, and calls the typed api.berthing*
// helpers directly — FULLY SERVER-DRIVEN (search/filter/paginate resolved by the
// backend, spanning the whole dataset). Tabs: Dashboard (KPIs + per-terminal), Vessel
// List (table → Timeline dialog on row click), Data Upload (CSV/XLS/XLSX). Nothing mocked.
import { useEffect, useState } from "react";
import { useQuery, keepPreviousData } from "@tanstack/react-query";
import {
  Anchor,
  Ship,
  ShipWheel,
  LogOut,
  CheckCircle2,
  Timer,
  Search,
  ChevronLeft,
  ChevronRight,
  Inbox,
  RotateCcw,
  LayoutDashboard,
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
import { api } from "@/lib/api";
import { authEnabled, getRole } from "@/lib/auth";
import BerthingReportUpload from "@/screens/berthing/ReportUpload";
import BerthingTimelineDialog, { statusTone } from "@/screens/berthing/Timeline";

const UPLOAD_ROLES = ["JNPA_TRAFFIC", "DTCCC_ADMIN", "TERMINAL_OPS", "CUSTOMS"];
const CAN_UPLOAD = !authEnabled() || UPLOAD_ROLES.includes(getRole() ?? "");
type TopTab = "dashboard" | "list" | "upload";

const TERMINALS = ["APMT", "BMCT", "NSFT", "NSICT", "NSIGT"];
const STATUSES = [
  "EXPECTED",
  "ARRIVED",
  "BERTH_ASSIGNED",
  "BERTHING_STARTED",
  "CARGO_OPERATION",
  "COMPLETED",
  "DEPARTED",
];
const PAGE_SIZE = 15;

function useDebounced<T>(value: T, delay = 350): T {
  const [d, setD] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setD(value), delay);
    return () => clearTimeout(id);
  }, [value, delay]);
  return d;
}

function fmtTs(ts?: string | null): string {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString("en-IN", {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  } catch {
    return String(ts);
  }
}

function fmtHours(h?: number | null): string {
  if (h === null || h === undefined) return "—";
  return h < 48 ? `${h.toFixed(1)} h` : `${(h / 24).toFixed(1)} d`;
}

const terminalTone = (t: string): Tone =>
  t === "APMT" ? "info" : t === "BMCT" ? "warn" : t === "NSFT" ? "ok" : "neutral";

export default function Berthing() {
  const [topTab, setTopTab] = useState<TopTab>("dashboard");
  const [terminal, setTerminal] = useState<string>("");
  const [statusF, setStatusF] = useState<string>("");
  const [searchInput, setSearchInput] = useState("");
  const search = useDebounced(searchInput, 350);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<number | null>(null);

  const terminalParam = terminal || undefined;
  const statusParam = statusF || undefined;
  const vesselParam = search.trim() || undefined;

  useEffect(() => {
    setOffset(0);
  }, [terminal, statusF, search]);

  const statsQ = useQuery({
    queryKey: ["berthing-stats", terminalParam],
    queryFn: () => api.berthingStats({ terminal: terminalParam }),
    placeholderData: keepPreviousData,
  });

  const listQ = useQuery({
    queryKey: ["berthing-list", terminalParam, statusParam, vesselParam, offset],
    queryFn: () =>
      api.berthingReports({
        terminal: terminalParam,
        status: statusParam,
        vessel: vesselParam,
        sort: "updated_at",
        direction: "desc",
        limit: PAGE_SIZE,
        offset,
      }),
    placeholderData: keepPreviousData,
  });

  const s = statsQ.data;
  const rows: any[] = listQ.data?.items ?? [];
  const total = listQ.data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const page = Math.floor(offset / PAGE_SIZE);

  const filtersActive = !!(terminal || statusF || searchInput);
  const resetFilters = () => {
    setTerminal("");
    setStatusF("");
    setSearchInput("");
  };

  return (
    <PageContainer>
      <PageHeader
        icon={Anchor}
        title="Berthing Reports"
        subtitle="Per-terminal vessel calls — APMT · BMCT · NSFT · NSICT · NSIGT"
        updatedAt={statsQ.dataUpdatedAt}
        isFetching={statsQ.isFetching || listQ.isFetching}
        onRefresh={() => {
          statsQ.refetch();
          listQ.refetch();
        }}
      />

      <div className="px-4 pt-3">
        <SegmentedTabs<TopTab>
          tabs={[
            { key: "dashboard", label: "Dashboard", icon: LayoutDashboard },
            { key: "list", label: "Vessel List", icon: LayoutList },
            ...(CAN_UPLOAD
              ? [{ key: "upload" as TopTab, label: "Report Upload", icon: UploadCloud }]
              : []),
          ]}
          value={topTab}
          onChange={setTopTab}
        />
      </div>

      {/* Dashboard */}
      {topTab === "dashboard" && (
        <div className="flex flex-col gap-4 p-4">
          <StatGrid>
            <StatCard
              icon={Ship}
              label="Total vessels"
              value={s?.total ?? "—"}
              tone="info"
              loading={statsQ.isLoading}
            />
            <StatCard
              icon={Timer}
              label="Expected"
              value={s?.expected ?? "—"}
              tone="warn"
              loading={statsQ.isLoading}
            />
            <StatCard
              icon={Anchor}
              label="Arrivals"
              value={(s ? s.arrived + s.berthed + s.completed + s.departed : "—") as any}
              tone="neutral"
              sub="arrived or later"
              loading={statsQ.isLoading}
            />
            <StatCard
              icon={ShipWheel}
              label="Currently berthed"
              value={s?.berthed ?? "—"}
              tone="ok"
              loading={statsQ.isLoading}
            />
            <StatCard
              icon={LogOut}
              label="Departures"
              value={s?.departed ?? "—"}
              tone="neutral"
              loading={statsQ.isLoading}
            />
            <StatCard
              icon={CheckCircle2}
              label="Avg berth duration"
              value={fmtHours(s?.avg_berth_hours)}
              tone="info"
              loading={statsQ.isLoading}
            />
          </StatGrid>

          <Card className="p-0">
            <div className="border-b border-border px-3 py-2 text-sm font-semibold text-foreground">
              Per-terminal
            </div>
            {statsQ.isLoading ? (
              <div className="p-6">
                <LoadingState />
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[420px] text-left text-[13px]">
                  <thead className="bg-muted/60 text-[11px] uppercase tracking-wide text-muted-foreground">
                    <tr>
                      <th className="px-3 py-2 font-semibold">Terminal</th>
                      <th className="px-3 py-2 text-right font-semibold">Vessel calls</th>
                      <th className="px-3 py-2 text-right font-semibold">Currently berthed</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {(s?.by_terminal ?? []).map((t) => (
                      <tr key={t.terminal} className="hover:bg-muted/40">
                        <td className="px-3 py-2">
                          <StatusChip label={t.terminal} tone={terminalTone(t.terminal)} />
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums">{t.count}</td>
                        <td className="px-3 py-2 text-right tabular-nums">{t.berthed}</td>
                      </tr>
                    ))}
                    {(s?.by_terminal ?? []).length === 0 && (
                      <tr>
                        <td colSpan={3} className="px-3 py-8 text-center text-muted-foreground">
                          No berthing data yet — import the terminal reports.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </div>
      )}

      {/* Vessel List */}
      {topTab === "list" && (
        <div className="flex flex-col gap-4 p-4">
          <Card className="p-0">
            <div className="flex flex-wrap items-center gap-2 border-b border-border p-3">
              <div className="relative w-full max-w-xs">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <input
                  type="search"
                  value={searchInput}
                  onChange={(e) => setSearchInput(e.target.value)}
                  placeholder="Search vessel… (all records)"
                  className="h-9 w-full rounded-md border border-border bg-background py-1.5 pl-8 pr-3 text-[13px] outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary/20"
                />
              </div>
              <FilterSelect
                label="Terminal"
                value={terminal}
                onChange={setTerminal}
                options={[
                  { value: "", label: "All terminals" },
                  ...TERMINALS.map((t) => ({ value: t, label: t })),
                ]}
              />
              <FilterSelect
                label="Status"
                value={statusF}
                onChange={setStatusF}
                options={[
                  { value: "", label: "All statuses" },
                  ...STATUSES.map((v) => ({ value: v, label: v.replace(/_/g, " ") })),
                ]}
              />
              {filtersActive && (
                <button
                  type="button"
                  onClick={resetFilters}
                  className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-[12px] font-medium text-foreground hover:bg-muted"
                >
                  <RotateCcw className="h-3.5 w-3.5" /> Reset
                </button>
              )}
              {listQ.isFetching && !listQ.isLoading && (
                <span className="ml-auto text-[11px] text-muted-foreground">Updating…</span>
              )}
            </div>

            {listQ.isError ? (
              <ErrorState onRetry={() => listQ.refetch()} detail="Unable to load berthing data." />
            ) : listQ.isLoading ? (
              <div className="p-6">
                <LoadingState />
              </div>
            ) : rows.length === 0 ? (
              <div className="flex flex-col items-center justify-center gap-2 py-12 text-center">
                <span className="flex h-12 w-12 items-center justify-center rounded-full bg-muted text-muted-foreground">
                  <Inbox size={22} />
                </span>
                <div className="text-sm font-medium">No vessel calls found</div>
                <div className="max-w-xs text-[12px] text-muted-foreground">
                  No berthing rows match the current filters. Try clearing the search or import the
                  terminal reports.
                </div>
              </div>
            ) : (
              <>
                <div
                  className={`overflow-x-auto transition-opacity ${listQ.isFetching ? "opacity-60" : ""}`}
                >
                  <table className="w-full min-w-[820px] text-left text-[13px]">
                    <thead className="bg-muted/60 text-[11px] uppercase tracking-wide text-muted-foreground">
                      <tr>
                        <th className="px-3 py-2 font-semibold">Vessel</th>
                        <th className="px-3 py-2 font-semibold">IMO</th>
                        <th className="px-3 py-2 font-semibold">Voyage</th>
                        <th className="px-3 py-2 font-semibold">Terminal</th>
                        <th className="px-3 py-2 font-semibold">Berth</th>
                        <th className="px-3 py-2 font-semibold">ETA</th>
                        <th className="px-3 py-2 font-semibold">ATA</th>
                        <th className="px-3 py-2 font-semibold">Status</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {rows.map((r) => (
                        <tr
                          key={r.id}
                          onClick={() => setSelected(r.id)}
                          className="cursor-pointer hover:bg-muted/40"
                        >
                          <td className="px-3 py-2 font-semibold text-foreground">
                            {r.vessel_name}
                          </td>
                          <td className="px-3 py-2 tabular-nums text-muted-foreground">
                            {r.imo_number ?? "—"}
                          </td>
                          <td className="px-3 py-2 font-mono">{r.voyage_number}</td>
                          <td className="px-3 py-2">
                            <StatusChip label={r.terminal} tone={terminalTone(r.terminal)} />
                          </td>
                          <td className="px-3 py-2">{r.berth_number ?? "—"}</td>
                          <td className="px-3 py-2 tabular-nums">{fmtTs(r.eta)}</td>
                          <td className="px-3 py-2 tabular-nums">{fmtTs(r.ata)}</td>
                          <td className="px-3 py-2">
                            <StatusChip label={r.status} tone={statusTone(r.status)} />
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
                      {total ? offset + 1 : 0}–{offset + rows.length}
                    </span>{" "}
                    of{" "}
                    <span className="font-semibold text-foreground">{total.toLocaleString()}</span>{" "}
                    calls
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
      )}

      {/* Report Upload — one flow for PDF (full extract) + CSV/XLS/XLSX (structured) */}
      {topTab === "upload" && CAN_UPLOAD && (
        <div className="p-4">
          <BerthingReportUpload />
        </div>
      )}

      <BerthingTimelineDialog reportId={selected} onClose={() => setSelected(null)} />
    </PageContainer>
  );
}
