// Reports & Enforcement — command-centre reporting & enforcement console
// (FINAL PHASE redesign). Six report tabs (Traffic / Police / Violations /
// Challans / Customs / Carbon) over existing RDS endpoints, with summary cards,
// advanced filters, pagination, PDF export, and incident drill-down. The live
// enforcement flow (Driver Identity, Vehicle Violation Detection, the
// `violation_enforced` WS toast) is preserved verbatim. No backend changes.

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  FileText,
  FileWarning,
  CalendarClock,
  Clock,
  ReceiptText,
  ShieldAlert,
  Cpu,
  Leaf,
  ScanFace,
  FileDown,
  ExternalLink,
  BarChart3,
} from "lucide-react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis, Cell } from "recharts";
import { useSocket } from "@/hooks/SocketContext";
import { getAdapter } from "@/data";
import { api } from "@/lib/api";
import type { PoliceIncident } from "@/lib/types";
import { Card } from "@/components/ui/card";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { IdentityPanel } from "@/components/panels/IdentityPanel";
import { ViolationDetectionPanel } from "@/components/panels/ViolationDetectionPanel";
import {
  PageContainer,
  PageHeader,
  StatGrid,
  StatCard,
  SegmentedTabs,
  DataTable,
  FilterSelect,
  StatusChip,
  type Column,
  type Tone,
} from "@/components/ui/dtccc";
import { EmptyState } from "@/components/ui/misc";
import { severityColour } from "@/lib/palette";
import { STATUS } from "@/lib/tokens";
import { fmtDateTimeIST } from "@/lib/utils";

async function exportPolicePdf(params: Record<string, string | undefined>) {
  try {
    await getAdapter().downloadPolicePdf(params);
  } catch (err) {
    console.error("police pdf export failed", err);
    alert("Could not export the PDF report. Please try again.");
  }
}

const KINDS = ["WRONG_WAY", "ILLEGAL_PARKING", "OVERSPEEDING", "ROUTE_DEVIATION"];
const GATES = ["G-NSICT", "G-JNPCT", "G-NSIGT", "G-BMCT"];
const SEVERITIES = ["info", "warning", "critical", "REPORT_TO_POLICE"];
const KIND_COLOURS = [STATUS.critical, STATUS.warning, STATUS.info, STATUS.ok, "#CC79A7"];

type TabKey = "traffic" | "police" | "violations" | "challans" | "customs" | "carbon";

function sevTone(sev: string): Tone {
  if (sev === "critical" || sev === "REPORT_TO_POLICE") return "critical";
  if (sev === "warning") return "warn";
  if (sev === "info") return "info";
  return "neutral";
}

export default function PoliceReports() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [tab, setTab] = useState<TabKey>("police");
  const [kind, setKind] = useState("");
  const [gate, setGate] = useState("");
  const [severity, setSeverity] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [selected, setSelected] = useState<PoliceIncident | null>(null);
  const [identityOpen, setIdentityOpen] = useState(false);
  const [violationOpen, setViolationOpen] = useState(false);

  // Live enforcement toast + row highlight (violation_enforced WS frame).
  const { subscribe } = useSocket();
  const [flash, setFlash] = useState<{ plate?: string | null; challan_no?: string | null; fine: number } | null>(null);
  const [highlightPlate, setHighlightPlate] = useState<string | null>(null);
  useEffect(() => {
    return subscribe((frame) => {
      if (frame.type !== "violation_enforced") return;
      const p = frame.payload;
      void qc.invalidateQueries({ queryKey: ["police"] });
      setFlash({ plate: p.plate, challan_no: p.challan_no, fine: p.fine });
      setHighlightPlate(p.plate ?? null);
    });
  }, [subscribe, qc]);
  useEffect(() => {
    if (!flash) return;
    const t1 = setTimeout(() => setFlash(null), 6000);
    const t2 = setTimeout(() => setHighlightPlate(null), 6000);
    return () => { clearTimeout(t1); clearTimeout(t2); };
  }, [flash]);

  const filters: Record<string, string | undefined> = {
    kind: kind || undefined,
    gate: gate || undefined,
    severity: severity || undefined,
    since: since ? new Date(since).toISOString() : undefined,
    until: until ? new Date(until).toISOString() : undefined,
  };

  const q = useQuery({ queryKey: ["police", filters], queryFn: () => getAdapter().policeReport(filters), refetchInterval: 10_000 });
  const customsQ = useQuery({ queryKey: ["customs-history"], queryFn: () => api.customsHistory(200) });
  const carbonQ = useQuery({ queryKey: ["carbon"], queryFn: () => getAdapter().carbonRollup() });
  const aiQ = useQuery({ queryKey: ["ai-events"], queryFn: () => api.aiEvents(undefined, 200) });
  const catalogQ = useQuery({ queryKey: ["violation-catalog"], queryFn: () => getAdapter().violationCatalog() });

  const incidents = q.data ?? [];
  const withChallan = incidents.filter((i) => i.challan && Object.keys(i.challan).length > 0);
  const today = new Date().toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata" });
  const todayCount = incidents.filter((i) => new Date(i.ts).toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata" }) === today).length;

  return (
    <PageContainer>
      <PageHeader
        icon={FileText}
        title={t("nav.reports")}
        subtitle="Traffic · Police · Violations · Challans · Customs · Carbon — RDS-backed"
        updatedAt={q.dataUpdatedAt}
        isFetching={q.isFetching && !q.isLoading}
        onRefresh={() => qc.invalidateQueries({ queryKey: ["police"] })}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <button onClick={() => setIdentityOpen(true)} className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium hover:bg-muted"><ScanFace className="h-3.5 w-3.5" /> Identity</button>
            <button onClick={() => setViolationOpen(true)} className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium hover:bg-muted"><ShieldAlert className="h-3.5 w-3.5" /> Detect</button>
            <button onClick={() => void exportPolicePdf(filters)} className="inline-flex items-center gap-1.5 rounded-md bg-primary px-2.5 py-1.5 text-xs font-semibold text-primary-foreground hover:bg-primary/90"><FileDown className="h-3.5 w-3.5" /> {t("reports.exportPdf")}</button>
          </div>
        }
      />

      {flash && (
        <div className="flex items-center gap-2 border-b border-severity-warning/40 bg-severity-warning/10 px-4 py-2 text-xs">
          <ShieldAlert className="h-4 w-4 text-severity-warning" />
          <span className="font-semibold">New challan enforced</span>
          <span className="font-mono">{flash.plate ?? "—"}</span>
          {flash.challan_no && <span className="font-mono text-muted-foreground">· {flash.challan_no}</span>}
          <span className="ml-auto font-mono font-semibold text-severity-critical">₹{flash.fine.toLocaleString("en-IN")}</span>
        </div>
      )}

      {/* Summary cards */}
      <div className="px-4 pt-3">
        <StatGrid className="lg:grid-cols-6">
          <StatCard icon={FileWarning} label="Total Violations" value={incidents.length} tone="warn" loading={q.isLoading} />
          <StatCard icon={CalendarClock} label="Today's Cases" value={todayCount} tone="info" loading={q.isLoading} />
          <StatCard icon={Clock} label="Pending Cases" value={incidents.length - withChallan.length} tone={(incidents.length - withChallan.length) > 0 ? "warn" : "ok"} loading={q.isLoading} />
          <StatCard icon={ReceiptText} label="Challans Issued" value={withChallan.length} tone="ok" loading={q.isLoading} />
          <StatCard icon={ShieldAlert} label="Customs Flags" value={customsQ.data?.alerts?.length ?? "—"} tone={(customsQ.data?.alerts?.length ?? 0) > 0 ? "critical" : "ok"} loading={customsQ.isLoading} />
          <StatCard icon={Cpu} label="AI Incidents" value={aiQ.data?.count ?? "—"} tone={(aiQ.data?.count ?? 0) > 0 ? "warn" : "ok"} loading={aiQ.isLoading} />
        </StatGrid>
      </div>

      {/* Tabs */}
      <div className="px-4 py-3">
        <SegmentedTabs
          value={tab}
          onChange={setTab}
          className="mb-3"
          tabs={[
            { key: "traffic", label: "Traffic Reports", icon: BarChart3 },
            { key: "police", label: "Police Reports", icon: FileText, count: incidents.length },
            { key: "violations", label: "Violations", icon: FileWarning },
            { key: "challans", label: "Challans", icon: ReceiptText, count: withChallan.length },
            { key: "customs", label: "Customs", icon: ShieldAlert, count: customsQ.data?.alerts?.length },
            { key: "carbon", label: "Carbon", icon: Leaf },
          ]}
        />

        {tab === "traffic" && <TrafficReports incidents={incidents} />}
        {tab === "police" && (
          <div className="space-y-3">
            <FilterBar kind={kind} setKind={setKind} gate={gate} setGate={setGate} severity={severity} setSeverity={setSeverity} since={since} setSince={setSince} until={until} setUntil={setUntil} />
            <Card className="overflow-hidden">
              <IncidentsTable incidents={incidents} status={q} onRetry={() => q.refetch()} onSelect={setSelected} highlightPlate={highlightPlate} />
            </Card>
          </div>
        )}
        {tab === "violations" && <ViolationsTab catalogQ={catalogQ} incidents={incidents} />}
        {tab === "challans" && (
          <Card className="overflow-hidden">
            <ChallansTable incidents={withChallan} onSelect={setSelected} />
          </Card>
        )}
        {tab === "customs" && (
          <Card className="overflow-hidden">
            <CustomsTable rows={customsQ.data?.alerts ?? []} status={customsQ} onRetry={() => customsQ.refetch()} />
          </Card>
        )}
        {tab === "carbon" && <CarbonTab carbonQ={carbonQ} />}
      </div>

      <IncidentDialog incident={selected} onClose={() => setSelected(null)} />

      <Dialog open={identityOpen} onOpenChange={setIdentityOpen}>
        <DialogContent className="max-w-xl p-3">
          <DialogTitle className="sr-only">{t("reports.driverIdentityVerification")}</DialogTitle>
          <IdentityPanel />
        </DialogContent>
      </Dialog>
      <Dialog open={violationOpen} onOpenChange={setViolationOpen}>
        <DialogContent className="max-w-xl p-3">
          <DialogTitle className="sr-only">{t("reports.vehicleViolationDetection", { defaultValue: "Vehicle Violation Detection" })}</DialogTitle>
          <ViolationDetectionPanel />
        </DialogContent>
      </Dialog>
    </PageContainer>
  );
}

function FilterBar(props: {
  kind: string; setKind: (v: string) => void;
  gate: string; setGate: (v: string) => void;
  severity: string; setSeverity: (v: string) => void;
  since: string; setSince: (v: string) => void;
  until: string; setUntil: (v: string) => void;
}) {
  const opt = (arr: string[]) => [{ value: "", label: "All" }, ...arr.map((o) => ({ value: o, label: o }))];
  const dateCls = "h-9 rounded-md border border-border bg-background px-2 text-[13px] outline-none focus:ring-2 focus:ring-primary/20";
  return (
    <Card className="flex flex-wrap items-end gap-3 p-3">
      <label className="text-[11px] text-muted-foreground">Kind<div className="mt-1"><FilterSelect value={props.kind} onChange={props.setKind} options={opt(KINDS)} /></div></label>
      <label className="text-[11px] text-muted-foreground">Gate<div className="mt-1"><FilterSelect value={props.gate} onChange={props.setGate} options={opt(GATES)} /></div></label>
      <label className="text-[11px] text-muted-foreground">Severity<div className="mt-1"><FilterSelect value={props.severity} onChange={props.setSeverity} options={opt(SEVERITIES)} /></div></label>
      <label className="text-[11px] text-muted-foreground">From<input type="datetime-local" value={props.since} onChange={(e) => props.setSince(e.target.value)} className={`mt-1 block ${dateCls}`} /></label>
      <label className="text-[11px] text-muted-foreground">To<input type="datetime-local" value={props.until} onChange={(e) => props.setUntil(e.target.value)} className={`mt-1 block ${dateCls}`} /></label>
    </Card>
  );
}

function IncidentsTable({ incidents, status, onRetry, onSelect, highlightPlate }: { incidents: PoliceIncident[]; status: any; onRetry: () => void; onSelect: (i: PoliceIncident) => void; highlightPlate: string | null }) {
  const columns: Column<PoliceIncident>[] = [
    { key: "ts", header: "Time", className: "tabular-nums whitespace-nowrap", render: (i) => fmtDateTimeIST(i.ts) },
    { key: "kind", header: "Kind", render: (i) => i.kind },
    { key: "sev", header: "Severity", render: (i) => <StatusChip label={i.severity} tone={sevTone(i.severity)} /> },
    { key: "plate", header: "Plate", className: "font-mono", render: (i) => (
      <span className={i.plate && i.plate === highlightPlate ? "animate-pulse rounded bg-severity-warning/20 px-1" : ""}>{i.plate ?? "—"}</span>
    ) },
    { key: "gate", header: "Gate", render: (i) => i.gate_id?.replace("G-", "") ?? "—" },
    { key: "owner", header: "Owner", className: "text-muted-foreground", render: (i) => i.rc?.owner_name_masked ?? "—" },
    { key: "ev", header: "Evidence", align: "right", render: (i) => (i.evidence_url ? <span className="text-severity-info">photo</span> : "—") },
  ];
  return (
    <DataTable
      columns={columns}
      rows={incidents}
      rowKey={(i) => i.id}
      status={status}
      onRetry={onRetry}
      emptyLabel="No incidents match these filters."
      search={(i, q) => `${i.plate ?? ""} ${i.kind} ${i.gate_id ?? ""} ${i.rc?.owner_name_masked ?? ""}`.toLowerCase().includes(q)}
      searchPlaceholder="Search plate / kind / gate…"
      pageSize={12}
      onRowClick={onSelect}
    />
  );
}

function ChallansTable({ incidents, onSelect }: { incidents: PoliceIncident[]; onSelect: (i: PoliceIncident) => void }) {
  const columns: Column<PoliceIncident>[] = [
    { key: "no", header: "Challan", className: "font-mono", render: (i) => String((i.challan as any)?.challan_no ?? (i.challan as any)?.echallan_id ?? i.id) },
    { key: "plate", header: "Plate", className: "font-mono", render: (i) => i.plate ?? "—" },
    { key: "kind", header: "Offence", render: (i) => i.kind },
    { key: "fine", header: "Fine", align: "right", className: "tabular-nums", render: (i) => {
      const f = (i.challan as any)?.fine_inr ?? (i.challan as any)?.amount ?? (i.challan as any)?.total_fine;
      return f != null ? `₹${Number(f).toLocaleString("en-IN")}` : "—";
    } },
    { key: "ts", header: "Issued", className: "whitespace-nowrap text-muted-foreground", render: (i) => fmtDateTimeIST(i.ts) },
  ];
  return (
    <DataTable
      columns={columns}
      rows={incidents}
      rowKey={(i) => i.id}
      emptyLabel="No challans issued in this filter."
      search={(i, q) => `${i.plate ?? ""} ${i.kind}`.toLowerCase().includes(q)}
      searchPlaceholder="Search challans…"
      pageSize={12}
      onRowClick={onSelect}
    />
  );
}

function CustomsTable({ rows, status, onRetry }: { rows: any[]; status: any; onRetry: () => void }) {
  const columns: Column<any>[] = [
    { key: "flag", header: "Flag", className: "font-medium", render: (a) => String(a.payload?.flag ?? "—") },
    { key: "sev", header: "Severity", render: (a) => <StatusChip label={a.severity} tone={a.severity === "critical" ? "critical" : "warn"} /> },
    { key: "container", header: "Container", className: "font-mono", render: (a) => String(a.payload?.container_no ?? "—") },
    { key: "plate", header: "Vehicle", className: "font-mono", render: (a) => a.plate ?? "—" },
    { key: "ts", header: "Raised", className: "text-muted-foreground", render: (a) => fmtDateTimeIST(a.ts) },
  ];
  return (
    <DataTable columns={columns} rows={rows} rowKey={(a) => a.id} status={status} onRetry={onRetry} emptyLabel="No customs flags in RDS." search={(a, q) => `${String(a.payload?.flag ?? "")} ${a.plate ?? ""} ${a.severity}`.toLowerCase().includes(q)} searchPlaceholder="Search flags…" pageSize={12} />
  );
}

function TrafficReports({ incidents }: { incidents: PoliceIncident[] }) {
  const byKind = useMemo(() => {
    const m = new Map<string, number>();
    for (const i of incidents) m.set(i.kind, (m.get(i.kind) ?? 0) + 1);
    return Array.from(m.entries()).map(([name, count]) => ({ name, count }));
  }, [incidents]);
  const byGate = useMemo(() => {
    const m = new Map<string, number>();
    for (const i of incidents) { const g = i.gate_id?.replace("G-", "") ?? "—"; m.set(g, (m.get(g) ?? 0) + 1); }
    return Array.from(m.entries()).map(([name, count]) => ({ name, count }));
  }, [incidents]);
  if (incidents.length === 0) return <Card className="p-0"><EmptyState>No incident data to report.</EmptyState></Card>;
  return (
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
      <ChartCard title="Incidents by Kind" data={byKind} coloured />
      <ChartCard title="Incidents by Gate" data={byGate} />
    </div>
  );
}

function ChartCard({ title, data, coloured }: { title: string; data: { name: string; count: number }[]; coloured?: boolean }) {
  return (
    <Card className="p-3">
      <h3 className="mb-2 text-sm font-semibold">{title}</h3>
      <div className="h-56">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 4, right: 8, left: -18, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(215 20% 90%)" vertical={false} />
            <XAxis dataKey="name" tick={{ fontSize: 10 }} interval={0} angle={-12} textAnchor="end" height={50} />
            <YAxis tick={{ fontSize: 10 }} allowDecimals={false} />
            <Tooltip contentStyle={{ fontSize: 12, borderRadius: 8 }} />
            <Bar dataKey="count" radius={[3, 3, 0, 0]} fill={STATUS.info}>
              {coloured && data.map((_, i) => <Cell key={i} fill={KIND_COLOURS[i % KIND_COLOURS.length]} />)}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}

function ViolationsTab({ catalogQ, incidents }: { catalogQ: any; incidents: PoliceIncident[] }) {
  const counts = useMemo(() => {
    const m = new Map<string, number>();
    for (const i of incidents) m.set(i.kind, (m.get(i.kind) ?? 0) + 1);
    return m;
  }, [incidents]);
  const catalog = catalogQ.data ?? [];
  const columns: Column<any>[] = [
    { key: "label", header: "Violation", className: "font-medium", render: (c) => c.label ?? c.kind },
    { key: "section", header: "Section", render: (c) => c.section ?? "—" },
    { key: "fine", header: "Fine", align: "right", className: "tabular-nums", render: (c) => (c.fine_inr != null ? `₹${Number(c.fine_inr).toLocaleString("en-IN")}` : "—") },
    { key: "cases", header: "Cases (filtered)", align: "right", className: "tabular-nums", render: (c) => counts.get(c.kind) ?? 0 },
  ];
  return (
    <Card className="overflow-hidden">
      <DataTable columns={columns} rows={catalog} rowKey={(c: any) => c.kind} status={catalogQ} onRetry={() => catalogQ.refetch()} emptyLabel="No violation catalog available." search={(c: any, q) => `${c.label ?? ""} ${c.kind}`.toLowerCase().includes(q)} searchPlaceholder="Search violation types…" pageSize={12} />
    </Card>
  );
}

function CarbonTab({ carbonQ }: { carbonQ: any }) {
  const c = carbonQ.data;
  const byClass = useMemo(() => (c ? Object.entries(c.by_class).map(([name, kg]) => ({ name, count: Math.round(Number(kg)) })) : []), [c]);
  if (carbonQ.isLoading) return <Card className="p-0"><EmptyState>Loading carbon report…</EmptyState></Card>;
  if (carbonQ.isError || !c) return <Card className="p-0"><EmptyState>No carbon data available.</EmptyState></Card>;
  return (
    <div className="space-y-3">
      <StatGrid className="lg:grid-cols-4">
        <StatCard icon={Leaf} label="Total CO₂e" value={c.total_kg >= 1000 ? `${(c.total_kg / 1000).toFixed(1)} t` : `${Math.round(c.total_kg)} kg`} tone="ok" />
        <StatCard icon={BarChart3} label="Vehicles" value={c.vehicle_count} tone="info" />
        <StatCard icon={Leaf} label="Moving" value={`${Math.round(c.by_source.moving)} kg`} tone="info" />
        <StatCard icon={Clock} label="Idle" value={`${Math.round(c.by_source.idle)} kg`} tone="warn" />
      </StatGrid>
      <ChartCard title="CO₂e by Vehicle Class (kg)" data={byClass} coloured />
    </div>
  );
}

// --- Incident drill-down dialog (preserved) ----------------------------------

function IncidentDialog({ incident, onClose }: { incident: PoliceIncident | null; onClose: () => void }) {
  const { t } = useTranslation();
  const sev = incident ? severityColour(incident.severity) : undefined;
  const challan = incident?.challan;
  return (
    <Dialog open={!!incident} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        {incident && (
          <>
            <DialogHeader className="flex items-center gap-3">
              <span className="h-9 w-1 shrink-0 rounded-full" style={{ backgroundColor: sev }} aria-hidden />
              <DialogTitle className="flex min-w-0 flex-col gap-0.5">
                <span className="truncate text-base font-semibold leading-tight tracking-tight">{t(`alertKind.${incident.kind}`, { defaultValue: incident.kind })}</span>
                <span className="font-mono text-xs font-medium text-muted-foreground">{incident.plate ?? "—"}</span>
              </DialogTitle>
            </DialogHeader>
            <div className="space-y-4 p-5">
              <div className="grid grid-cols-2 gap-x-4 gap-y-3.5">
                <KV k={t("reports.incidentId")} v={incident.id} />
                <KV k={t("notifications.time")} v={fmtDateTimeIST(incident.ts)} />
                <KV k={t("reports.ownerMasked")} v={incident.rc?.owner_name_masked ?? "—"} />
                <KV k={t("reports.vehicleClass")} v={incident.rc?.vehicle_class ?? "—"} />
                <KV k={t("reports.rtoState")} v={`${incident.rc?.rto_code ?? "—"} / ${incident.rc?.state ?? "—"}`} />
                <KV k={t("reports.fastag")} v={incident.rc?.fastag_status ?? "—"} />
              </div>
              {incident.evidence_url && (
                <img src={incident.evidence_url} alt={t("reports.evidenceAlt")} className="w-full rounded-lg border border-border shadow-sm" onError={(e) => ((e.target as HTMLImageElement).style.display = "none")} />
              )}
              {challan && Object.keys(challan).length > 0 && (
                <div className="overflow-hidden rounded-lg border border-severity-warning/40">
                  <div className="flex items-center gap-1.5 border-b border-severity-warning/30 bg-severity-warning/10 px-3.5 py-2.5 text-xs font-semibold">
                    <ReceiptText className="h-4 w-4 text-severity-warning" aria-hidden /> {t("reports.recommendedAction")}
                  </div>
                  <dl>
                    {Object.entries(challan).map(([key, value]) => (
                      <div key={key} className="flex items-start justify-between gap-3 border-b border-border/60 px-3.5 py-2 text-xs last:border-b-0">
                        <dt className="shrink-0 font-medium text-muted-foreground">{humanizeKey(key)}</dt>
                        <dd className="min-w-0 break-words text-right font-mono font-medium text-foreground" title={formatChallanValue(key, value)}>{formatChallanValue(key, value)}</dd>
                      </div>
                    ))}
                  </dl>
                </div>
              )}
              <button type="button" onClick={() => void exportPolicePdf({ id: incident.id })} className="inline-flex w-full items-center justify-center gap-2 rounded-lg border border-severity-info/40 bg-severity-info/10 px-4 py-2.5 text-sm font-semibold text-severity-info transition-colors hover:bg-severity-info/20">
                <ExternalLink className="h-4 w-4" /> {t("reports.downloadThisReport")}
              </button>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

function KV({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="mb-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">{k}</div>
      <div className="break-words text-sm text-foreground">{v}</div>
    </div>
  );
}

function humanizeKey(key: string): string {
  const spaced = key.replace(/[_-]+/g, " ").trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
function formatChallanValue(key: string, value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "number" && /amount|inr|fee|fine/i.test(key)) return `₹${value.toLocaleString("en-IN")}`;
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
