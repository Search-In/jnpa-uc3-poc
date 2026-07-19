// Driver Master & Driver Intelligence — enterprise console over the licensed
// port-driver registry (jnpa.driver_master + jnpa.driver_pdp_history). Server-side
// search / filter / sort / pagination via /api/drivers/master*. Read-only: it does
// NOT touch login, enrollment or identity. Same design system as Transport Master.

import { useEffect, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  AlertTriangle,
  Building2,
  Calendar,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock,
  CreditCard,
  ExternalLink,
  FileText,
  History,
  Inbox,
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
import { fmtDateTimeIST } from "@/lib/utils";

const PAGE_SIZE = 10;

const inputCls =
  "w-full rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary/20";

function useDebounced<T>(value: T, delay = 320): T {
  const [d, setD] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setD(value), delay);
    return () => clearTimeout(id);
  }, [value, delay]);
  return d;
}

function Highlight({ text, query }: { text?: string | null; query: string }) {
  const value = text == null || text === "" ? "—" : String(text);
  const q = query.trim();
  if (!q || value === "—") return <>{value}</>;
  const i = value.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0) return <>{value}</>;
  return (
    <>
      {value.slice(0, i)}
      <mark className="rounded bg-primary/20 px-0.5 text-foreground">
        {value.slice(i, i + q.length)}
      </mark>
      {value.slice(i + q.length)}
    </>
  );
}

function pdpTone(s?: string): Tone {
  switch ((s ?? "").toUpperCase()) {
    case "ACTIVE":
      return "ok";
    case "EXPIRING":
      return "warn";
    case "EXPIRED":
      return "critical";
    default:
      return "neutral";
  }
}
function enrolTone(s?: string): Tone {
  switch ((s ?? "").toUpperCase()) {
    case "ENROLLED":
      return "ok";
    case "PENDING":
      return "warn";
    case "REJECTED":
      return "critical";
    default:
      return "neutral";
  }
}
function verifyTone(s?: string): Tone {
  switch ((s ?? "").toUpperCase()) {
    case "VERIFIED":
      return "ok";
    case "PROVISIONAL":
      return "warn";
    case "REJECTED":
      return "critical";
    default:
      return "neutral";
  }
}

function Avatar({ name }: { name?: string }) {
  const initials = (name || "?")
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((s) => s[0])
    .join("")
    .toUpperCase();
  return (
    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary/10 text-[11px] font-semibold text-primary">
      {initials || "?"}
    </span>
  );
}

/* ==================== Screen ==================== */

export default function DriverMaster() {
  const embedded = useEmbedded();
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const dq = useDebounced(q, 320);
  const [company, setCompany] = useState("");
  const dCompany = useDebounced(company, 320);
  const [statusF, setStatusF] = useState("");
  const [enrolledF, setEnrolledF] = useState("");
  const [verifyF, setVerifyF] = useState("");
  const [sort, setSort] = useState("name");
  const [direction, setDirection] = useState("asc");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);

  const enrolledParam =
    enrolledF === "ENROLLED" ? true : enrolledF === "NOT_ENROLLED" ? false : undefined;

  const params = {
    q: dq.trim() || undefined,
    company: dCompany.trim() || undefined,
    status: statusF || undefined,
    enrolled: enrolledParam,
    verification: verifyF || undefined,
    sort,
    direction,
    limit: PAGE_SIZE,
    offset,
  };

  const listQ = useQuery({
    queryKey: ["driver-master", params],
    queryFn: () => api.driversMaster(params),
  });
  const statsQ = useQuery({
    queryKey: ["driver-master-stats"],
    queryFn: () => api.driverMasterStats(),
  });

  // Reset paging whenever a filter/search changes.
  useEffect(() => {
    setOffset(0);
  }, [dq, dCompany, statusF, enrolledF, verifyF, sort, direction]);

  const items: any[] = listQ.data?.items ?? [];
  const total = listQ.data?.total ?? 0;
  const s = statsQ.data;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const page = Math.floor(offset / PAGE_SIZE);
  const showingFrom = total ? offset + 1 : 0;
  const showingTo = offset + items.length;

  return (
    <PageContainer>
      <PageHeader title="Driver Master" subtitle="Licensed Port Driver Registry" icon={Users} />

      <div className="space-y-4 px-4 py-4">
        {embedded && (
          <div className="flex items-center gap-2.5">
            <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <Users className="h-5 w-5" strokeWidth={2} />
            </span>
            <div>
              <h2 className="text-base font-bold leading-tight tracking-tight">Driver Master</h2>
              <p className="text-xs text-muted-foreground">Licensed Port Driver Registry</p>
            </div>
          </div>
        )}

        {/* KPI cards */}
        <StatGrid className="lg:grid-cols-4 xl:grid-cols-7">
          <StatCard
            icon={Users}
            label="Total Drivers"
            value={(s?.total_drivers ?? 0).toLocaleString()}
            tone="info"
            loading={statsQ.isLoading}
          />
          <StatCard
            icon={ShieldCheck}
            label="Active PDP"
            value={(s?.active_pdp ?? 0).toLocaleString()}
            tone="ok"
            loading={statsQ.isLoading}
          />
          <StatCard
            icon={ShieldAlert}
            label="Expired PDP"
            value={(s?.expired_pdp ?? 0).toLocaleString()}
            tone="critical"
            loading={statsQ.isLoading}
          />
          <StatCard
            icon={Clock}
            label="Expiring Soon"
            value={(s?.expiring_soon ?? 0).toLocaleString()}
            tone="warn"
            loading={statsQ.isLoading}
            sub="≤ 30 days"
          />
          <StatCard
            icon={CheckCircle2}
            label="Enrolled"
            value={(s?.enrolled ?? 0).toLocaleString()}
            tone="ok"
            loading={statsQ.isLoading}
          />
          <StatCard
            icon={AlertTriangle}
            label="Pending Enrollment"
            value={(s?.pending_enrollment ?? 0).toLocaleString()}
            tone="warn"
            loading={statsQ.isLoading}
          />
          <StatCard
            icon={Building2}
            label="Companies"
            value={(s?.companies ?? 0).toLocaleString()}
            tone="neutral"
            loading={statsQ.isLoading}
          />
        </StatGrid>

        <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
          {/* Left: registry */}
          <Card className="rounded-2xl p-0 xl:col-span-7">
            <div className="space-y-3 border-b border-border p-4">
              <div className="flex items-center gap-2">
                <Users size={16} className="text-primary" />
                <h3 className="text-sm font-semibold">Drivers</h3>
                <span className="ml-auto rounded-full bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
                  {total.toLocaleString()}
                </span>
              </div>
              {/* Search */}
              <div className="flex items-center gap-2 rounded-lg border border-border bg-background px-3 py-2 focus-within:border-primary focus-within:ring-2 focus-within:ring-primary/20">
                <Search size={15} className="shrink-0 text-muted-foreground" />
                <input
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder="Search by Licence, Driver, Company or PDP"
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
              {/* Filters (server-side) */}
              <div className="flex flex-wrap items-center gap-2">
                <input
                  value={company}
                  onChange={(e) => setCompany(e.target.value)}
                  placeholder="Company / Transporter"
                  className={`${inputCls} h-9 w-44`}
                />
                <select
                  value={statusF}
                  onChange={(e) => setStatusF(e.target.value)}
                  className={`${inputCls} h-9 w-36`}
                >
                  <option value="">PDP: All</option>
                  <option value="ACTIVE">Active</option>
                  <option value="EXPIRING">Expiring</option>
                  <option value="EXPIRED">Expired</option>
                  <option value="UNKNOWN">Unknown</option>
                </select>
                <select
                  value={enrolledF}
                  onChange={(e) => setEnrolledF(e.target.value)}
                  className={`${inputCls} h-9 w-40`}
                >
                  <option value="">Enrollment: All</option>
                  <option value="ENROLLED">Enrolled</option>
                  <option value="NOT_ENROLLED">Not enrolled</option>
                </select>
                <select
                  value={verifyF}
                  onChange={(e) => setVerifyF(e.target.value)}
                  className={`${inputCls} h-9 w-40`}
                >
                  <option value="">Verification: All</option>
                  <option value="VERIFIED">Verified</option>
                  <option value="PROVISIONAL">Provisional</option>
                  <option value="REJECTED">Rejected</option>
                </select>
                <select
                  value={`${sort}:${direction}`}
                  onChange={(e) => {
                    const [so, di] = e.target.value.split(":");
                    setSort(so);
                    setDirection(di);
                  }}
                  className={`${inputCls} h-9 w-44`}
                >
                  <option value="name:asc">Name A→Z</option>
                  <option value="name:desc">Name Z→A</option>
                  <option value="validity:asc">Validity ↑</option>
                  <option value="validity:desc">Validity ↓</option>
                  <option value="company:asc">Company A→Z</option>
                </select>
              </div>
            </div>

            {listQ.isLoading ? (
              <div className="p-6">
                <LoadingState />
              </div>
            ) : !items.length ? (
              <div className="flex flex-col items-center justify-center gap-2 py-12 text-center">
                <span className="flex h-12 w-12 items-center justify-center rounded-full bg-muted text-muted-foreground">
                  <Inbox size={22} />
                </span>
                <div className="text-sm font-medium">No drivers match</div>
                <div className="max-w-xs text-[12px] text-muted-foreground">
                  Adjust the search or filters.
                </div>
              </div>
            ) : (
              <>
                <div
                  className={`overflow-x-auto transition-opacity ${listQ.isFetching ? "opacity-60" : ""}`}
                >
                  <table className="w-full min-w-[720px] border-collapse text-[12.5px]">
                    <thead>
                      <tr className="border-b border-border text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                        <th className="px-3 py-2 font-medium">Driver</th>
                        <th className="px-2.5 py-2 font-medium">Licence</th>
                        <th className="px-2.5 py-2 font-medium">Company / Transporter</th>
                        <th className="px-2.5 py-2 font-medium">PDP</th>
                        <th className="px-2.5 py-2 font-medium">Validity</th>
                        <th className="px-2.5 py-2 font-medium">Enrollment</th>
                        <th className="px-2.5 py-2 font-medium">Verify</th>
                        <th className="px-2.5 py-2 pr-3 text-right font-medium">Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {items.map((d) => {
                        const active = selected === d.licence_no_norm || selected === d.licence_no;
                        const key = d.licence_no_norm || d.licence_no || d.id;
                        return (
                          <tr
                            key={key}
                            onClick={() => setSelected(d.licence_no_norm || d.licence_no)}
                            className={`cursor-pointer border-b border-border/60 align-middle transition-colors hover:bg-muted/50 ${active ? "bg-primary/5" : ""}`}
                          >
                            <td className="px-3 py-2.5">
                              <div className="flex items-center gap-2">
                                <Avatar name={d.name} />
                                <div className="min-w-0">
                                  <div className="font-medium text-foreground">
                                    <Highlight text={d.name} query={dq} />
                                  </div>
                                  {d.dob && (
                                    <div className="text-[10px] text-muted-foreground">
                                      DOB {String(d.dob)}
                                    </div>
                                  )}
                                </div>
                              </div>
                            </td>
                            <td className="px-2.5 py-2.5 font-mono text-[11.5px]">
                              <Highlight text={d.licence_no} query={dq} />
                            </td>
                            <td className="px-2.5 py-2.5">
                              <div className="text-foreground">
                                <Highlight text={d.company_name} query={dq} />
                              </div>
                              {d.transporter_id && (
                                <div className="text-[10px] text-primary">
                                  ↳ transporter #{d.transporter_id}
                                </div>
                              )}
                            </td>
                            <td className="px-2.5 py-2.5">
                              <StatusChip
                                label={d.pdp_status || "—"}
                                tone={pdpTone(d.pdp_status)}
                              />
                            </td>
                            <td className="px-2.5 py-2.5 whitespace-nowrap text-muted-foreground">
                              {d.licence_valid_to || "—"}
                            </td>
                            <td className="px-2.5 py-2.5">
                              <StatusChip
                                label={d.enrollment_status || "—"}
                                tone={enrolTone(d.enrollment_status)}
                              />
                            </td>
                            <td className="px-2.5 py-2.5">
                              {d.verification ? (
                                <StatusChip
                                  label={d.verification}
                                  tone={verifyTone(d.verification)}
                                />
                              ) : (
                                <span className="text-muted-foreground">—</span>
                              )}
                            </td>
                            <td className="px-2.5 py-2.5 pr-3 text-right whitespace-nowrap">
                              <button
                                onClick={(e) => {
                                  e.stopPropagation();
                                  setSelected(d.licence_no_norm || d.licence_no);
                                }}
                                className="rounded-md border border-border px-2 py-1 text-[11px] font-medium hover:bg-muted"
                              >
                                View
                              </button>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                {/* Server-side pagination */}
                <div className="flex flex-wrap items-center gap-2 border-t border-border px-4 py-2.5 text-[11.5px] text-muted-foreground">
                  <span>
                    Showing{" "}
                    <span className="font-semibold text-foreground">
                      {showingFrom}–{showingTo}
                    </span>{" "}
                    of{" "}
                    <span className="font-semibold text-foreground">{total.toLocaleString()}</span>{" "}
                    drivers
                  </span>
                  <div className="ml-auto flex items-center gap-1">
                    <button
                      disabled={page <= 0}
                      onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                      className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-border hover:bg-muted disabled:opacity-40"
                      aria-label="Previous"
                    >
                      <ChevronLeft size={14} />
                    </button>
                    <span className="px-1 tabular-nums">
                      {page + 1} / {pageCount}
                    </span>
                    <button
                      disabled={page >= pageCount - 1}
                      onClick={() => setOffset(offset + PAGE_SIZE)}
                      className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-border hover:bg-muted disabled:opacity-40"
                      aria-label="Next"
                    >
                      <ChevronRight size={14} />
                    </button>
                  </div>
                </div>
              </>
            )}
          </Card>

          {/* Right: detail */}
          <div className="xl:col-span-5">
            {selected == null ? (
              <Card className="flex min-h-[320px] items-center justify-center rounded-2xl p-6">
                <div className="flex flex-col items-center gap-2 text-center">
                  <span className="flex h-12 w-12 items-center justify-center rounded-full bg-muted text-muted-foreground">
                    <UserRound size={22} />
                  </span>
                  <div className="text-sm font-medium">No driver selected</div>
                  <div className="max-w-xs text-[12px] text-muted-foreground">
                    Select a driver to view profile, PDP history, enrollment and verification.
                  </div>
                </div>
              </Card>
            ) : (
              <DriverDetail
                licence={selected}
                onOpenTransporter={(id) =>
                  navigate(`/vehicles?tab=transporters&transporterId=${id}`)
                }
                onEnroll={(name, lic) =>
                  navigate(
                    `/enrollments?create=1&name=${encodeURIComponent(name || "")}&license=${encodeURIComponent(lic || "")}`,
                  )
                }
              />
            )}
          </div>
        </div>
      </div>
    </PageContainer>
  );
}

/* ==================== Detail ==================== */

function Field({
  icon: Icon,
  label,
  value,
}: {
  icon?: LucideIcon;
  label: string;
  value?: ReactNode;
}) {
  const empty = value == null || value === "";
  return (
    <div className="min-w-0">
      <div className="flex items-center gap-1 text-[10.5px] font-medium uppercase tracking-wide text-muted-foreground">
        {Icon && <Icon size={11} />} {label}
      </div>
      <div
        className={`mt-0.5 break-words text-[13px] ${empty ? "text-muted-foreground/60" : "text-foreground"}`}
      >
        {empty ? "—" : value}
      </div>
    </div>
  );
}

function DriverDetail({
  licence,
  onOpenTransporter,
  onEnroll,
}: {
  licence: string;
  onOpenTransporter: (transporterId: number) => void;
  onEnroll: (name?: string, licence?: string) => void;
}) {
  const [tab, setTab] = useState<"profile" | "pdp">("profile");
  const profileQ = useQuery({
    queryKey: ["driver-master-detail", licence],
    queryFn: () => api.driverMaster(licence),
  });
  const pdpQ = useQuery({
    queryKey: ["driver-master-pdp", licence],
    queryFn: () => api.driverMasterPdpHistory(licence, { limit: 50 }),
    enabled: tab === "pdp",
  });

  if (profileQ.isLoading)
    return (
      <Card className="min-h-[320px] rounded-2xl p-6">
        <LoadingState />
      </Card>
    );
  const p = profileQ.data;
  if (!p)
    return (
      <Card className="flex min-h-[320px] items-center justify-center rounded-2xl p-6">
        <div className="text-sm text-muted-foreground">Driver not found.</div>
      </Card>
    );

  const d = p.driver,
    lic = p.licence,
    co = p.transport_company,
    pdp = p.pdp,
    en = p.enrollment,
    ver = p.verification;

  return (
    <Card className="rounded-2xl p-0">
      {/* Header */}
      <div className="flex items-start justify-between gap-3 border-b border-border p-4">
        <div className="flex items-start gap-3">
          <Avatar name={d?.name} />
          <div className="min-w-0">
            <div className="text-base font-bold leading-tight">{d?.name}</div>
            <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">
              {lic?.licence_no}
            </div>
          </div>
        </div>
        <StatusChip label={lic?.pdp_status || "—"} tone={pdpTone(lic?.pdp_status)} />
      </div>

      {/* Tabs */}
      <div className="flex gap-1 border-b border-border px-3 pt-2">
        {(["profile", "pdp"] as const).map((tk) => (
          <button
            key={tk}
            onClick={() => setTab(tk)}
            className={`rounded-t-md px-3 py-1.5 text-[12px] font-medium ${tab === tk ? "border-b-2 border-primary text-foreground" : "text-muted-foreground hover:text-foreground"}`}
          >
            {tk === "profile" ? "Profile" : "PDP History"}
          </button>
        ))}
      </div>

      {tab === "profile" ? (
        <div className="space-y-4 p-4">
          <Section title="Driver Information">
            <Field icon={UserRound} label="Name" value={d?.name} />
            <Field icon={Calendar} label="Date of Birth" value={d?.dob} />
            <Field label="Photo" value={d?.photo_url ? "on file" : d?.photo_file || "—"} />
          </Section>
          <Section title="Licence Information">
            <Field icon={CreditCard} label="Licence No" value={lic?.licence_no} />
            <Field label="Type" value={lic?.licence_type} />
            <Field icon={Calendar} label="Valid To" value={lic?.valid_to} />
            <Field
              label="Status"
              value={<StatusChip label={lic?.pdp_status || "—"} tone={pdpTone(lic?.pdp_status)} />}
            />
          </Section>
          <Section title="Transport Company">
            <Field icon={Building2} label="Company" value={co?.name} />
            <Field icon={Truck} label="Transporter" value={co?.transporter_name} />
            <Field
              label="Transporter Status"
              value={
                co?.transporter_status ? (
                  <StatusChip
                    label={co.transporter_status}
                    tone={co.transporter_status === "BLACKLISTED" ? "critical" : "ok"}
                  />
                ) : (
                  "—"
                )
              }
            />
            <div className="flex items-end">
              {co?.transporter_id && (
                <button
                  onClick={() => onOpenTransporter(co.transporter_id)}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-[12px] font-medium hover:bg-muted"
                >
                  <ExternalLink size={13} /> Open Transporter
                </button>
              )}
            </div>
          </Section>
          <Section title="PDP Details">
            <Field icon={FileText} label="Latest PDP" value={pdp?.latest_pdp_number} />
            <Field label="Application" value={pdp?.appl_number} />
            <Field label="Active" value={pdp?.active == null ? "—" : pdp.active ? "Yes" : "No"} />
            <Field icon={Calendar} label="PDP Validity" value={pdp?.validity} />
          </Section>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <Section title="Enrollment Status">
              <Field
                label="Status"
                value={<StatusChip label={en?.status || "—"} tone={enrolTone(en?.status)} />}
              />
              <Field label="Linked Driver" value={en?.linked_driver_id} />
              <Field label="Vehicle" value={en?.vehicle_no} />
              {en?.status !== "ENROLLED" && (
                <div className="col-span-2 flex items-end">
                  <button
                    onClick={() => onEnroll(d?.name, lic?.licence_no)}
                    className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-[12px] font-semibold text-primary-foreground hover:bg-primary/90"
                  >
                    <UserRound size={13} /> Enroll Driver
                  </button>
                </div>
              )}
            </Section>
            <Section title="Verification Status">
              <Field
                icon={ScanLine}
                label="Decision"
                value={
                  ver?.decision ? (
                    <StatusChip label={ver.decision} tone={verifyTone(ver.decision)} />
                  ) : (
                    "—"
                  )
                }
              />
              <Field
                label="Score"
                value={ver?.score != null ? Number(ver.score).toFixed(3) : "—"}
              />
              <Field
                label="Verified At"
                value={ver?.verified_at ? fmtDateTimeIST(ver.verified_at) : "—"}
              />
            </Section>
          </div>
        </div>
      ) : (
        <div className="p-4">
          <div className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            <History size={13} /> PDP Timeline {pdpQ.data ? `(${pdpQ.data.total})` : ""}
          </div>
          {pdpQ.isLoading ? (
            <LoadingState />
          ) : !pdpQ.data?.items?.length ? (
            <div className="py-8 text-center text-[12px] text-muted-foreground">
              No PDP history.
            </div>
          ) : (
            <div className="space-y-2">
              {pdpQ.data.items.map((h: any, i: number) => {
                const cancelled = !!h.cancellation_time || !!h.pdp_cancelled_by;
                return (
                  <div
                    key={h.pdp_id ?? i}
                    className="rounded-xl border border-border bg-muted/20 px-3 py-2 text-[11.5px]"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <StatusChip
                        label={h.active ? "ACTIVE" : cancelled ? "CANCELLED" : "EXPIRED"}
                        tone={h.active ? "ok" : cancelled ? "critical" : "neutral"}
                      />
                      <span className="font-mono font-medium text-foreground">{h.pdp_number}</span>
                      <span className="ml-auto text-muted-foreground">
                        {h.acceptance_time_stamp ? fmtDateTimeIST(h.acceptance_time_stamp) : ""}
                      </span>
                    </div>
                    <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[10.5px] text-muted-foreground">
                      {h.validity && <span>Valid to {String(h.validity)}</span>}
                      {h.remarks && <span>· {h.remarks}</span>}
                      {cancelled && (
                        <span>
                          · Cancelled{" "}
                          {h.cancellation_time ? fmtDateTimeIST(h.cancellation_time) : ""}
                          {h.pdp_cancelled_by ? ` by ${h.pdp_cancelled_by}` : ""}
                        </span>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-3">{children}</div>
    </div>
  );
}
