// FASTag (ULIP) — financial & journey dashboard (FINAL PHASE redesign).
// A single RC drives Balance / Transactions / Journey / History; Toll-Enroute and
// Health are standalone. Every figure is RDS-backed (jnpa.fastag_*) via the
// DataAdapter / /api/fastag/* — the data source (LIVE ULIP / RDS store / SIM) is
// surfaced as a badge. Query keys and endpoints are UNCHANGED — no backend edits.

import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  CreditCard,
  Search,
  Wallet,
  ArrowLeftRight,
  Nfc,
  Route,
  Activity,
  CalendarClock,
  MapPin,
  ArrowRightLeft,
} from "lucide-react";
import { getAdapter } from "@/data";
import { api } from "@/lib/api";
import { useGlobalSearch } from "@/lib/searchStore";
import { Card } from "@/components/ui/card";
import {
  PageContainer,
  PageHeader,
  StatGrid,
  StatCard,
  SegmentedTabs,
  DataTable,
  StatusChip,
  type Column,
  type Tone,
} from "@/components/ui/dtccc";
import { EmptyState, LoadingState } from "@/components/ui/misc";
import { fmtDateTimeIST, relativeAge } from "@/lib/utils";
import type {
  FastagTransactionRow,
  FastagTransactions,
  TollEnroute,
  TollEnrouteInput,
} from "@/lib/types";

const VEHICLE_TYPES = ["CAR", "LMV", "LGV", "HGV", "TRUCK", "BUS", "MAV", "MMV", "2W", "3W"];
type TabKey = "balance" | "transactions" | "journey" | "enroute" | "history" | "health";

function friendlyError(e: unknown): string {
  const msg = e instanceof Error ? e.message : String(e);
  if (/\b400\b/.test(msg)) return "Invalid RC number format (e.g. MH12AB1234).";
  if (/\b504\b/.test(msg)) return "The FASTag provider timed out — please try again.";
  if (msg.includes('"config"')) return "FASTag provider is not configured yet.";
  if (/\b502\b/.test(msg)) return "FASTag provider is currently unavailable — try again later.";
  if (/\b40[13]\b/.test(msg)) return "You don't have permission to view FASTag data.";
  if (/\b5\d\d\b/.test(msg)) return "FASTag service error — please try again.";
  return "Something went wrong — please try again.";
}

function tagTone(status?: string | null): Tone {
  const s = (status ?? "").toLowerCase();
  if (s.includes("activ")) return "ok";
  if (s.includes("low")) return "warn";
  if (s.includes("block") || s.includes("blacklist")) return "critical";
  return "neutral";
}
function sourceTone(src?: string | null): Tone {
  const s = (src ?? "").toUpperCase();
  if (s === "LIVE") return "ok";
  if (s === "RDS") return "info";
  if (s === "SIM") return "warn";
  return "neutral";
}

/** Combined transactions fetch: best-effort LIVE refresh (persists to RDS) → read stored history. */
async function fetchTransactions(rc: string): Promise<FastagTransactions> {
  let live: FastagTransactions | null = null;
  try {
    live = await getAdapter().fastagTransactions(rc);
  } catch {
    live = null;
  }
  const h = await api.fastagTransactionsHistory(rc, 500);
  if (!h.transactions.length) {
    if (live && live.transactions.length) return live;
    throw new Error(`No stored FASTag transactions found for ${rc}.`);
  }
  return {
    transactions: h.transactions,
    source: "RDS",
    fetch_source: live?.fetch_source ?? "UNAVAILABLE",
    stored_count: h.count,
    inserted_count: live?.inserted_count ?? 0,
    skipped_count: live?.skipped_count ?? 0,
    failed_count: live?.failed_count ?? 0,
    total: h.count,
    correlation_id: live?.correlation_id ?? "",
    rc_number: h.rc_number,
  } satisfies FastagTransactions;
}

export default function Fastag() {
  const [tab, setTab] = useState<TabKey>("balance");
  const [rcInput, setRcInput] = useState("");
  const [rc, setRc] = useState("");

  // Global Search / deep-link hand-off.
  const gs = useGlobalSearch();
  const [params] = useSearchParams();
  useEffect(() => {
    if ((gs.entity === "fastag" || gs.entity === "vehicle") && gs.query) {
      setRcInput(gs.query.toUpperCase());
      setRc(gs.query.toUpperCase());
      setTab("balance");
    }
  }, [gs.nonce]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const q = params.get("q");
    if (q && !rc) {
      setRcInput(q.toUpperCase());
      setRc(q.toUpperCase());
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const enabled = !!rc;
  const balanceQ = useQuery({
    queryKey: ["fastag-balance", rc],
    queryFn: () => getAdapter().fastagBalance(rc),
    enabled,
    retry: false,
  });
  const txQ = useQuery({
    queryKey: ["fastag-tx", rc],
    queryFn: () => fetchTransactions(rc),
    enabled,
    retry: false,
  });
  const healthQ = useQuery({
    queryKey: ["fastag-health"],
    queryFn: () => getAdapter().fastagHealth(),
    refetchInterval: 15000,
    retry: false,
  });

  const b = balanceQ.data;
  const tx = txQ.data;
  const rows = tx?.transactions ?? [];

  const todayCount = useMemo(() => {
    const today = new Date().toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata" });
    return rows.filter(
      (r) =>
        r.transaction_date_time &&
        new Date(r.transaction_date_time).toLocaleDateString("en-IN", {
          timeZone: "Asia/Kolkata",
        }) === today,
    ).length;
  }, [rows]);

  const systemStatus = healthQ.isError
    ? "DOWN"
    : healthQ.data?.status === "ok"
      ? healthQ.data?.ulip_configured
        ? "LIVE"
        : "RDS"
      : "SIM";

  function run() {
    const v = rcInput.trim().toUpperCase();
    if (v) setRc(v);
  }

  return (
    <PageContainer>
      <PageHeader
        icon={CreditCard}
        title="FASTag"
        subtitle="NETC · ULIP · RDS-backed financial & journey dashboard"
        updatedAt={txQ.dataUpdatedAt || balanceQ.dataUpdatedAt}
        isFetching={txQ.isFetching || balanceQ.isFetching}
        onRefresh={() => {
          void balanceQ.refetch();
          void txQ.refetch();
          void healthQ.refetch();
        }}
        actions={
          tx && (
            <div className="flex items-center gap-1.5">
              <span className="text-[11px] text-muted-foreground">Source</span>
              <StatusChip label={tx.source ?? "RDS"} tone={sourceTone(tx.source)} />
              <StatusChip
                label={`via ${tx.fetch_source ?? "SIM"}`}
                tone={sourceTone(tx.fetch_source)}
              />
            </div>
          )
        }
      />

      {/* RC search */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border bg-card px-4 py-3">
        <div className="relative min-w-0 flex-1 sm:max-w-md">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <input
            value={rcInput}
            onChange={(e) => setRcInput(e.target.value.toUpperCase())}
            onKeyDown={(e) => e.key === "Enter" && run()}
            placeholder="RC Number e.g. MH12AB1234"
            autoCapitalize="characters"
            spellCheck={false}
            className="h-9 w-full rounded-md border border-border bg-background pl-9 pr-3 text-[13px] outline-none focus:border-primary focus:ring-2 focus:ring-primary/20"
          />
        </div>
        <button
          onClick={run}
          disabled={!rcInput.trim()}
          className="inline-flex h-9 items-center gap-1.5 rounded-md bg-primary px-4 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        >
          <Search className="h-4 w-4" /> Search
        </button>
      </div>

      {/* Summary cards */}
      <div className="px-4 pt-3">
        <StatGrid className="lg:grid-cols-6">
          <StatCard
            icon={Wallet}
            label="Current Balance"
            value={b?.available_balance != null ? `₹${b.available_balance}` : "—"}
            tone="ok"
            loading={balanceQ.isLoading}
            sub={b?.tag_status ?? undefined}
          />
          <StatCard
            icon={CalendarClock}
            label="Today's Transactions"
            value={enabled ? todayCount : "—"}
            tone="info"
            loading={txQ.isLoading}
          />
          <StatCard
            icon={ArrowLeftRight}
            label="Total Transactions"
            value={enabled ? (tx?.stored_count ?? rows.length) : "—"}
            tone="info"
            loading={txQ.isLoading}
          />
          <StatCard
            icon={Nfc}
            label="Active FASTags"
            value={b ? (tagTone(b.tag_status) === "ok" ? 1 : 0) : "—"}
            tone="info"
            loading={balanceQ.isLoading}
          />
          <StatCard icon={Route} label="Toll Enroute" value="Plan" tone="neutral" />
          <StatCard
            icon={Activity}
            label="System Status"
            value={systemStatus}
            tone={sourceTone(systemStatus)}
            loading={healthQ.isLoading}
          />
        </StatGrid>
      </div>

      {/* Tabs */}
      <div className="px-4 py-3">
        <SegmentedTabs
          value={tab}
          onChange={setTab}
          className="mb-3"
          tabs={[
            { key: "balance", label: "Balance", icon: Wallet },
            {
              key: "transactions",
              label: "Transactions",
              icon: ArrowLeftRight,
              count: enabled ? rows.length : undefined,
            },
            { key: "journey", label: "Journey", icon: Route },
            { key: "enroute", label: "Toll Enroute", icon: MapPin },
            { key: "history", label: "History", icon: CalendarClock },
            { key: "health", label: "Health", icon: Activity },
          ]}
        />

        {tab === "balance" && <BalanceView rc={rc} balanceQ={balanceQ} />}
        {tab === "transactions" && <TransactionsView rc={rc} rows={rows} status={txQ} />}
        {tab === "journey" && <JourneyView rc={rc} rows={rows} status={txQ} />}
        {tab === "enroute" && <EnrouteView />}
        {tab === "history" && <TransactionsView rc={rc} rows={rows} status={txQ} history />}
        {tab === "health" && <HealthView healthQ={healthQ} />}
      </div>
    </PageContainer>
  );
}

function KV({ k, v }: { k: string; v?: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-3 border-b border-border/40 py-1.5 text-[13px]">
      <span className="text-muted-foreground">{k}</span>
      <span className="truncate text-right font-medium">{v ?? "—"}</span>
    </div>
  );
}

function BalanceView({ rc, balanceQ }: { rc: string; balanceQ: any }) {
  if (!rc)
    return (
      <Card className="p-0">
        <EmptyState>Enter an RC number to see the FASTag balance.</EmptyState>
      </Card>
    );
  if (balanceQ.isLoading)
    return (
      <Card className="p-0">
        <LoadingState label="Looking up FASTag balance…" />
      </Card>
    );
  if (balanceQ.isError || !balanceQ.data)
    return (
      <Card className="p-4">
        <div className="rounded-md border border-severity-critical/40 bg-severity-critical/10 px-3 py-2 text-xs">
          {friendlyError(balanceQ.error)}
        </div>
      </Card>
    );
  const b = balanceQ.data;
  return (
    <Card className="p-0">
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <h3 className="text-sm font-semibold">FASTag Balance · {b.rc_number ?? rc}</h3>
        <StatusChip label={b.tag_status ?? "—"} tone={tagTone(b.tag_status)} />
      </div>
      <div className="grid gap-x-8 p-4 sm:grid-cols-2">
        <KV k="Customer Name" v={b.customer_name} />
        <KV k="Provider" v={b.provider_name} />
        <KV
          k="Available Balance"
          v={b.available_balance != null ? `₹ ${b.available_balance}` : "—"}
        />
        <KV
          k="Recharge Limit"
          v={b.available_recharge_limit != null ? `₹ ${b.available_recharge_limit}` : "—"}
        />
        <KV k="Vehicle Class" v={b.vehicle_class_desc ?? b.vehicle_class} />
        <KV k="Model" v={b.model_name} />
        <KV k="Provider Code" v={b.provider_code} />
        <KV k="Tag Status" v={b.tag_status} />
      </div>
    </Card>
  );
}

const TXN_COLUMNS: Column<FastagTransactionRow & { __k?: number }>[] = [
  { key: "bank", header: "Bank", render: (r) => r.bank_name ?? "—" },
  {
    key: "status",
    header: "Status",
    render: (r) => <StatusChip label={r.status ?? "—"} tone={tagTone(r.status)} />,
  },
  { key: "seq", header: "Seq No.", className: "font-mono", render: (r) => r.seq_no ?? "—" },
  {
    key: "dt",
    header: "Date & Time",
    className: "whitespace-nowrap",
    render: (r) => (r.transaction_date_time ? fmtDateTimeIST(r.transaction_date_time) : "—"),
  },
  { key: "plaza", header: "Toll Plaza", render: (r) => r.toll_plaza_name ?? "—" },
  { key: "vt", header: "Vehicle", render: (r) => r.vehicle_type ?? "—" },
  { key: "lane", header: "Lane", render: (r) => r.lane_direction ?? "—" },
];

function TransactionsView({
  rc,
  rows,
  status,
  history,
}: {
  rc: string;
  rows: FastagTransactionRow[];
  status: any;
  history?: boolean;
}) {
  if (!rc)
    return (
      <Card className="p-0">
        <EmptyState>
          Enter an RC number to see {history ? "stored history" : "toll transactions"}.
        </EmptyState>
      </Card>
    );
  const keyed = rows.map((r, i) => ({ ...r, __k: i }));
  return (
    <Card className="overflow-hidden">
      <DataTable
        columns={TXN_COLUMNS}
        rows={keyed}
        rowKey={(r) => String(r.__k)}
        status={status}
        onRetry={() => status.refetch?.()}
        emptyLabel={`No ${history ? "stored " : ""}transactions for this RC.`}
        search={(r, q) =>
          `${r.bank_name ?? ""} ${r.toll_plaza_name ?? ""} ${r.status ?? ""} ${r.seq_no ?? ""}`
            .toLowerCase()
            .includes(q)
        }
        searchPlaceholder="Search transactions…"
        pageSize={10}
      />
    </Card>
  );
}

function JourneyView({
  rc,
  rows,
  status,
}: {
  rc: string;
  rows: FastagTransactionRow[];
  status: any;
}) {
  if (!rc)
    return (
      <Card className="p-0">
        <EmptyState>Enter an RC number to see the toll journey.</EmptyState>
      </Card>
    );
  if (status.isLoading)
    return (
      <Card className="p-0">
        <LoadingState />
      </Card>
    );
  const journey = [...rows]
    .filter((r) => r.transaction_date_time)
    .sort((a, b) => Date.parse(b.transaction_date_time!) - Date.parse(a.transaction_date_time!))
    .slice(0, 50);
  if (journey.length === 0)
    return (
      <Card className="p-0">
        <EmptyState>No toll crossings on record for this RC.</EmptyState>
      </Card>
    );
  return (
    <Card className="overflow-hidden">
      <div className="border-b border-border px-4 py-2.5">
        <h3 className="text-sm font-semibold">Toll Journey · {rc}</h3>
      </div>
      <ol className="relative space-y-0 p-4 pl-6">
        <span className="absolute left-[13px] top-4 bottom-4 w-px bg-border" aria-hidden />
        {journey.map((r, i) => (
          <li key={i} className="relative flex gap-3 pb-4 last:pb-0">
            <span className="absolute -left-[11px] mt-1 flex h-4 w-4 items-center justify-center rounded-full bg-primary ring-4 ring-card">
              <ArrowRightLeft className="h-2.5 w-2.5 text-white" />
            </span>
            <div className="ml-4 flex flex-1 flex-wrap items-center gap-x-2 gap-y-0.5">
              <MapPin className="h-3.5 w-3.5 text-muted-foreground" />
              <span className="text-[13px] font-medium text-foreground">
                {r.toll_plaza_name ?? "Toll plaza"}
              </span>
              {r.lane_direction && <StatusChip label={r.lane_direction} tone="info" />}
              {r.status && <span className="text-[11px] text-muted-foreground">· {r.status}</span>}
              <span
                className="ml-auto text-[11px] text-muted-foreground"
                title={fmtDateTimeIST(r.transaction_date_time)}
              >
                {relativeAge(r.transaction_date_time)}
              </span>
            </div>
          </li>
        ))}
      </ol>
    </Card>
  );
}

function HealthView({ healthQ }: { healthQ: any }) {
  if (healthQ.isLoading)
    return (
      <Card className="p-0">
        <LoadingState />
      </Card>
    );
  const h = healthQ.data;
  const err = healthQ.isError || !h;
  const tables = h?.tables ?? {};
  return (
    <Card className="p-0">
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <h3 className="text-sm font-semibold">FASTag ULIP Module Health</h3>
        <StatusChip
          label={err ? "DOWN" : h.status === "ok" ? "OK" : "DEGRADED"}
          tone={err ? "critical" : h.status === "ok" ? "ok" : "warn"}
        />
      </div>
      <div className="grid gap-x-8 p-4 sm:grid-cols-2">
        <KV k="Module" v={h?.module} />
        <KV k="Status" v={h?.status} />
        <KV k="ULIP Vendor" v={h?.ulip_configured ? "configured" : "not configured"} />
        <KV k="Database" v={h?.db} />
        <KV
          k="Tables healthy"
          v={
            Object.keys(tables).length
              ? `${Object.values(tables).filter(Boolean).length}/${Object.keys(tables).length}`
              : "—"
          }
        />
      </div>
      {Object.keys(tables).length > 0 && (
        <div className="flex flex-wrap gap-1.5 border-t border-border px-4 py-3">
          {Object.entries(tables).map(([name, ok]) => (
            <StatusChip key={name} label={name} tone={ok ? "ok" : "critical"} />
          ))}
        </div>
      )}
    </Card>
  );
}

// --- Toll Enroute (unchanged flow, reskinned) --------------------------------

function EnrouteView() {
  const [form, setForm] = useState<TollEnrouteInput>({
    source_state: "",
    source_name: "",
    destination_state: "",
    destination_name: "",
    vehicle_type: "TRUCK",
  });
  const m = useMutation<TollEnroute, Error, TollEnrouteInput>({
    mutationFn: (body) => getAdapter().tollEnroute(body),
  });
  const r = m.data;
  const set = (k: keyof TollEnrouteInput) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));
  const canSubmit =
    form.source_state.trim() &&
    form.source_name.trim() &&
    form.destination_state.trim() &&
    form.destination_name.trim() &&
    form.vehicle_type;
  const inputCls =
    "h-9 w-full rounded-md border border-border bg-background px-3 text-[13px] outline-none focus:border-primary focus:ring-2 focus:ring-primary/20";

  return (
    <div className="space-y-3">
      <Card className="p-4">
        <form
          className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (canSubmit) m.mutate(form);
          }}
        >
          <Field label="Source State">
            <input
              className={inputCls}
              placeholder="uttar_pradesh"
              value={form.source_state}
              onChange={set("source_state")}
            />
          </Field>
          <Field label="Source Name">
            <input
              className={inputCls}
              placeholder="ghazipur"
              value={form.source_name}
              onChange={set("source_name")}
            />
          </Field>
          <Field label="Vehicle Type">
            <select
              className={inputCls}
              value={form.vehicle_type}
              onChange={(e) => setForm((f) => ({ ...f, vehicle_type: e.target.value }))}
            >
              {VEHICLE_TYPES.map((vt) => (
                <option key={vt} value={vt}>
                  {vt}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Destination State">
            <input
              className={inputCls}
              placeholder="bihar"
              value={form.destination_state}
              onChange={set("destination_state")}
            />
          </Field>
          <Field label="Destination Name">
            <input
              className={inputCls}
              placeholder="patna"
              value={form.destination_name}
              onChange={set("destination_name")}
            />
          </Field>
          <div className="flex items-end">
            <button
              type="submit"
              disabled={m.isPending || !canSubmit}
              className="inline-flex h-9 items-center gap-1.5 rounded-md bg-primary px-4 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              <Route className="h-4 w-4" /> {m.isPending ? "Computing…" : "Find Toll Route"}
            </button>
          </div>
        </form>
      </Card>

      {m.isError && (
        <Card className="p-4">
          <div className="rounded-md border border-severity-critical/40 bg-severity-critical/10 px-3 py-2 text-xs">
            {friendlyError(m.error)}
          </div>
        </Card>
      )}
      {r && (
        <>
          <StatGrid className="lg:grid-cols-4">
            <StatCard
              icon={Route}
              label="Distance"
              value={r.distance ? `${r.distance} km` : "—"}
              tone="info"
            />
            <StatCard icon={CalendarClock} label="Duration" value={r.duration ?? "—"} tone="info" />
            <StatCard icon={MapPin} label="Toll Plazas" value={r.plaza_count} tone="warn" />
            <StatCard
              icon={ArrowRightLeft}
              label="Route"
              value={`${r.source ?? "—"} → ${r.destination ?? "—"}`}
              tone="neutral"
            />
          </StatGrid>
          <Card className="overflow-hidden">
            <DataTable
              columns={[
                { key: "name", header: "Toll Plaza", render: (p: any) => p.name ?? "—" },
                {
                  key: "cost",
                  header: "Cost",
                  align: "right",
                  className: "tabular-nums",
                  render: (p: any) => (p.cost != null ? `₹ ${p.cost}` : "—"),
                },
                {
                  key: "lat",
                  header: "Latitude",
                  className: "font-mono",
                  render: (p: any) => p.lat ?? "—",
                },
                {
                  key: "lng",
                  header: "Longitude",
                  className: "font-mono",
                  render: (p: any) => p.lng ?? "—",
                },
              ]}
              rows={r.toll_plaza_details.map((p, i) => ({ ...p, __k: i }))}
              rowKey={(p: any) => String(p.__k)}
              emptyLabel="No toll plazas on this route."
              pageSize={10}
            />
          </Card>
        </>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="space-y-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}
