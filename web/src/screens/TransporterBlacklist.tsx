// Transport Master — enterprise console for the registered transport-company
// registry: company profiles, fleet ownership / vehicle mapping, live gate
// validation and the gate-enforcement blacklist. Search transporters, drill into
// a company to see its profile, mapped vehicles + drivers and blacklist card,
// add vehicles, blacklist / lift, and probe the ALLOW / DENY validation gate.
// Backed exclusively by the existing /api/transporters/* endpoints — this file is
// a pure UI redesign (no API, schema or business-rule changes).

import { useEffect, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Ban,
  Building2,
  ChevronLeft,
  ChevronRight,
  FileText,
  Hash,
  Inbox,
  Mail,
  MapPin,
  Phone,
  Plus,
  ScanLine,
  Search,
  ShieldAlert,
  ShieldCheck,
  Truck,
  UserRound,
  Users,
  type LucideIcon,
} from "lucide-react";
import { api } from "@/lib/api";
import {
  PageContainer,
  PageHeader,
  StatCard,
  StatGrid,
  StatusChip,
  useEmbedded,
  type Tone,
} from "@/components/ui/dtccc";
import { Card } from "@/components/ui/card";
import { LoadingState } from "@/components/ui/misc";
import { STATUS } from "@/lib/tokens";
import { fmtDateTimeIST } from "@/lib/utils";

const SEVERITIES = ["LOW", "MEDIUM", "HIGH", "CRITICAL"] as const;
const LIST_LIMIT = 1000; // server hard-caps at 1000; we page/search client-side over this window
const PAGE_SIZE = 10;

function severityTone(sev?: string): Tone {
  switch ((sev ?? "").toUpperCase()) {
    case "CRITICAL":
    case "HIGH":
      return "critical";
    case "MEDIUM":
      return "warn";
    case "LOW":
      return "info";
    default:
      return "neutral";
  }
}

const inputCls =
  "w-full rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary/20";

/* ---------- small utilities ---------- */

function useDebounced<T>(value: T, delay = 320): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(id);
  }, [value, delay]);
  return debounced;
}

/** Case-insensitive highlight of the matched query inside a string. */
function Highlight({ text, query }: { text?: string | null; query: string }) {
  const value = text == null || text === "" ? "—" : String(text);
  const q = query.trim();
  if (!q || value === "—") return <>{value}</>;
  const idx = value.toLowerCase().indexOf(q.toLowerCase());
  if (idx < 0) return <>{value}</>;
  return (
    <>
      {value.slice(0, idx)}
      <mark className="rounded bg-primary/20 px-0.5 text-foreground">
        {value.slice(idx, idx + q.length)}
      </mark>
      {value.slice(idx + q.length)}
    </>
  );
}

/* ==================== Screen ==================== */

export default function TransporterBlacklist({ mode = "master" }: { mode?: "master" | "blacklist" }) {
  const qc = useQueryClient();
  const embedded = useEmbedded();
  const isBlacklistMode = mode === "blacklist";
  const [q, setQ] = useState("");
  const dq = useDebounced(q, 320);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [page, setPage] = useState(0);
  // Deep-link target: "Open Transporter" from Driver Master navigates here with
  // ?transporterId=<id> — auto-select that transporter (master mode only).
  const [searchParams] = useSearchParams();
  useEffect(() => {
    if (isBlacklistMode) return;
    const tid = searchParams.get("transporterId");
    if (tid && /^\d+$/.test(tid)) setSelectedId(Number(tid));
  }, [searchParams, isBlacklistMode]);

  // Filtered, server-searched list (covers the full registry; display window 1000).
  const listQ = useQuery({
    queryKey: ["transporters-list", dq],
    queryFn: () => api.transporters({ q: dq.trim() || undefined, limit: LIST_LIMIT }),
  });
  const blacklistQ = useQuery({
    queryKey: ["transporter-blacklist"],
    queryFn: () => api.transporterBlacklist(),
  });
  const detailQ = useQuery({
    queryKey: ["transporter", selectedId],
    queryFn: () => api.transporter(selectedId as number),
    enabled: selectedId != null,
  });

  // Registry-wide KPI baseline: snapshot the UNFILTERED list result so the KPI
  // cards stay stable while the user searches — reuses the same query, no extra
  // API call is made.
  const registryRef = useRef<{ count: number; transporters: any[] } | null>(null);
  useEffect(() => {
    if (!dq.trim() && listQ.data) registryRef.current = listQ.data;
  }, [dq, listQ.data]);
  const registry = registryRef.current ?? (!dq.trim() ? listQ.data : undefined);

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["transporters-list"] });
    void qc.invalidateQueries({ queryKey: ["transporter-blacklist"] });
    if (selectedId != null) void qc.invalidateQueries({ queryKey: ["transporter", selectedId] });
  };

  const blacklist: any[] = blacklistQ.data?.blacklist ?? [];
  const masterRows: any[] = listQ.data?.transporters ?? [];
  // Blacklist tab: the list is driven ONLY by the /api/transporters/blacklist
  // endpoint (active blacklist records), search-filtered client-side over that
  // set. We never fetch all transporters and filter them down to build it.
  const blacklistRows: any[] = dq.trim()
    ? blacklist.filter((b) =>
        `${b.transporter_name ?? ""} ${b.transporter_code ?? ""} ${b.reason ?? ""} ${b.severity ?? ""}`
          .toLowerCase()
          .includes(dq.trim().toLowerCase()),
      )
    : blacklist;
  const rows: any[] = isBlacklistMode ? blacklistRows : masterRows;
  const listLoading = isBlacklistMode ? blacklistQ.isLoading : listQ.isLoading;
  const listFetching = isBlacklistMode ? blacklistQ.isFetching : listQ.isFetching;

  // KPIs from the registry snapshot (+ active-blacklist count, which is registry-wide).
  const regRows = registry?.transporters ?? [];
  const regTotal = registry?.count ?? 0;
  const capped = regTotal >= LIST_LIMIT;
  const blacklistedCount = blacklistQ.data?.count ?? 0;
  const activeCount = Math.max(0, regTotal - blacklistedCount);
  const vehiclesAssigned = regRows.reduce((s, r) => s + (Number(r.vehicle_count) || 0), 0);
  const plus = (n: number) => `${n.toLocaleString()}${capped ? "+" : ""}`;

  // Reset paging whenever the result set changes.
  useEffect(() => {
    setPage(0);
  }, [dq]);
  const pageCount = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const clampedPage = Math.min(page, pageCount - 1);
  const pageRows = rows.slice(clampedPage * PAGE_SIZE, clampedPage * PAGE_SIZE + PAGE_SIZE);
  const showingFrom = rows.length ? clampedPage * PAGE_SIZE + 1 : 0;
  const showingTo = clampedPage * PAGE_SIZE + pageRows.length;

  const HeaderIcon = isBlacklistMode ? ShieldAlert : Building2;
  const headerTitle = isBlacklistMode ? "Blacklist" : "Transport Master";
  const headerSubtitle = isBlacklistMode
    ? "Transporters currently denied at the gate — active blacklist only."
    : "Manage registered transport companies, fleet ownership, vehicle mapping and blacklist status.";

  return (
    <PageContainer>
      <PageHeader title={headerTitle} subtitle={headerSubtitle} icon={HeaderIcon} />

      <div className="space-y-4 px-4 py-4">
        {/* In-content title for embedded hosts (PageHeader is suppressed there). */}
        {embedded && (
          <div className="flex items-center gap-2.5">
            <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <HeaderIcon className="h-5 w-5" strokeWidth={2} />
            </span>
            <div>
              <h2 className="text-base font-bold leading-tight tracking-tight">{headerTitle}</h2>
              <p className="text-xs text-muted-foreground">{headerSubtitle}</p>
            </div>
          </div>
        )}

        {/* ---------------- KPI summary ---------------- */}
        <StatGrid>
          <StatCard
            icon={Building2}
            label="Total Transporters"
            value={plus(regTotal)}
            tone="info"
            loading={listQ.isLoading && !registry}
            sub={capped ? "registered (window 1000)" : "registered companies"}
          />
          <StatCard
            icon={ShieldCheck}
            label="Active"
            value={plus(activeCount)}
            tone="ok"
            loading={listQ.isLoading && !registry}
            sub="cleared for gate entry"
          />
          <StatCard
            icon={ShieldAlert}
            label="Blacklisted"
            value={blacklistedCount.toLocaleString()}
            tone="critical"
            loading={blacklistQ.isLoading}
            sub="denied at the gate"
          />
          <StatCard
            icon={Truck}
            label="Vehicles Assigned"
            value={plus(vehiclesAssigned)}
            tone="neutral"
            loading={listQ.isLoading && !registry}
            sub="mapped to transporters"
          />
        </StatGrid>

        {/* ---------------- Registry + detail ---------------- */}
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
          {/* Left: searchable registry */}
          <Card className="rounded-2xl p-0 xl:col-span-5">
            <div className="border-b border-border p-4">
              <div className="mb-3 flex items-center gap-2">
                {isBlacklistMode ? (
                  <ShieldAlert size={16} style={{ color: STATUS.critical }} />
                ) : (
                  <Users size={16} className="text-primary" />
                )}
                <h3 className="text-sm font-semibold">
                  {isBlacklistMode ? "Blacklisted transporters" : "Transporters"}
                </h3>
                <span className="ml-auto rounded-full bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
                  {isBlacklistMode ? blacklistedCount.toLocaleString() : plus(regTotal)}
                </span>
              </div>
              <div className="flex items-center gap-2 rounded-lg border border-border bg-background px-3 py-2 focus-within:border-primary focus-within:ring-2 focus-within:ring-primary/20">
                <Search size={15} className="shrink-0 text-muted-foreground" />
                <input
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder={
                    isBlacklistMode
                      ? "Search blacklisted company, code or reason"
                      : "Search by Company, Contact Person, Mobile, Email or Company ID"
                  }
                  className="w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground/70"
                />
                {q && (
                  <button
                    onClick={() => setQ("")}
                    className="shrink-0 text-[11px] font-medium text-muted-foreground hover:text-foreground"
                  >
                    Clear
                  </button>
                )}
              </div>
            </div>

            {listLoading ? (
              <div className="p-6">
                <LoadingState />
              </div>
            ) : !rows.length ? (
              isBlacklistMode ? (
                <DetailEmpty
                  icon={ShieldCheck}
                  title={q ? "No blacklisted transporters match" : "No active blacklisted transporters found."}
                  hint={q ? `Nothing matches “${q}”.` : "No transporters are currently denied at the gate."}
                />
              ) : (
                <DetailEmpty
                  icon={Inbox}
                  title="No transporters match"
                  hint={q ? `Nothing matches “${q}”. Try a company, contact, mobile or ID.` : "The registry is empty."}
                />
              )
            ) : (
              <>
                <div className={`overflow-x-auto transition-opacity ${listFetching ? "opacity-60" : ""}`}>
                  {isBlacklistMode ? (
                    <table className="w-full min-w-[440px] border-collapse text-[12.5px]">
                      <thead>
                        <tr className="border-b border-border text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                          <th className="px-3 py-2 font-medium">Company</th>
                          <th className="px-2.5 py-2 font-medium">Severity</th>
                          <th className="px-2.5 py-2 font-medium">Reason</th>
                          <th className="px-2.5 py-2 pr-3 font-medium">Since</th>
                        </tr>
                      </thead>
                      <tbody>
                        {pageRows.map((b, i) => {
                          const active = selectedId === b.transporter_id;
                          return (
                            <tr
                              key={`${b.transporter_id}-${i}`}
                              onClick={() => setSelectedId(b.transporter_id)}
                              className={`cursor-pointer border-b border-border/60 align-middle transition-colors hover:bg-muted/50 ${
                                active ? "bg-primary/5" : ""
                              }`}
                            >
                              <td className="px-3 py-2.5">
                                <div className="font-medium text-foreground">
                                  <Highlight text={b.transporter_name} query={dq} />
                                </div>
                                <div className="mt-0.5 flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
                                  <Hash size={9} />
                                  <Highlight text={b.transporter_code} query={dq} />
                                </div>
                              </td>
                              <td className="px-2.5 py-2.5">
                                <StatusChip label={b.severity || "—"} tone={severityTone(b.severity)} />
                              </td>
                              <td className="px-2.5 py-2.5 text-muted-foreground">
                                <Highlight text={b.reason} query={dq} />
                              </td>
                              <td className="px-2.5 py-2.5 pr-3 whitespace-nowrap text-muted-foreground">
                                {b.blacklisted_at ? fmtDateTimeIST(b.blacklisted_at) : "—"}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  ) : (
                    <table className="w-full min-w-[440px] border-collapse text-[12.5px]">
                      <thead>
                        <tr className="border-b border-border text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                          <th className="px-3 py-2 font-medium">Company</th>
                          <th className="px-2.5 py-2 font-medium">Contact Person</th>
                          <th className="px-2.5 py-2 font-medium">Mobile</th>
                          <th className="px-2.5 py-2 text-center font-medium">Veh.</th>
                          <th className="px-2.5 py-2 pr-3 font-medium">Status</th>
                        </tr>
                      </thead>
                      <tbody>
                        {pageRows.map((t) => {
                          const isBl = t.blacklisted || t.status === "BLACKLISTED";
                          const active = selectedId === t.id;
                          return (
                            <tr
                              key={t.id}
                              onClick={() => setSelectedId(t.id)}
                              className={`cursor-pointer border-b border-border/60 align-middle transition-colors hover:bg-muted/50 ${
                                active ? "bg-primary/5" : ""
                              }`}
                            >
                              <td className="px-3 py-2.5">
                                <div className="font-medium text-foreground">
                                  <Highlight text={t.name} query={dq} />
                                </div>
                                <div className="mt-0.5 flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
                                  <Hash size={9} />
                                  <Highlight text={t.source_company_id ?? t.code} query={dq} />
                                </div>
                              </td>
                              <td className="px-2.5 py-2.5 text-muted-foreground">
                                <Highlight text={t.contact_person} query={dq} />
                              </td>
                              <td className="px-2.5 py-2.5 font-mono text-[11.5px] text-muted-foreground">
                                <Highlight text={t.mobile} query={dq} />
                              </td>
                              <td className="px-2.5 py-2.5 text-center tabular-nums">
                                {t.vehicle_count ?? 0}
                              </td>
                              <td className="px-2.5 py-2.5 pr-3 whitespace-nowrap">
                                <StatusChip
                                  label={isBl ? "BLACKLISTED" : t.status || "ACTIVE"}
                                  tone={isBl ? "critical" : "ok"}
                                />
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  )}
                </div>

                {/* Pagination / count footer */}
                <div className="flex flex-wrap items-center gap-2 border-t border-border px-4 py-2.5 text-[11.5px] text-muted-foreground">
                  <span>
                    Showing <span className="font-semibold text-foreground">{showingFrom}–{showingTo}</span> of{" "}
                    <span className="font-semibold text-foreground">{rows.length.toLocaleString()}</span>
                    {isBlacklistMode
                      ? q
                        ? " matches"
                        : " blacklisted"
                      : rows.length >= LIST_LIMIT
                        ? "+ (refine search to narrow)"
                        : q
                          ? " matches"
                          : " transporters"}
                  </span>
                  <div className="ml-auto flex items-center gap-1">
                    <button
                      disabled={clampedPage <= 0}
                      onClick={() => setPage((p) => Math.max(0, p - 1))}
                      className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-border hover:bg-muted disabled:opacity-40"
                      aria-label="Previous page"
                    >
                      <ChevronLeft size={14} />
                    </button>
                    <span className="px-1 tabular-nums">
                      {clampedPage + 1} / {pageCount}
                    </span>
                    <button
                      disabled={clampedPage >= pageCount - 1}
                      onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
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

          {/* Right: transporter detail */}
          <div className="xl:col-span-7">
            {selectedId == null ? (
              <Card className="flex min-h-[320px] items-center justify-center rounded-2xl p-6">
                <DetailEmpty
                  icon={Building2}
                  title="No transporter selected"
                  hint="Select a company from the registry to view its profile, fleet and blacklist status."
                />
              </Card>
            ) : detailQ.isLoading ? (
              <Card className="min-h-[320px] rounded-2xl p-6">
                <LoadingState />
              </Card>
            ) : !detailQ.data ? (
              <Card className="flex min-h-[320px] items-center justify-center rounded-2xl p-6">
                <DetailEmpty icon={Inbox} title="Transporter not found" hint="It may have been removed." />
              </Card>
            ) : (
              <TransporterDetail detail={detailQ.data} onChanged={invalidate} />
            )}
          </div>
        </div>

        {/* Master-only tools + the global active-blacklist table. In blacklist
            mode the left list already IS the active blacklist, so these are
            hidden to avoid duplication (no functionality is removed — they remain
            on the Transport Master tab). */}
        {!isBlacklistMode && (
          <>
            {/* Utilities: validation + create */}
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              <SectionCard icon={ScanLine} title="Vehicle validation" hint="live gate-enforcement check">
                <VehicleValidation />
              </SectionCard>
              <SectionCard icon={Plus} title="Register transporter" hint="add a company to the master">
                <CreateTransporter onCreated={invalidate} />
              </SectionCard>
            </div>

            {/* Global active blacklist */}
            <SectionCard
              icon={ShieldAlert}
              title="Active blacklist"
              hint={`${blacklistedCount} currently denied`}
              tone="critical"
            >
              {blacklistQ.isLoading ? (
                <LoadingState />
              ) : !blacklist.length ? (
                <DetailEmpty
                  icon={ShieldCheck}
                  title="No active blacklist"
                  hint="No transporters are currently denied at the gate."
                />
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[620px] border-collapse text-[12.5px]">
                    <thead>
                      <tr className="border-b border-border text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                        <th className="py-2 pr-3 font-medium">Transporter</th>
                        <th className="py-2 pr-3 font-medium">Code</th>
                        <th className="py-2 pr-3 font-medium">Severity</th>
                        <th className="py-2 pr-3 font-medium">Reason</th>
                        <th className="py-2 pr-3 font-medium">Since</th>
                      </tr>
                    </thead>
                    <tbody>
                      {blacklist.map((b, i) => (
                        <tr
                          key={`${b.transporter_id}-${i}`}
                          onClick={() => setSelectedId(b.transporter_id)}
                          className="cursor-pointer border-b border-border/60 align-middle transition-colors hover:bg-muted/50"
                        >
                          <td className="py-2.5 pr-3 font-medium">{b.transporter_name}</td>
                          <td className="py-2.5 pr-3 font-mono text-[11px] text-muted-foreground">
                            {b.transporter_code || "—"}
                          </td>
                          <td className="py-2.5 pr-3">
                            <StatusChip label={b.severity} tone={severityTone(b.severity)} />
                          </td>
                          <td className="py-2.5 pr-3 text-muted-foreground">{b.reason || "—"}</td>
                          <td className="py-2.5 pr-3 whitespace-nowrap text-muted-foreground">
                            {b.blacklisted_at ? fmtDateTimeIST(b.blacklisted_at) : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </SectionCard>
          </>
        )}
      </div>
    </PageContainer>
  );
}

/* ==================== Reusable pieces ==================== */

function SectionCard({
  icon: Icon,
  title,
  hint,
  tone = "info",
  children,
}: {
  icon: LucideIcon;
  title: string;
  hint?: string;
  tone?: Tone;
  children: ReactNode;
}) {
  const colour = tone === "critical" ? STATUS.critical : STATUS.info;
  return (
    <Card className="rounded-2xl p-0">
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <span
          className="flex h-7 w-7 items-center justify-center rounded-lg"
          style={{ backgroundColor: `${colour}1a`, color: colour }}
        >
          <Icon size={15} />
        </span>
        <h3 className="text-sm font-semibold">{title}</h3>
        {hint && <span className="ml-auto text-[11px] text-muted-foreground">{hint}</span>}
      </div>
      <div className="p-4">{children}</div>
    </Card>
  );
}

function DetailEmpty({ icon: Icon, title, hint }: { icon: LucideIcon; title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-10 text-center">
      <span className="flex h-12 w-12 items-center justify-center rounded-full bg-muted text-muted-foreground">
        <Icon size={22} strokeWidth={1.75} />
      </span>
      <div className="text-sm font-medium text-foreground">{title}</div>
      {hint && <div className="max-w-xs text-[12px] text-muted-foreground">{hint}</div>}
    </div>
  );
}

function Field({ icon: Icon, label, value, mono }: { icon?: LucideIcon; label: string; value?: ReactNode; mono?: boolean }) {
  const empty = value == null || value === "";
  return (
    <div className="min-w-0">
      <div className="flex items-center gap-1 text-[10.5px] font-medium uppercase tracking-wide text-muted-foreground">
        {Icon && <Icon size={11} />}
        {label}
      </div>
      <div className={`mt-0.5 break-words text-[13px] ${empty ? "text-muted-foreground/60" : "text-foreground"} ${mono ? "font-mono" : ""}`}>
        {empty ? "—" : value}
      </div>
    </div>
  );
}

/* ==================== Detail panel ==================== */

function TransporterDetail({ detail, onChanged }: { detail: any; onChanged: () => void }) {
  const t = detail.transporter ?? {};
  const vehicles: any[] = detail.vehicles ?? [];
  const history: any[] = detail.blacklist_history ?? [];
  const isBlacklisted = t.blacklisted || t.status === "BLACKLISTED";
  const activeBl = history.find((h) => (h.status ?? "").toUpperCase() === "ACTIVE") ?? (isBlacklisted ? history[0] : undefined);

  const [vehNo, setVehNo] = useState("");
  const [driverId, setDriverId] = useState("");
  const [reason, setReason] = useState("");
  const [severity, setSeverity] = useState<(typeof SEVERITIES)[number]>("HIGH");

  const addVehicle = useMutation({
    mutationFn: () =>
      api.transporterAddVehicle(t.id, { vehicle_no: vehNo.trim(), driver_id: driverId.trim() || undefined }),
    onSuccess: () => {
      setVehNo("");
      setDriverId("");
      onChanged();
    },
  });
  const blacklistAdd = useMutation({
    mutationFn: () => api.transporterBlacklistAdd(t.id, { reason: reason.trim(), severity }),
    onSuccess: () => {
      setReason("");
      onChanged();
    },
  });
  const lift = useMutation({
    mutationFn: () => api.transporterLift(t.id, { actor: "admin" }),
    onSuccess: onChanged,
  });

  const driverCount = vehicles.filter((v) => v.driver_id).length;

  return (
    <div className="space-y-4">
      {/* Header */}
      <Card className="rounded-2xl p-0">
        <div className="flex items-start justify-between gap-3 border-b border-border p-4">
          <div className="flex items-start gap-3">
            <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-primary/10 text-primary">
              <Building2 size={22} />
            </span>
            <div className="min-w-0">
              <div className="text-base font-bold leading-tight">{t.name}</div>
              <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 font-mono text-[11px] text-muted-foreground">
                <span className="inline-flex items-center gap-1">
                  <Hash size={10} />
                  {t.source_company_id ?? t.code ?? "—"}
                </span>
                <span>·</span>
                <span>{t.gstin || "no GSTIN"}</span>
              </div>
            </div>
          </div>
          <StatusChip
            label={isBlacklisted ? "BLACKLISTED" : t.status || "ACTIVE"}
            tone={isBlacklisted ? "critical" : "ok"}
          />
        </div>

        {/* Company information */}
        <div className="p-4">
          <div className="mb-3 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            Company Information
          </div>
          <div className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3">
            <Field icon={Hash} label="Company ID" value={t.source_company_id ?? "—"} mono />
            <Field icon={Building2} label="Company Name" value={t.name} />
            <Field icon={UserRound} label="Contact Person" value={t.contact_person} />
            <Field label="Designation" value={t.designation} />
            <Field icon={Phone} label="Mobile" value={t.mobile} mono />
            <Field icon={Mail} label="Email" value={t.email} />
            <Field icon={FileText} label="Document Type" value={t.doc_type} />
            <Field label="GSTIN" value={t.gstin} mono />
            <Field label="Status" value={<StatusChip label={isBlacklisted ? "BLACKLISTED" : t.status || "ACTIVE"} tone={isBlacklisted ? "critical" : "ok"} />} />
          </div>
          <div className="mt-3">
            <Field icon={MapPin} label="Address" value={t.address} />
          </div>
        </div>
      </Card>

      {/* Fleet information */}
      <Card className="rounded-2xl p-0">
        <div className="flex items-center gap-2 border-b border-border px-4 py-3">
          <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <Truck size={15} />
          </span>
          <h3 className="text-sm font-semibold">Fleet Information</h3>
          <div className="ml-auto flex items-center gap-3 text-[11px] text-muted-foreground">
            <span><span className="font-semibold text-foreground">{vehicles.length}</span> vehicles</span>
            <span><span className="font-semibold text-foreground">{driverCount}</span> drivers</span>
          </div>
        </div>
        <div className="space-y-3 p-4">
          {!vehicles.length ? (
            <DetailEmpty icon={Truck} title="No vehicles mapped" hint="Map a vehicle below to assign it to this transporter." />
          ) : (
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {vehicles.map((v, i) => (
                <div
                  key={`${v.vehicle_no_norm || v.vehicle_no}-${i}`}
                  className="flex items-center justify-between gap-2 rounded-xl border border-border bg-muted/30 px-3 py-2"
                >
                  <span className="inline-flex items-center gap-2 font-mono text-[12.5px] font-medium">
                    <Truck size={13} className="text-muted-foreground" />
                    {v.vehicle_no}
                  </span>
                  <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
                    <UserRound size={11} />
                    {v.driver_id ? v.driver_id : "no driver"}
                  </span>
                </div>
              ))}
            </div>
          )}

          {/* Add vehicle */}
          <div className="rounded-xl border border-dashed border-border p-3">
            <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              Add vehicle
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <input
                value={vehNo}
                onChange={(e) => setVehNo(e.target.value)}
                placeholder="Vehicle no (MH04AB1234)"
                className={`${inputCls} w-44 font-mono`}
              />
              <input
                value={driverId}
                onChange={(e) => setDriverId(e.target.value)}
                placeholder="Driver ID (optional)"
                className={`${inputCls} w-40`}
              />
              <button
                disabled={!vehNo.trim() || addVehicle.isPending}
                onClick={() => addVehicle.mutate()}
                className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-2 text-[12.5px] font-semibold text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
              >
                <Plus size={14} />
                {addVehicle.isPending ? "Adding…" : "Add"}
              </button>
            </div>
            {addVehicle.isError && (
              <div className="mt-1.5 text-[11px]" style={{ color: STATUS.critical }}>
                {(addVehicle.error as Error)?.message}
              </div>
            )}
          </div>
        </div>
      </Card>

      {/* Blacklist */}
      <Card className="rounded-2xl p-0">
        <div className="flex items-center gap-2 border-b border-border px-4 py-3">
          <span
            className="flex h-7 w-7 items-center justify-center rounded-lg"
            style={{ backgroundColor: `${STATUS.critical}1a`, color: STATUS.critical }}
          >
            <Ban size={15} />
          </span>
          <h3 className="text-sm font-semibold">Blacklist</h3>
          <span className="ml-auto">
            <StatusChip
              label={isBlacklisted ? "BLACKLISTED" : "CLEAR"}
              tone={isBlacklisted ? "critical" : "ok"}
            />
          </span>
        </div>
        <div className="space-y-3 p-4">
          {isBlacklisted ? (
            <div
              className="rounded-xl border p-3"
              style={{ borderColor: `${STATUS.critical}55`, backgroundColor: `${STATUS.critical}0f` }}
            >
              <div className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
                <Field label="Current Status" value={<StatusChip label="BLACKLISTED" tone="critical" />} />
                <Field label="Severity" value={activeBl?.severity ? <StatusChip label={activeBl.severity} tone={severityTone(activeBl.severity)} /> : "—"} />
                <Field label="Blacklisted Date" value={activeBl?.blacklisted_at ? fmtDateTimeIST(activeBl.blacklisted_at) : "—"} />
                <Field label="Blacklisted By" value={activeBl?.blacklisted_by} />
              </div>
              <div className="mt-2">
                <Field label="Reason" value={activeBl?.reason} />
              </div>
              <div className="mt-1 text-[11.5px] text-muted-foreground">
                Vehicles operated by this transporter are <strong>denied at the gate</strong>.
              </div>
              <button
                disabled={lift.isPending}
                onClick={() => lift.mutate()}
                className="mt-3 inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-2 text-[12.5px] font-semibold text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
              >
                <ShieldCheck size={14} />
                {lift.isPending ? "Lifting…" : "Lift Blacklist"}
              </button>
              {lift.isError && (
                <div className="mt-1.5 text-[11px]" style={{ color: STATUS.critical }}>
                  {(lift.error as Error)?.message}
                </div>
              )}
            </div>
          ) : (
            <div className="rounded-xl border border-border p-3">
              <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                Blacklist this transporter
              </div>
              <div className="space-y-2">
                <input
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="Reason (e.g. repeated e-Challan violations)"
                  className={inputCls}
                />
                <div className="flex items-center gap-2">
                  <select
                    value={severity}
                    onChange={(e) => setSeverity(e.target.value as (typeof SEVERITIES)[number])}
                    className={`${inputCls} w-36`}
                  >
                    {SEVERITIES.map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                  </select>
                  <button
                    disabled={!reason.trim() || blacklistAdd.isPending}
                    onClick={() => blacklistAdd.mutate()}
                    className="inline-flex items-center gap-1.5 rounded-lg px-3.5 py-2 text-[12.5px] font-semibold text-white transition-opacity disabled:opacity-50"
                    style={{ backgroundColor: STATUS.critical }}
                  >
                    <Ban size={14} />
                    {blacklistAdd.isPending ? "Blacklisting…" : "Blacklist"}
                  </button>
                </div>
              </div>
              {blacklistAdd.isError && (
                <div className="mt-1.5 text-[11px]" style={{ color: STATUS.critical }}>
                  {(blacklistAdd.error as Error)?.message}
                </div>
              )}
            </div>
          )}

          {/* History */}
          <div>
            <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              History ({history.length})
            </div>
            {!history.length ? (
              <DetailEmpty icon={ShieldCheck} title="No blacklist history" hint="This transporter has never been blacklisted." />
            ) : (
              <div className="space-y-1.5">
                {history.map((h, i) => {
                  const lifted = (h.status ?? "").toUpperCase() === "LIFTED";
                  return (
                    <div
                      key={h.id ?? i}
                      className="rounded-xl border border-border bg-muted/20 px-3 py-2 text-[11.5px]"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <StatusChip
                          label={(h.status || "").toUpperCase() || "—"}
                          tone={lifted ? "ok" : "critical"}
                        />
                        <StatusChip label={h.severity || "—"} tone={severityTone(h.severity)} />
                        <span className="text-foreground">{h.reason || "—"}</span>
                      </div>
                      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[10.5px] text-muted-foreground">
                        <span>
                          Blacklisted {h.blacklisted_at ? fmtDateTimeIST(h.blacklisted_at) : "—"}
                          {h.blacklisted_by ? ` · by ${h.blacklisted_by}` : ""}
                        </span>
                        {(h.lifted_at || h.lifted_by) && (
                          <span>
                            Lifted {h.lifted_at ? fmtDateTimeIST(h.lifted_at) : "—"}
                            {h.lifted_by ? ` · by ${h.lifted_by}` : ""}
                          </span>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </Card>
    </div>
  );
}

/* ==================== Vehicle validation widget ==================== */

function VehicleValidation() {
  const [plate, setPlate] = useState("");
  const check = useMutation({
    mutationFn: (p: string) => api.validateVehicle(p.trim()),
  });
  const res: any = check.data;
  const deny = res?.decision === "DENY" || res?.blacklisted;

  return (
    <div className="space-y-3 text-sm">
      <div className="flex items-center gap-2">
        <input
          value={plate}
          onChange={(e) => setPlate(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && plate.trim() && check.mutate(plate)}
          placeholder="Plate (MH04AB1234)"
          className={`${inputCls} font-mono`}
        />
        <button
          disabled={!plate.trim() || check.isPending}
          onClick={() => check.mutate(plate)}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-2 text-[13px] font-semibold text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
        >
          <ScanLine size={14} />
          {check.isPending ? "Checking…" : "Validate"}
        </button>
      </div>

      {check.isError && (
        <div className="text-[11px]" style={{ color: STATUS.critical }}>
          {(check.error as Error)?.message}
        </div>
      )}

      {res && (
        <div
          className="rounded-xl border p-3"
          style={{
            borderColor: `${deny ? STATUS.critical : STATUS.ok}55`,
            backgroundColor: `${deny ? STATUS.critical : STATUS.ok}0f`,
          }}
        >
          <div className="flex items-center gap-2">
            <span
              className="rounded-md px-2 py-0.5 text-[13px] font-bold text-white"
              style={{ backgroundColor: deny ? STATUS.critical : STATUS.ok }}
            >
              {res.decision || (deny ? "DENY" : "ALLOW")}
            </span>
            <span className="font-mono text-[12px]">{res.plate}</span>
          </div>
          <div className="mt-2 space-y-0.5 text-[12px]">
            {res.transporter_name && (
              <div>
                <span className="text-muted-foreground">Transporter: </span>
                {res.transporter_name}
              </div>
            )}
            {res.severity && (
              <div>
                <span className="text-muted-foreground">Severity: </span>
                {res.severity}
              </div>
            )}
            {res.reason && (
              <div>
                <span className="text-muted-foreground">Reason: </span>
                {res.reason}
              </div>
            )}
            {!deny && !res.reason && (
              <div className="text-muted-foreground">No active blacklist — vehicle may enter.</div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/* ==================== Create transporter ==================== */

function CreateTransporter({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [code, setCode] = useState("");
  const [gstin, setGstin] = useState("");

  const create = useMutation({
    mutationFn: () =>
      api.transporterCreate({
        name: name.trim(),
        code: code.trim() || undefined,
        gstin: gstin.trim() || undefined,
      }),
    onSuccess: () => {
      setName("");
      setCode("");
      setGstin("");
      onCreated();
    },
  });

  return (
    <div className="space-y-2 text-sm">
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="Transporter name"
        className={inputCls}
      />
      <div className="flex items-center gap-2">
        <input
          value={code}
          onChange={(e) => setCode(e.target.value)}
          placeholder="Code"
          className={`${inputCls} font-mono`}
        />
        <input
          value={gstin}
          onChange={(e) => setGstin(e.target.value)}
          placeholder="GSTIN"
          className={`${inputCls} font-mono`}
        />
      </div>
      <button
        disabled={!name.trim() || create.isPending}
        onClick={() => create.mutate()}
        className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-2 text-[13px] font-semibold text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
      >
        <Plus size={14} />
        {create.isPending ? "Creating…" : "Register transporter"}
      </button>
      {create.isError && (
        <div className="text-[11px]" style={{ color: STATUS.critical }}>
          {(create.error as Error)?.message}
        </div>
      )}
    </div>
  );
}
