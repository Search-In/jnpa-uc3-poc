// Shipping Lines — UC-II module 4 (additive, read-only).
//
// Import/Export Advance List (IAL/EAL) + Electronic Delivery Order (EDO) console
// over /api/shipping-lines. Composes the DTCCC kit (PageHeader / StatGrid /
// SegmentedTabs / FilterSelect / StatusChip) exactly like CfsEcyMovements, and
// calls the typed `api.shippingLines*` helpers. The list is FULLY SERVER-DRIVEN:
// container/BL/line search, list-type, terminal, category and freight-kind filters
// and pagination are resolved by the backend, so search/filter span the ENTIRE
// dataset — never just a loaded page. Row click opens a container drawer that
// surfaces the advance-list facts, EDO delivery orders and (soft link) the cargo
// lifecycle via the /api/cargo/{cn}/shipping-line enrichment endpoint.

import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery, keepPreviousData } from "@tanstack/react-query";
import {
  Ship,
  Boxes,
  Container,
  FileStack,
  Anchor,
  Ticket,
  Search,
  ChevronLeft,
  ChevronRight,
  ChevronsUpDown,
  ArrowDownToLine,
  ArrowUpFromLine,
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
import ShippingUploadPanel from "@/screens/shipping/UploadPanel";

// Data Upload is a WRITE surface — show it only to control-room / customs / admin
// (the gateway enforces the same policy on /api/shipping-lines).
const UPLOAD_ROLES = ["JNPA_TRAFFIC", "DTCCC_ADMIN", "TERMINAL_OPS", "CUSTOMS"];
const CAN_UPLOAD = !authEnabled() || UPLOAD_ROLES.includes(getRole() ?? "");
type TopTab = "browse" | "upload";

type ListTab = "all" | "IAL" | "EAL";
type SortKey = "container_no" | "shipping_line_code" | "terminal" | "gross_weight_kg" | "pod";

const PAGE_SIZE = 15;

interface Row {
  id: number;
  list_type: string;
  terminal: string;
  container_no: string;
  iso_code: string | null;
  container_valid_iso: boolean;
  freight_kind: string;
  category: string;
  gross_weight_kg: number | null;
  weight_source_uom: string | null;
  pol: string | null;
  pod: string | null;
  shipping_line_code: string | null;
  vessel_visit: string | null;
  bill_of_lading: string | null;
}

const listTone = (t: string): Tone => (t === "IAL" ? "info" : t === "EAL" ? "warn" : "neutral");
const catTone = (c: string): Tone =>
  c === "IMPORT" ? "info" : c === "EXPORT" ? "warn" : c === "TRANSHIP" ? "ok" : "neutral";
const fkTone = (f: string): Tone => (f === "FULL" ? "ok" : f === "EMPTY" ? "neutral" : "critical");

function useDebounced<T>(value: T, delay = 350): T {
  const [d, setD] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setD(value), delay);
    return () => clearTimeout(id);
  }, [value, delay]);
  return d;
}

function fmtWeight(kg?: number | null): string {
  if (kg === null || kg === undefined) return "—";
  return kg >= 1000 ? `${(kg / 1000).toFixed(2)} t` : `${kg} kg`;
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

export default function ShippingLines() {
  const [sp, setSp] = useSearchParams();
  const [topTab, setTopTab] = useState<TopTab>("browse");
  const [tab, setTab] = useState<ListTab>("all");
  const [terminal, setTerminal] = useState("");
  const [category, setCategory] = useState("");
  const [freightKind, setFreightKind] = useState("");
  const [searchInput, setSearchInput] = useState(sp.get("q") ?? sp.get("container") ?? "");
  const search = useDebounced(searchInput, 350);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  // Page-scoped column sort (the loaded page). Filters/search still span the whole dataset.
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({
    key: "container_no",
    dir: "asc",
  });

  // Deep-link support: a shipping_line code passed via ?line= filters the list.
  const lineParam = sp.get("line") || undefined;

  const listTypeParam = tab === "all" ? undefined : tab;
  const containerParam = search.trim() || undefined;

  useEffect(() => {
    setOffset(0);
  }, [tab, terminal, category, freightKind, search, lineParam]);

  const summaryQ = useQuery({
    queryKey: ["sl-summary"],
    queryFn: () => api.shippingLinesSummary(),
    placeholderData: keepPreviousData,
  });

  const listQ = useQuery({
    queryKey: [
      "sl-list",
      listTypeParam,
      terminal,
      category,
      freightKind,
      lineParam,
      containerParam,
      offset,
    ],
    queryFn: () =>
      api.shippingLinesList({
        list_type: listTypeParam,
        terminal: terminal || undefined,
        category: category || undefined,
        freight_kind: freightKind || undefined,
        shipping_line: lineParam,
        q: containerParam,
        limit: PAGE_SIZE,
        offset,
      }),
    placeholderData: keepPreviousData,
  });

  const rowsRaw: Row[] = listQ.data?.items ?? [];
  const rows = useMemo(() => {
    const arr = [...rowsRaw];
    arr.sort((a, b) => {
      const av = a[sort.key];
      const bv = b[sort.key];
      if (av === null || av === undefined) return 1;
      if (bv === null || bv === undefined) return -1;
      const cmp =
        typeof av === "number" && typeof bv === "number"
          ? av - bv
          : String(av).localeCompare(String(bv));
      return sort.dir === "asc" ? cmp : -cmp;
    });
    return arr;
  }, [rowsRaw, sort]);

  const total = listQ.data?.total ?? 0;
  const totals = summaryQ.data?.totals;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const page = Math.floor(offset / PAGE_SIZE);
  const showingFrom = total ? offset + 1 : 0;
  const showingTo = offset + rows.length;

  const filtersActive = !!(terminal || category || freightKind || searchInput || lineParam);
  const resetFilters = () => {
    setTerminal("");
    setCategory("");
    setFreightKind("");
    setSearchInput("");
    if (lineParam) {
      sp.delete("line");
      setSp(sp, { replace: true });
    }
  };

  const toggleSort = (key: SortKey) =>
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));

  return (
    <PageContainer>
      <PageHeader
        icon={Ship}
        title="Shipping Lines"
        subtitle="Import / Export Advance Lists (IAL · EAL) & Electronic Delivery Orders (EDO) — module 4"
        updatedAt={summaryQ.dataUpdatedAt}
        isFetching={summaryQ.isFetching || listQ.isFetching}
        onRefresh={() => {
          summaryQ.refetch();
          listQ.refetch();
        }}
      />

      {/* Top-level tabs: Browse the imported data vs upload new files (module 4 sub-module). */}
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
          <ShippingUploadPanel />
        </div>
      )}

      <div className={`flex flex-col gap-4 p-4 ${topTab === "browse" ? "" : "hidden"}`}>
        <StatGrid>
          <StatCard
            icon={Container}
            label="Advance-list containers"
            value={totals?.advance_containers?.toLocaleString() ?? "—"}
            tone="info"
            loading={summaryQ.isLoading}
          />
          <StatCard
            icon={Boxes}
            label="Distinct containers"
            value={totals?.distinct_containers?.toLocaleString() ?? "—"}
            tone="neutral"
            loading={summaryQ.isLoading}
          />
          <StatCard
            icon={Anchor}
            label="Shipping lines"
            value={totals?.shipping_lines ?? "—"}
            tone="ok"
            loading={summaryQ.isLoading}
          />
          <StatCard
            icon={Ticket}
            label="Delivery orders (EDO)"
            value={totals?.delivery_orders ?? "—"}
            tone="warn"
            loading={summaryQ.isLoading}
          />
          <StatCard
            icon={FileStack}
            label="With Bill of Lading"
            value={totals?.with_bl?.toLocaleString() ?? "—"}
            tone="neutral"
            loading={summaryQ.isLoading}
          />
          <StatCard
            icon={FileStack}
            label="Import files"
            value={totals?.files ?? "—"}
            tone="info"
            sub={totals?.failed_files ? `${totals.failed_files} failed` : "all clean"}
            loading={summaryQ.isLoading}
          />
        </StatGrid>

        {/* Active shipping-line deep-link chip */}
        {lineParam && (
          <div className="flex items-center gap-2 text-[13px]">
            <span className="text-muted-foreground">Filtered by shipping line:</span>
            <StatusChip label={lineParam} tone="ok" />
            <button
              type="button"
              onClick={resetFilters}
              className="text-[12px] text-primary underline-offset-2 hover:underline"
            >
              clear
            </button>
          </div>
        )}

        <SegmentedTabs<ListTab>
          tabs={[
            { key: "all", label: "All", count: totals?.advance_containers },
            { key: "IAL", label: "Import (IAL)" },
            { key: "EAL", label: "Export (EAL)" },
          ]}
          value={tab}
          onChange={setTab}
        />

        <Card className="p-0">
          {/* Toolbar: server-side search + filters + reset */}
          <div className="flex flex-wrap items-center gap-2 border-b border-border p-3">
            <div className="relative w-full max-w-xs">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <input
                type="search"
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                placeholder="Search container / BL / line… (all records)"
                className="h-9 w-full rounded-md border border-border bg-background py-1.5 pl-8 pr-3 text-[13px] outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary/20"
              />
            </div>
            <FilterSelect
              label="Terminal"
              value={terminal}
              onChange={setTerminal}
              options={[
                { value: "", label: "All terminals" },
                ...["APMT", "BMCT", "GTI", "NSFT", "NSICT", "NSIGT"].map((t) => ({
                  value: t,
                  label: t,
                })),
              ]}
            />
            <FilterSelect
              label="Category"
              value={category}
              onChange={setCategory}
              options={[
                { value: "", label: "All categories" },
                { value: "IMPORT", label: "Import" },
                { value: "EXPORT", label: "Export" },
                { value: "TRANSHIP", label: "Transhipment" },
              ]}
            />
            <FilterSelect
              label="Freight"
              value={freightKind}
              onChange={setFreightKind}
              options={[
                { value: "", label: "Full & Empty" },
                { value: "FULL", label: "Full (FCL)" },
                { value: "EMPTY", label: "Empty (MTY)" },
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
              <div className="text-sm font-medium">No containers found</div>
              <div className="max-w-xs text-[12px] text-muted-foreground">
                No advance-list rows match the current filters. Try clearing the search or filters.
              </div>
            </div>
          ) : (
            <>
              <div
                className={`overflow-x-auto transition-opacity ${listQ.isFetching ? "opacity-60" : ""}`}
              >
                <table className="w-full min-w-[840px] text-left text-[13px]">
                  <thead className="bg-muted/60 text-[11px] uppercase tracking-wide text-muted-foreground">
                    <tr>
                      <SortTh label="Container No." k="container_no" sort={sort} onSort={toggleSort} />
                      <th className="px-3 py-2 font-semibold">List</th>
                      <SortTh label="Terminal" k="terminal" sort={sort} onSort={toggleSort} />
                      <SortTh label="Line" k="shipping_line_code" sort={sort} onSort={toggleSort} />
                      <th className="px-3 py-2 font-semibold">Category</th>
                      <th className="px-3 py-2 font-semibold">Freight</th>
                      <SortTh label="Gross Wt." k="gross_weight_kg" sort={sort} onSort={toggleSort} />
                      <SortTh label="POD" k="pod" sort={sort} onSort={toggleSort} />
                      <th className="px-3 py-2 font-semibold">BL</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {rows.map((r) => (
                      <tr
                        key={r.id}
                        onClick={() => setSelected(r.container_no)}
                        className="cursor-pointer hover:bg-muted/40"
                      >
                        <td className="px-3 py-2 font-mono font-semibold text-foreground">
                          {r.container_no}
                          {!r.container_valid_iso && (
                            <span className="ml-1.5 align-middle text-[10px] font-normal text-destructive">
                              (ISO?)
                            </span>
                          )}
                        </td>
                        <td className="px-3 py-2">
                          <StatusChip
                            label={
                              <span className="inline-flex items-center gap-1">
                                {r.list_type === "IAL" ? (
                                  <ArrowDownToLine className="h-3 w-3" />
                                ) : (
                                  <ArrowUpFromLine className="h-3 w-3" />
                                )}
                                {r.list_type}
                              </span>
                            }
                            tone={listTone(r.list_type)}
                          />
                        </td>
                        <td className="px-3 py-2">{r.terminal}</td>
                        <td className="px-3 py-2 font-medium">{r.shipping_line_code ?? "—"}</td>
                        <td className="px-3 py-2">
                          <StatusChip label={r.category} tone={catTone(r.category)} />
                        </td>
                        <td className="px-3 py-2">
                          <StatusChip label={r.freight_kind} tone={fkTone(r.freight_kind)} />
                        </td>
                        <td className="px-3 py-2 tabular-nums">{fmtWeight(r.gross_weight_kg)}</td>
                        <td className="px-3 py-2">{r.pod ?? "—"}</td>
                        <td className="px-3 py-2 font-mono text-[12px]">
                          {r.bill_of_lading ?? "—"}
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
                    {showingFrom}–{showingTo}
                  </span>{" "}
                  of <span className="font-semibold text-foreground">{total.toLocaleString()}</span>{" "}
                  containers
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

      <ContainerDrawer
        containerNo={selected}
        onClose={() => setSelected(null)}
        onPickLine={(code) => {
          setSelected(null);
          sp.set("line", code);
          setSp(sp, { replace: true });
        }}
      />
    </PageContainer>
  );
}

function SortTh({
  label,
  k,
  sort,
  onSort,
}: {
  label: string;
  k: SortKey;
  sort: { key: SortKey; dir: "asc" | "desc" };
  onSort: (k: SortKey) => void;
}) {
  const active = sort.key === k;
  return (
    <th className="px-3 py-2 font-semibold">
      <button
        type="button"
        onClick={() => onSort(k)}
        className={`inline-flex items-center gap-1 hover:text-foreground ${active ? "text-foreground" : ""}`}
      >
        {label}
        <ChevronsUpDown className="h-3 w-3 opacity-60" />
        {active && <span className="text-[9px]">{sort.dir === "asc" ? "▲" : "▼"}</span>}
      </button>
    </th>
  );
}

// --- Container detail drawer ----------------------------------------------------
function ContainerDrawer({
  containerNo,
  onClose,
  onPickLine,
}: {
  containerNo: string | null;
  onClose: () => void;
  onPickLine: (code: string) => void;
}) {
  const q = useQuery({
    queryKey: ["sl-container", containerNo],
    queryFn: () => api.shippingLinesContainer(containerNo as string),
    enabled: !!containerNo,
  });
  // Soft link to jnpa.cargo via the enrichment endpoint (200 = in cargo lifecycle).
  const cargoQ = useQuery({
    queryKey: ["sl-cargo-link", containerNo],
    queryFn: () => api.cargoShippingLine(containerNo as string),
    enabled: !!containerNo,
    retry: false,
  });

  const s = q.data?.summary;
  const advance = q.data?.advance_lists ?? [];
  const orders = q.data?.delivery_orders ?? [];
  const inCargo = cargoQ.isSuccess;

  return (
    <Dialog open={!!containerNo} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="font-mono">{containerNo}</DialogTitle>
        </DialogHeader>

        {q.isLoading ? (
          <div className="py-8">
            <LoadingState />
          </div>
        ) : q.isError ? (
          <div className="py-8 text-center text-sm text-destructive">{friendlyError(q.error)}</div>
        ) : (
          <div className="flex flex-col gap-4">
            {/* Summary chips */}
            <div className="flex flex-wrap gap-2">
              {s?.shipping_line_code && (
                <button type="button" onClick={() => onPickLine(s.shipping_line_code)}>
                  <StatusChip label={`Line: ${s.shipping_line_code}`} tone="ok" />
                </button>
              )}
              {s?.list_type && <StatusChip label={s.list_type} tone={listTone(s.list_type)} />}
              {s?.category && <StatusChip label={s.category} tone={catTone(s.category)} />}
              {s?.freight_kind && <StatusChip label={s.freight_kind} tone={fkTone(s.freight_kind)} />}
              {s?.terminal && <StatusChip label={`Terminal ${s.terminal}`} tone="info" />}
              <StatusChip
                label={inCargo ? "Linked to Cargo lifecycle" : "Not in Cargo lifecycle"}
                tone={inCargo ? "ok" : "neutral"}
              />
            </div>

            {/* Key facts */}
            <Card className="p-3 text-[13px]">
              <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                Shipping-line facts
              </div>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-3">
                <Field label="ISO type" value={s?.iso_code} />
                <Field label="Gross weight" value={fmtWeight(s?.gross_weight_kg)} />
                <Field label="POL → POD" value={[s?.pol, s?.pod].filter(Boolean).join(" → ") || undefined} />
                <Field label="Vessel visit" value={s?.vessel_visit} />
                <Field label="Bill of Lading" value={s?.bill_of_lading} mono />
                <Field label="Reefer" value={s?.reefer_status} />
              </dl>
            </Card>

            {/* Advance-list rows (a box can appear on multiple lists / terminals) */}
            <div>
              <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                Advance-list records ({advance.length})
              </div>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[520px] text-left text-[12.5px]">
                  <thead className="bg-muted/60 text-[10.5px] uppercase tracking-wide text-muted-foreground">
                    <tr>
                      <th className="px-2.5 py-1.5 font-semibold">List</th>
                      <th className="px-2.5 py-1.5 font-semibold">Terminal</th>
                      <th className="px-2.5 py-1.5 font-semibold">Line</th>
                      <th className="px-2.5 py-1.5 font-semibold">Cat.</th>
                      <th className="px-2.5 py-1.5 font-semibold">Weight</th>
                      <th className="px-2.5 py-1.5 font-semibold">POD</th>
                      <th className="px-2.5 py-1.5 font-semibold">Seal</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {advance.map((a: any) => (
                      <tr key={a.id}>
                        <td className="px-2.5 py-1.5">
                          <StatusChip label={a.list_type} tone={listTone(a.list_type)} />
                        </td>
                        <td className="px-2.5 py-1.5">{a.terminal}</td>
                        <td className="px-2.5 py-1.5 font-medium">{a.shipping_line_code ?? "—"}</td>
                        <td className="px-2.5 py-1.5">{a.category}</td>
                        <td className="px-2.5 py-1.5 tabular-nums">{fmtWeight(a.gross_weight_kg)}</td>
                        <td className="px-2.5 py-1.5">{a.pod ?? "—"}</td>
                        <td className="px-2.5 py-1.5 font-mono text-[11px]">{a.seal_no ?? "—"}</td>
                      </tr>
                    ))}
                    {advance.length === 0 && (
                      <tr>
                        <td colSpan={7} className="px-2.5 py-3 text-muted-foreground">
                          No advance-list record.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>

            {/* EDO delivery orders */}
            {orders.length > 0 && (
              <div>
                <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                  Electronic Delivery Orders ({orders.length})
                </div>
                <ol className="flex flex-col gap-2">
                  {orders.map((o: any) => (
                    <li
                      key={o.id}
                      className="flex flex-wrap items-center gap-2 rounded-md border border-border px-3 py-2 text-[12.5px]"
                    >
                      <StatusChip label={`Gate pass ${o.gate_pass_no ?? "—"}`} tone="info" />
                      {o.vehicle_no && <StatusChip label={`Vehicle ${o.vehicle_no}`} tone="neutral" />}
                      {o.shipping_agent_code && (
                        <StatusChip label={`Agent ${o.shipping_agent_code}`} tone="ok" />
                      )}
                      {o.equipment_status && (
                        <span className="text-muted-foreground">{o.equipment_status}</span>
                      )}
                    </li>
                  ))}
                </ol>
              </div>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function Field({ label, value, mono }: { label: string; value?: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex flex-col">
      <dt className="text-[11px] text-muted-foreground">{label}</dt>
      <dd className={`font-medium text-foreground ${mono ? "font-mono text-[12px]" : ""}`}>
        {value ?? "—"}
      </dd>
    </div>
  );
}
