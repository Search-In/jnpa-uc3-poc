// Performance & Daily Reports (UC-III module 12) — read-only analytical dashboard
// over the official JNPA Daily Status Report, monthly JN Port TEUs, and NLDS/LDB
// Analytics feeds (jnpa.perf_* via /api/performance/*). Mirrors the DTCCC report
// pattern: PageHeader + StatGrid + SegmentedTabs + recharts + DataTable (CSV/Print).
// All data is live from the local Postgres — nothing is mocked.

import { useState } from "react";
import { useQuery, keepPreviousData, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  BarChart3,
  Container,
  GaugeCircle,
  Layers,
  Ship,
  Snowflake,
  TimerReset,
  TrendingUp,
  Truck,
  UploadCloud,
} from "lucide-react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "@/lib/api";
import {
  PageContainer,
  PageHeader,
  StatGrid,
  StatCard,
  StatusChip,
  SegmentedTabs,
  FilterSelect,
  DataTable,
  type Column,
  type Tone,
} from "@/components/ui/dtccc";
import { Card } from "@/components/ui/card";
import { STATUS } from "@/lib/tokens";
import { authEnabled, getRole } from "@/lib/auth";
import UploadPanel from "@/screens/perf/UploadPanel";

const POLL_MS = 30_000;

type TabKey = "overview" | "daily" | "monthly" | "ldb" | "upload";
// Data Upload is a WRITE surface — show it only to admins (gateway enforces it too).
const IS_ADMIN = !authEnabled() || getRole() === "DTCCC_ADMIN";

// --- helpers -----------------------------------------------------------------
function num(v: unknown): string {
  if (v == null || v === "") return "—";
  const n = Number(v);
  return Number.isNaN(n) ? String(v) : n.toLocaleString("en-IN");
}
// Whole-number grouping for large KPI values (e.g. tonnage) — the raw figure
// carries 2 decimals (408104.38) which overflow/clip the fixed-width StatCard.
function numInt(v: unknown): string {
  if (v == null || v === "") return "—";
  const n = Number(v);
  return Number.isNaN(n) ? String(v) : Math.round(n).toLocaleString("en-IN");
}
function hrs(v: unknown): string {
  if (v == null) return "—";
  const n = Number(v);
  return Number.isNaN(n) ? "—" : `${n.toFixed(1)} h`;
}
function pctStr(v: unknown): string {
  if (v == null) return "—";
  const n = Number(v);
  return Number.isNaN(n) ? "—" : `${n.toFixed(1)}%`;
}
function deltaSub(d: number | undefined, suffix = ""): { text: string; tone: Tone } | undefined {
  if (d == null || Number.isNaN(d)) return undefined;
  const sign = d > 0 ? "▲" : d < 0 ? "▼" : "▬";
  const tone: Tone = d > 0 ? "ok" : d < 0 ? "critical" : "neutral";
  // Percentages keep 1 dp; absolute counts (TEUs/tonnes/vessels) round to a whole
  // number so a long "2,29,840.65" doesn't wrap awkwardly under the card.
  const abs = Math.abs(d);
  const shown = suffix === "%" ? abs.toFixed(1) : Math.round(abs).toLocaleString("en-IN");
  return { text: `${sign} ${shown}${suffix} vs prev day`, tone };
}
function occTone(pct: unknown): Tone {
  const n = Number(pct);
  if (Number.isNaN(n)) return "neutral";
  return n >= 85 ? "critical" : n >= 70 ? "warn" : "ok";
}
function congTone(level?: string): Tone {
  return level === "HIGH" ? "critical" : level === "MEDIUM" ? "warn" : "ok";
}
function exportCsv(
  filename: string,
  headers: string[],
  rows: (string | number | null | undefined)[][],
) {
  const esc = (v: unknown) => {
    const s = String(v ?? "");
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const csv = [headers, ...rows].map((r) => r.map(esc).join(",")).join("\r\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
const BTN =
  "inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-muted";
function Toolbar({ onExport }: { onExport: () => void }) {
  return (
    <div className="ml-auto flex items-center gap-2">
      <button type="button" onClick={onExport} className={BTN}>
        Export CSV
      </button>
      <button type="button" onClick={() => window.print()} className={BTN}>
        Print
      </button>
    </div>
  );
}
function mergeStatus(...qs: { isLoading: boolean; isError: boolean; error?: unknown }[]) {
  return {
    isLoading: qs.some((q) => q.isLoading),
    isError: qs.some((q) => q.isError),
    error: qs.find((q) => q.isError)?.error,
  };
}
const CHART_AXIS = { fontSize: 10 };

// =====================================================================
export default function PerformanceReports() {
  const [tab, setTab] = useState<TabKey>("overview");
  const qc = useQueryClient();

  const metaQ = useQuery({
    queryKey: ["perf-meta"],
    queryFn: () => api.perfMeta(),
    staleTime: 60_000,
  });
  const dates = metaQ.data?.report_dates ?? [];
  const latest = metaQ.data?.latest_report_date ?? null;
  const ldbMonths = metaQ.data?.ldb_months ?? [];

  // Manual refresh must refetch what the user is actually looking at — invalidate
  // every active /api/performance query (keys all start with "perf-"), not just meta.
  const refreshAll = () =>
    qc.invalidateQueries({
      predicate: (query) =>
        Array.isArray(query.queryKey) && String(query.queryKey[0]).startsWith("perf-"),
    });

  return (
    <PageContainer>
      <PageHeader
        icon={BarChart3}
        title="Performance & Daily Reports"
        subtitle="JNPA Daily Status • monthly JN Port TEUs • NLDS/LDB dwell benchmarks"
        updatedAt={metaQ.dataUpdatedAt}
        isFetching={metaQ.isFetching}
        onRefresh={refreshAll}
      />
      <div className="flex flex-col gap-4 p-4">
        <SegmentedTabs
          tabs={[
            { key: "overview", label: "Overview", icon: Activity },
            { key: "daily", label: "Daily Status", icon: Container },
            { key: "monthly", label: "Monthly TEU", icon: TrendingUp },
            { key: "ldb", label: "Dwell & LDB", icon: TimerReset },
            ...(IS_ADMIN
              ? [{ key: "upload" as TabKey, label: "Data Upload", icon: UploadCloud }]
              : []),
          ]}
          value={tab}
          onChange={setTab}
        />
        {tab === "overview" && <OverviewTab />}
        {tab === "daily" && <DailyTab dates={dates} latest={latest} />}
        {tab === "monthly" && <MonthlyTab />}
        {tab === "ldb" && <LdbTab months={ldbMonths} />}
        {tab === "upload" && IS_ADMIN && <UploadPanel />}
      </div>
    </PageContainer>
  );
}

// --- OVERVIEW ----------------------------------------------------------------
function OverviewTab() {
  // /stats already returns latest_kpi (computed server-side), so we derive the KPI
  // cards from it — one request instead of two (no duplicate backend KPI compute).
  const statsQ = useQuery({
    queryKey: ["perf-stats"],
    queryFn: () => api.perfStats(),
    refetchInterval: POLL_MS,
  });
  const m = statsQ.data?.latest_kpi?.metrics ?? {};
  const d = statsQ.data?.latest_kpi?.deltas ?? {};
  const kpiLoading = statsQ.isLoading;

  const series = (statsQ.data?.daily ?? []).map((r) => ({
    day: r.day.slice(5),
    teus: r.total_teus ?? 0,
    gate_in: r.gate_in_teus ?? 0,
    gate_out: r.gate_out_teus ?? 0,
    occ: r.yard_occupancy_pct ?? 0,
  }));

  return (
    <div className="flex flex-col gap-4">
      <StatGrid className="lg:grid-cols-4 xl:grid-cols-7">
        <StatCard
          icon={Container}
          label="Container TEUs (day)"
          value={num(m.total_teus)}
          tone="info"
          loading={kpiLoading}
          sub={sub(deltaSub(d.total_teus))}
        />
        <StatCard
          icon={Layers}
          label="Throughput (tonnes)"
          value={numInt(m.total_tonnes)}
          tone="info"
          loading={kpiLoading}
          sub={sub(deltaSub(d.total_tonnes))}
        />
        <StatCard
          icon={Ship}
          label="Vessel calls"
          value={num(m.vessel_calls)}
          tone="neutral"
          loading={kpiLoading}
          sub={sub(deltaSub(d.vessel_calls))}
        />
        <StatCard
          icon={GaugeCircle}
          label="Yard occupancy"
          value={pctStr(m.yard_occupancy_pct)}
          tone={occTone(m.yard_occupancy_pct)}
          loading={kpiLoading}
          sub={sub(deltaSub(d.yard_occupancy_pct, "%"))}
        />
        <StatCard
          icon={TimerReset}
          label="Import pendency"
          value={num(m.total_pendency_teus)}
          tone="warn"
          loading={kpiLoading}
          sub="ICD + CFS (TEUs)"
        />
        <StatCard
          icon={Truck}
          label="Gate total"
          value={num(m.gate_total_teus)}
          tone="ok"
          loading={kpiLoading}
          sub={sub(deltaSub(d.gate_total_teus))}
        />
        <StatCard
          icon={Snowflake}
          label="Reefer available"
          value={num(m.reefer_available_slots)}
          tone="ok"
          loading={kpiLoading}
          sub={m.reefer_total_slots ? `of ${num(m.reefer_total_slots)} slots` : undefined}
        />
      </StatGrid>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card className="p-3">
          <h2 className="mb-2 text-sm font-semibold text-foreground">
            JN Port container TEUs — daily trend
          </h2>
          <div className="h-[280px]">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={series} margin={{ top: 6, right: 10, bottom: 0, left: -12 }}>
                <defs>
                  <linearGradient id="teuGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={STATUS.info} stopOpacity={0.5} />
                    <stop offset="100%" stopColor={STATUS.info} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(215 20% 90%)" />
                <XAxis
                  dataKey="day"
                  tick={CHART_AXIS}
                  interval="preserveStartEnd"
                  minTickGap={28}
                />
                <YAxis tick={CHART_AXIS} width={44} />
                <Tooltip contentStyle={{ fontSize: 12, borderRadius: 8 }} />
                <Area
                  type="monotone"
                  dataKey="teus"
                  name="TEUs"
                  stroke={STATUS.info}
                  fill="url(#teuGrad)"
                  strokeWidth={2}
                  isAnimationActive={false}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </Card>
        <Card className="p-3">
          <h2 className="mb-2 text-sm font-semibold text-foreground">
            Gate movements — daily IN / OUT (TEUs)
          </h2>
          <div className="h-[280px]">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={series} margin={{ top: 6, right: 10, bottom: 0, left: -12 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(215 20% 90%)" />
                <XAxis
                  dataKey="day"
                  tick={CHART_AXIS}
                  interval="preserveStartEnd"
                  minTickGap={28}
                />
                <YAxis tick={CHART_AXIS} width={44} />
                <Tooltip contentStyle={{ fontSize: 12, borderRadius: 8 }} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                <Bar
                  dataKey="gate_in"
                  name="Gate IN"
                  fill={STATUS.ok}
                  radius={[2, 2, 0, 0]}
                  isAnimationActive={false}
                />
                <Bar
                  dataKey="gate_out"
                  name="Gate OUT"
                  fill={STATUS.warning}
                  radius={[2, 2, 0, 0]}
                  isAnimationActive={false}
                />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>
      </div>
    </div>
  );
}
function sub(d?: { text: string; tone: Tone }) {
  if (!d) return undefined;
  const colour =
    d.tone === "ok" ? STATUS.ok : d.tone === "critical" ? STATUS.critical : STATUS.unknown;
  return <span style={{ color: colour }}>{d.text}</span>;
}

// --- DAILY STATUS ------------------------------------------------------------
function DailyTab({ dates, latest }: { dates: string[]; latest: string | null }) {
  const [date, setDate] = useState<string>("");
  const active = date || latest || "";
  const bundleQ = useQuery({
    queryKey: ["perf-daily", active],
    queryFn: () => api.perfDaily(active),
    enabled: !!active,
    placeholderData: keepPreviousData,
  });
  const b = bundleQ.data;
  const status: any[] = b?.status ?? [];
  const traffic: any[] = (b?.traffic ?? []).filter((r: any) => r.period === "DAY");
  const vessels: any[] = b?.vessels ?? [];

  const statusCols: Column<any>[] = [
    {
      key: "terminal_code",
      header: "Terminal",
      render: (r) => <span className="font-semibold">{r.terminal_code}</span>,
    },
    {
      key: "yard_occupancy_pct",
      header: "Yard occ.",
      align: "right",
      render: (r) => (
        <StatusChip label={pctStr(r.yard_occupancy_pct)} tone={occTone(r.yard_occupancy_pct)} />
      ),
    },
    {
      key: "yard_total_teus",
      header: "Yard TEUs",
      align: "right",
      render: (r) => num(r.yard_total_teus),
    },
    {
      key: "icd_pendency_teus",
      header: "ICD pend.",
      align: "right",
      render: (r) => num(r.icd_pendency_teus),
    },
    {
      key: "cfs_pendency_teus",
      header: "CFS pend.",
      align: "right",
      render: (r) => num(r.cfs_pendency_teus),
    },
    {
      key: "gate_total_teus",
      header: "Gate total",
      align: "right",
      render: (r) => num(r.gate_total_teus),
    },
    {
      key: "reefer_available_slots",
      header: "Reefer avail.",
      align: "right",
      render: (r) => num(r.reefer_available_slots),
    },
  ];
  const vesselCols: Column<any>[] = [
    {
      key: "terminal_code",
      header: "Terminal",
      render: (r) => <span className="font-semibold">{r.terminal_code}</span>,
    },
    { key: "berth_no", header: "Berth" },
    {
      key: "via_no",
      header: "Voyage",
      render: (r) => <span className="font-mono">{r.via_no || "—"}</span>,
    },
    {
      key: "vessel_name",
      header: "Vessel",
      render: (r) => <span className="font-mono">{r.vessel_name}</span>,
    },
    { key: "cargo_commodity", header: "Cargo" },
    {
      key: "berthed_on",
      header: "Berthed",
      render: (r) => (r.berthed_on ? String(r.berthed_on).replace("T", " ").slice(0, 16) : "—"),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-[12px] font-medium text-muted-foreground">Report date</label>
        <FilterSelect
          label="Report date"
          value={active}
          onChange={setDate}
          options={dates.map((d) => ({ value: d, label: d }))}
        />
        {b?.snapshot?.as_of_ts && (
          <span className="text-[12px] text-muted-foreground">
            As on {String(b.snapshot.as_of_ts).replace("T", " ").slice(0, 16)} IST
          </span>
        )}
      </div>

      <Card className="p-3">
        <h2 className="mb-2 text-sm font-semibold text-foreground">
          Container terminal TEUs (day) — by terminal
        </h2>
        <div className="h-[260px]">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart
              data={traffic.filter((r) => r.terminal_code !== "JN_PORT" && r.imp_teus != null)}
              margin={{ top: 6, right: 10, bottom: 0, left: -8 }}
            >
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(215 20% 90%)" />
              <XAxis dataKey="terminal_code" tick={CHART_AXIS} />
              <YAxis tick={CHART_AXIS} width={48} />
              <Tooltip contentStyle={{ fontSize: 12, borderRadius: 8 }} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <Bar
                dataKey="imp_teus"
                name="Import"
                stackId="a"
                fill={STATUS.info}
                isAnimationActive={false}
              />
              <Bar
                dataKey="exp_teus"
                name="Export"
                stackId="a"
                fill={STATUS.ok}
                isAnimationActive={false}
              />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Card>

      <Card className="p-0">
        <div className="border-b border-border px-3 py-2 text-sm font-semibold text-foreground">
          Terminal status — pendency / yard / gate / reefer
        </div>
        <DataTable
          columns={statusCols}
          rows={status}
          rowKey={(r) => `${r.report_date}-${r.terminal_code}`}
          status={mergeStatus(bundleQ)}
          onRetry={() => bundleQ.refetch()}
          emptyLabel="No terminal status for this date."
          pageSize={10}
          toolbar={
            <Toolbar
              onExport={() =>
                exportCsv(
                  `daily-status-${active}.csv`,
                  [
                    "terminal",
                    "yard_occupancy_pct",
                    "yard_total_teus",
                    "icd_pendency",
                    "cfs_pendency",
                    "gate_total",
                    "reefer_available",
                  ],
                  status.map((r) => [
                    r.terminal_code,
                    r.yard_occupancy_pct,
                    r.yard_total_teus,
                    r.icd_pendency_teus,
                    r.cfs_pendency_teus,
                    r.gate_total_teus,
                    r.reefer_available_slots,
                  ]),
                )
              }
            />
          }
        />
      </Card>

      <Card className="p-0">
        <div className="border-b border-border px-3 py-2 text-sm font-semibold text-foreground">
          Vessels under operation ({vessels.length})
        </div>
        <DataTable
          columns={vesselCols}
          rows={vessels}
          rowKey={(r) => `${r.report_date}-${r.terminal_code}-${r.berth_no}-${r.via_no ?? ""}`}
          status={mergeStatus(bundleQ)}
          onRetry={() => bundleQ.refetch()}
          emptyLabel="No vessels under operation for this date."
          search={(r, q) =>
            `${r.vessel_name} ${r.terminal_code} ${r.cargo_commodity} ${r.via_no}`
              .toLowerCase()
              .includes(q)
          }
          searchPlaceholder="Search vessel / terminal / cargo…"
          pageSize={10}
        />
      </Card>
    </div>
  );
}

// --- MONTHLY TEU -------------------------------------------------------------
const TERMINALS = ["JN_PORT", "NSFT", "NSICT", "NSIGT", "APMT", "BMCT", "NSDT"];
function MonthlyTab() {
  const [terminal, setTerminal] = useState("JN_PORT");
  const q = useQuery({
    queryKey: ["perf-monthly", terminal],
    queryFn: () => api.perfMonthly({ terminal, sort: "month_date", direction: "asc", limit: 60 }),
    placeholderData: keepPreviousData,
  });
  const rows: any[] = q.data?.items ?? [];
  const chart = rows.map((r) => ({
    m: `${r.month_label}'${String(r.year_label).slice(2)}`,
    total: Number(r.total_teus) || 0,
    disch: Number(r.discharge_teus) || 0,
    load: Number(r.load_teus) || 0,
  }));
  const cols: Column<any>[] = [
    { key: "month", header: "Month", render: (r) => `${r.month_label} ${r.year_label}` },
    { key: "fiscal_year", header: "FY" },
    {
      key: "vessel_calls",
      header: "Vessel calls",
      align: "right",
      render: (r) => num(r.vessel_calls),
    },
    {
      key: "discharge_teus",
      header: "Discharge",
      align: "right",
      render: (r) => num(r.discharge_teus),
    },
    { key: "load_teus", header: "Load", align: "right", render: (r) => num(r.load_teus) },
    {
      key: "total_teus",
      header: "Total TEUs",
      align: "right",
      render: (r) => <span className="font-semibold">{num(r.total_teus)}</span>,
    },
  ];
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-[12px] font-medium text-muted-foreground">Terminal</label>
        <FilterSelect
          label="Terminal"
          value={terminal}
          onChange={setTerminal}
          options={TERMINALS.map((t) => ({ value: t, label: t }))}
        />
      </div>
      <Card className="p-3">
        <h2 className="mb-2 text-sm font-semibold text-foreground">Monthly TEUs — {terminal}</h2>
        <div className="h-[300px]">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={chart} margin={{ top: 6, right: 10, bottom: 0, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(215 20% 90%)" />
              <XAxis dataKey="m" tick={CHART_AXIS} />
              <YAxis tick={CHART_AXIS} width={54} />
              <Tooltip contentStyle={{ fontSize: 12, borderRadius: 8 }} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <Bar
                dataKey="disch"
                name="Discharge"
                stackId="a"
                fill={STATUS.info}
                isAnimationActive={false}
              />
              <Bar
                dataKey="load"
                name="Load"
                stackId="a"
                fill={STATUS.ok}
                isAnimationActive={false}
              />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Card>
      <Card className="p-0">
        <DataTable
          columns={cols}
          rows={rows}
          rowKey={(r) => `${r.month_date}-${r.terminal_code}`}
          status={mergeStatus(q)}
          onRetry={() => q.refetch()}
          emptyLabel="No monthly TEU data."
          pageSize={13}
          toolbar={
            <Toolbar
              onExport={() =>
                exportCsv(
                  `monthly-teu-${terminal}.csv`,
                  [
                    "month",
                    "fiscal_year",
                    "vessel_calls",
                    "discharge_teus",
                    "load_teus",
                    "total_teus",
                  ],
                  rows.map((r) => [
                    `${r.month_label} ${r.year_label}`,
                    r.fiscal_year,
                    r.vessel_calls,
                    r.discharge_teus,
                    r.load_teus,
                    r.total_teus,
                  ]),
                )
              }
            />
          }
        />
      </Card>
    </div>
  );
}

// --- DWELL & LDB -------------------------------------------------------------
function LdbTab({ months }: { months: string[] }) {
  const month = months[0] || "";
  const dwellQ = useQuery({
    queryKey: ["perf-dwell", month, "OVERALL"],
    queryFn: () => api.perfDwell({ month, segment: "OVERALL" }),
    enabled: !!month,
  });
  const facQ = useQuery({
    queryKey: ["perf-cfsicd", month],
    queryFn: () => api.perfCfsIcd({ month, limit: 200 }),
    enabled: !!month,
  });
  const congQ = useQuery({
    queryKey: ["perf-cong", month, "IMPORT"],
    queryFn: () => api.perfCongestion({ month, cycle: "IMPORT" }),
    enabled: !!month,
  });

  const dwell: any[] = dwellQ.data?.items ?? [];
  // pivot import/export overall by terminal for a grouped bar chart
  const byTerm: Record<string, { terminal: string; import?: number; export?: number }> = {};
  dwell.forEach((r) => {
    const t = (byTerm[r.terminal_code] ??= { terminal: r.terminal_code });
    if (r.cycle === "IMPORT") t.import = Number(r.dwell_hours);
    else t.export = Number(r.dwell_hours);
  });
  const dwellChart = Object.values(byTerm);

  const facRows: any[] = facQ.data?.items ?? [];
  const cong: any[] = congQ.data?.items ?? [];

  const facCols: Column<any>[] = [
    {
      key: "facility_type",
      header: "Type",
      render: (r) => (
        <StatusChip label={r.facility_type} tone={r.facility_type === "CFS" ? "info" : "neutral"} />
      ),
    },
    { key: "facility_name", header: "Facility" },
    {
      key: "dwell_hours",
      header: "Dwell (Mar)",
      align: "right",
      render: (r) => hrs(r.dwell_hours),
    },
    {
      key: "dwell_hours_prev",
      header: "Dwell (Feb)",
      align: "right",
      render: (r) => hrs(r.dwell_hours_prev),
    },
  ];
  const congCols: Column<any>[] = [
    { key: "cluster_no", header: "Cluster", render: (r) => `#${r.cluster_no}` },
    { key: "cluster_name", header: "Area" },
    { key: "cfs_count", header: "CFS", align: "right", render: (r) => num(r.cfs_count) },
    {
      key: "pct_containers",
      header: "% containers",
      align: "right",
      render: (r) => pctStr(r.pct_containers),
    },
    {
      key: "congestion_level",
      header: "Congestion",
      render: (r) => <StatusChip label={r.congestion_level} tone={congTone(r.congestion_level)} />,
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="text-[12px] text-muted-foreground">
        NLDS / LDB Analytics — report month <span className="font-semibold">{month || "—"}</span>{" "}
        (vs previous month)
      </div>
      <Card className="p-3">
        <h2 className="mb-2 text-sm font-semibold text-foreground">
          Port dwell time (overall) — import vs export, by terminal
        </h2>
        <div className="h-[300px]">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={dwellChart} margin={{ top: 6, right: 10, bottom: 0, left: -8 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(215 20% 90%)" />
              <XAxis dataKey="terminal" tick={CHART_AXIS} />
              <YAxis tick={CHART_AXIS} width={44} unit="h" />
              <Tooltip
                contentStyle={{ fontSize: 12, borderRadius: 8 }}
                formatter={(v: number | string, n) => [`${Number(v).toFixed(1)} h`, n]}
              />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <Bar
                dataKey="import"
                name="Import"
                fill={STATUS.info}
                radius={[2, 2, 0, 0]}
                isAnimationActive={false}
              />
              <Bar
                dataKey="export"
                name="Export"
                fill={STATUS.warning}
                radius={[2, 2, 0, 0]}
                isAnimationActive={false}
              />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Card>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card className="p-0">
          <div className="border-b border-border px-3 py-2 text-sm font-semibold text-foreground">
            CFS / ICD facility dwell
          </div>
          <DataTable
            columns={facCols}
            rows={facRows}
            rowKey={(r) => `${r.facility_type}-${r.facility_name}`}
            status={mergeStatus(facQ)}
            onRetry={() => facQ.refetch()}
            emptyLabel="No facility dwell data."
            search={(r, q) => String(r.facility_name).toLowerCase().includes(q)}
            searchPlaceholder="Search CFS / ICD…"
            pageSize={10}
            toolbar={
              <Toolbar
                onExport={() =>
                  exportCsv(
                    `ldb-facility-dwell-${month}.csv`,
                    ["type", "facility", "dwell_hours", "dwell_hours_prev"],
                    facRows.map((r) => [
                      r.facility_type,
                      r.facility_name,
                      r.dwell_hours,
                      r.dwell_hours_prev,
                    ]),
                  )
                }
              />
            }
          />
        </Card>
        <Card className="p-0">
          <div className="border-b border-border px-3 py-2 text-sm font-semibold text-foreground">
            Import congestion clusters
          </div>
          <DataTable
            columns={congCols}
            rows={cong}
            rowKey={(r) => `${r.cycle}-${r.cluster_no}`}
            status={mergeStatus(congQ)}
            onRetry={() => congQ.refetch()}
            emptyLabel="No congestion data."
            pageSize={10}
          />
        </Card>
      </div>
    </div>
  );
}
