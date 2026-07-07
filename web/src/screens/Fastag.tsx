// FASTag (ULIP) console — Balance / Transactions / Toll Enroute.
// A single /fastag screen with in-page tabs, built entirely from existing UI
// primitives (Card / Button / Badge / Select / Spinner / EmptyState) so it reads
// as a native part of the dashboard. All data flows through the DataAdapter
// (getAdapter()), never fetch() directly — same pattern as every other screen.
import { useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Nfc, Search } from "lucide-react";
import { getAdapter } from "@/data";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Spinner, EmptyState } from "@/components/ui/misc";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn, fmtDateTimeIST } from "@/lib/utils";
import type {
  FastagBalance,
  FastagTransactionRow,
  FastagTransactions,
  TollEnroute,
} from "@/lib/types";

// Vehicle types the gateway accepts (gateway/routers/fastag.py VEHICLE_TYPES).
const VEHICLE_TYPES = ["CAR", "LMV", "LGV", "HGV", "TRUCK", "BUS", "MAV", "MMV", "2W", "3W"];

const inputCls =
  "h-8 w-full rounded-md border border-border bg-background px-3 text-xs focus:outline-none focus:ring-1 focus:ring-primary/40";

type TabKey = "balance" | "transactions" | "enroute";

const TABS: { key: TabKey; label: string }[] = [
  { key: "balance", label: "Balance" },
  { key: "transactions", label: "Transactions" },
  { key: "enroute", label: "Toll Enroute" },
];

export default function Fastag() {
  const [tab, setTab] = useState<TabKey>("balance");
  return (
    <div className="h-full overflow-y-auto p-4">
      <div className="mb-3 flex items-center gap-2">
        <Nfc className="h-5 w-5 text-primary" aria-hidden />
        <h1 className="text-lg font-semibold">FASTag</h1>
        <span className="text-xs text-muted-foreground">NETC · ULIP</span>
      </div>

      {/* Tab bar */}
      <div className="mb-4 inline-flex rounded-md border border-border p-0.5">
        {TABS.map((tb) => (
          <button
            key={tb.key}
            onClick={() => setTab(tb.key)}
            className={cn(
              "rounded-md px-3 py-1.5 text-sm transition-colors",
              tab === tb.key
                ? "bg-primary/15 text-primary"
                : "text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
          >
            {tb.label}
          </button>
        ))}
      </div>

      {tab === "balance" && <BalanceTab />}
      {tab === "transactions" && <TransactionsTab />}
      {tab === "enroute" && <EnrouteTab />}
    </div>
  );
}

// --- shared helpers ---------------------------------------------------------

/** Map a thrown adapter error to a friendly, actionable message. */
function friendlyError(e: unknown): string {
  const msg = e instanceof Error ? e.message : String(e);
  if (/\b400\b/.test(msg)) return "Invalid RC number format (e.g. MH12AB1234).";
  if (/\b504\b/.test(msg)) return "The FASTag provider timed out — please try again.";
  if (msg.includes('"config"'))
    return "FASTag provider is not configured yet. Set FASTAG_ULIP_URL on the gateway.";
  if (/\b502\b/.test(msg)) return "FASTag provider is currently unavailable — try again later.";
  if (/\b401\b/.test(msg) || /\b403\b/.test(msg))
    return "You don't have permission to view FASTag data.";
  if (/\b5\d\d\b/.test(msg)) return "FASTag service error — please try again.";
  return "Something went wrong — please try again.";
}

function ErrorNote({ error }: { error: unknown }) {
  return (
    <div className="rounded-md border border-severity-critical/40 bg-severity-critical/10 px-3 py-2 text-xs text-foreground">
      {friendlyError(error)}
    </div>
  );
}

function KV({ k, v }: { k: string; v?: React.ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{k}</dt>
      <dd className="text-right font-medium text-foreground">{v ?? "—"}</dd>
    </>
  );
}

function tagStatusColour(status?: string | null): string {
  const s = (status ?? "").toLowerCase();
  if (s.includes("activ")) return "#16a34a"; // green
  if (s.includes("low")) return "#d97706"; // amber
  if (s.includes("block") || s.includes("blacklist")) return "#dc2626"; // red
  return "#6b7280"; // grey
}

/** RC search bar reused by Balance + Transactions. */
function RcSearch({
  value,
  onChange,
  onSubmit,
  pending,
}: {
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  pending: boolean;
}) {
  return (
    <form
      className="flex items-end gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit();
      }}
    >
      <label className="flex-1 space-y-1">
        <span className="text-xs text-muted-foreground">RC Number</span>
        <input
          className={inputCls}
          placeholder="MH12AB1234"
          value={value}
          onChange={(e) => onChange(e.target.value.toUpperCase())}
          autoCapitalize="characters"
          spellCheck={false}
        />
      </label>
      <Button type="submit" size="sm" disabled={pending || !value.trim()}>
        {pending ? <Spinner className="text-primary-foreground" /> : <Search className="h-4 w-4" />}
        Search
      </Button>
    </form>
  );
}

// --- Balance tab ------------------------------------------------------------

function BalanceTab() {
  const [rc, setRc] = useState("");
  const m = useMutation<FastagBalance, Error, string>({
    mutationFn: (rcNumber) => getAdapter().fastagBalance(rcNumber),
  });
  const b = m.data;
  return (
    <div className="max-w-2xl space-y-4">
      <Card>
        <CardContent className="py-4">
          <RcSearch value={rc} onChange={setRc} onSubmit={() => m.mutate(rc.trim())} pending={m.isPending} />
        </CardContent>
      </Card>

      {m.isError && <ErrorNote error={m.error} />}

      {m.isPending && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> Looking up FASTag balance…
        </div>
      )}

      {!m.isPending && !m.isError && !b && (
        <EmptyState>Enter an RC number and search to see the FASTag balance.</EmptyState>
      )}

      {b && !m.isPending && (
        <Card>
          <CardHeader className="flex-row items-center justify-between">
            <CardTitle>FASTag Balance</CardTitle>
            <Badge colour={tagStatusColour(b.tag_status)}>{b.tag_status ?? "—"}</Badge>
          </CardHeader>
          <CardContent>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-sm">
              <KV k="RC Number" v={<span className="font-mono">{b.rc_number ?? "—"}</span>} />
              <KV k="Customer Name" v={b.customer_name} />
              <KV k="Provider" v={b.provider_name} />
              <KV k="Provider Code" v={<span className="font-mono">{b.provider_code ?? "—"}</span>} />
              <KV
                k="Available Balance"
                v={<span className="tabular-nums">{b.available_balance != null ? `₹ ${b.available_balance}` : "—"}</span>}
              />
              <KV
                k="Recharge Limit"
                v={<span className="tabular-nums">{b.available_recharge_limit != null ? `₹ ${b.available_recharge_limit}` : "—"}</span>}
              />
              <KV k="Vehicle Class" v={b.vehicle_class} />
              <KV k="Vehicle Class Description" v={b.vehicle_class_desc} />
              <KV k="Model Name" v={b.model_name} />
              <KV k="Tag Status" v={b.tag_status} />
            </dl>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

// --- Transactions tab -------------------------------------------------------

type TxnCol = keyof Pick<
  FastagTransactionRow,
  | "bank_name"
  | "status"
  | "seq_no"
  | "transaction_date_time"
  | "toll_plaza_name"
  | "vehicle_type"
  | "lane_direction"
>;

const TXN_COLS: { key: TxnCol; label: string }[] = [
  { key: "bank_name", label: "Bank Name" },
  { key: "status", label: "Status" },
  { key: "seq_no", label: "Sequence No." },
  { key: "transaction_date_time", label: "Date & Time" },
  { key: "toll_plaza_name", label: "Toll Plaza" },
  { key: "vehicle_type", label: "Vehicle Type" },
  { key: "lane_direction", label: "Lane" },
];

const PAGE_SIZE = 10;

function TransactionsTab() {
  const [rc, setRc] = useState("");
  const [sort, setSort] = useState<{ col: TxnCol; dir: "asc" | "desc" }>({
    col: "transaction_date_time",
    dir: "desc",
  });
  const [page, setPage] = useState(0);
  const m = useMutation<FastagTransactions, Error, string>({
    mutationFn: (rcNumber) => getAdapter().fastagTransactions(rcNumber),
    onSuccess: () => setPage(0),
  });
  const rows = m.data?.transactions ?? [];

  const sorted = useMemo(() => {
    const copy = [...rows];
    copy.sort((a, b) => {
      const av = (a[sort.col] ?? "").toString();
      const bv = (b[sort.col] ?? "").toString();
      const c = av.localeCompare(bv);
      return sort.dir === "asc" ? c : -c;
    });
    return copy;
  }, [rows, sort]);

  const pageCount = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const pageRows = sorted.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE);

  const toggleSort = (col: TxnCol) =>
    setSort((s) => (s.col === col ? { col, dir: s.dir === "asc" ? "desc" : "asc" } : { col, dir: "asc" }));

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="py-4">
          <div className="max-w-2xl">
            <RcSearch value={rc} onChange={setRc} onSubmit={() => m.mutate(rc.trim())} pending={m.isPending} />
          </div>
        </CardContent>
      </Card>

      {m.isError && <ErrorNote error={m.error} />}

      {m.isPending && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> Fetching transaction history…
        </div>
      )}

      {!m.isPending && !m.isError && !m.data && (
        <EmptyState>Enter an RC number and search to see toll transactions.</EmptyState>
      )}

      {m.data && !m.isPending && rows.length === 0 && (
        <EmptyState>No transactions found for this RC number.</EmptyState>
      )}

      {rows.length > 0 && !m.isPending && (
        <Card>
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs text-muted-foreground">
                    {TXN_COLS.map((c) => (
                      <th key={c.key} className="px-4 py-2">
                        <button
                          className="inline-flex items-center gap-1 hover:text-foreground"
                          onClick={() => toggleSort(c.key)}
                        >
                          {c.label}
                          {sort.col === c.key && <span aria-hidden>{sort.dir === "asc" ? "▲" : "▼"}</span>}
                        </button>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {pageRows.map((r, i) => (
                    <tr key={r.seq_no ?? i} className="border-b border-border/60 last:border-0">
                      <td className="px-4 py-2">{r.bank_name || "—"}</td>
                      <td className="px-4 py-2">
                        <Badge colour={tagStatusColour(r.status)}>{r.status ?? "—"}</Badge>
                      </td>
                      <td className="px-4 py-2 font-mono text-xs">{r.seq_no ?? "—"}</td>
                      <td className="px-4 py-2 whitespace-nowrap">
                        {r.transaction_date_time ? fmtDateTimeIST(r.transaction_date_time) : "—"}
                      </td>
                      <td className="px-4 py-2">{r.toll_plaza_name ?? "—"}</td>
                      <td className="px-4 py-2">{r.vehicle_type ?? "—"}</td>
                      <td className="px-4 py-2">{r.lane_direction ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Pagination */}
            <div className="flex items-center justify-between border-t border-border px-4 py-2 text-xs text-muted-foreground">
              <span>
                {sorted.length} transaction{sorted.length === 1 ? "" : "s"} · page {page + 1} of {pageCount}
              </span>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page === 0}
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                >
                  Prev
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page >= pageCount - 1}
                  onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
                >
                  Next
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

// --- Toll Enroute tab -------------------------------------------------------

function EnrouteTab() {
  const [form, setForm] = useState({
    source_state: "",
    source_name: "",
    destination_state: "",
    destination_name: "",
    vehicle_type: "TRUCK",
  });
  const m = useMutation<TollEnroute, Error, typeof form>({
    mutationFn: (body) => getAdapter().tollEnroute(body),
  });
  const r = m.data;
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));
  const canSubmit =
    form.source_state.trim() &&
    form.source_name.trim() &&
    form.destination_state.trim() &&
    form.destination_name.trim() &&
    form.vehicle_type;

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="py-4">
          <form
            className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3"
            onSubmit={(e) => {
              e.preventDefault();
              if (canSubmit) m.mutate(form);
            }}
          >
            <Field label="Source State">
              <input className={inputCls} placeholder="uttar_pradesh" value={form.source_state} onChange={set("source_state")} />
            </Field>
            <Field label="Source Name">
              <input className={inputCls} placeholder="ghazipur" value={form.source_name} onChange={set("source_name")} />
            </Field>
            <Field label="Vehicle Type">
              <Select value={form.vehicle_type} onValueChange={(v) => setForm((f) => ({ ...f, vehicle_type: v }))}>
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {VEHICLE_TYPES.map((vt) => (
                    <SelectItem key={vt} value={vt}>
                      {vt}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            <Field label="Destination State">
              <input className={inputCls} placeholder="bihar" value={form.destination_state} onChange={set("destination_state")} />
            </Field>
            <Field label="Destination Name">
              <input className={inputCls} placeholder="patna" value={form.destination_name} onChange={set("destination_name")} />
            </Field>
            <div className="flex items-end">
              <Button type="submit" size="sm" disabled={m.isPending || !canSubmit}>
                {m.isPending ? <Spinner className="text-primary-foreground" /> : <Search className="h-4 w-4" />}
                Find Toll Route
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      {m.isError && <ErrorNote error={m.error} />}

      {m.isPending && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> Computing toll route…
        </div>
      )}

      {!m.isPending && !m.isError && !r && (
        <EmptyState>Enter a source and destination to see the toll plazas enroute.</EmptyState>
      )}

      {r && !m.isPending && (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <StatCard label="Distance" value={r.distance ? `${r.distance} km` : "—"} />
            <StatCard label="Duration" value={r.duration ?? "—"} />
            <StatCard label="Toll Plazas" value={String(r.plaza_count)} />
            <StatCard label="Route" value={`${r.source ?? "—"} → ${r.destination ?? "—"}`} />
          </div>

          {r.toll_plaza_details.length === 0 ? (
            <EmptyState>No toll plazas on this route.</EmptyState>
          ) : (
            <Card>
              <CardContent className="p-0">
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border text-left text-xs text-muted-foreground">
                        <th className="px-4 py-2">Toll Plaza</th>
                        <th className="px-4 py-2">Toll Cost</th>
                        <th className="px-4 py-2">Latitude</th>
                        <th className="px-4 py-2">Longitude</th>
                      </tr>
                    </thead>
                    <tbody>
                      {r.toll_plaza_details.map((p, i) => (
                        <tr key={i} className="border-b border-border/60 last:border-0">
                          <td className="px-4 py-2">{p.name ?? "—"}</td>
                          <td className="px-4 py-2 tabular-nums">{p.cost != null ? `₹ ${p.cost}` : "—"}</td>
                          <td className="px-4 py-2 font-mono text-xs">{p.lat ?? "—"}</td>
                          <td className="px-4 py-2 font-mono text-xs">{p.lng ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </CardContent>
            </Card>
          )}
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

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <Card>
      <CardContent className="py-3">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className="mt-0.5 truncate text-sm font-semibold">{value}</div>
      </CardContent>
    </Card>
  );
}
