import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSocket } from "@/hooks/SocketContext";
import { getAdapter } from "@/data";
import type { PoliceIncident } from "@/lib/types";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { EmptyState, AsyncBoundary, LastUpdated } from "@/components/ui/misc";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { IdentityPanel } from "@/components/panels/IdentityPanel";
import { ViolationDetectionPanel } from "@/components/panels/ViolationDetectionPanel";
import { severityColour } from "@/lib/palette";
import { fmtDateTimeIST } from "@/lib/utils";
import { FileDown, ExternalLink, ReceiptText, ScanFace, ShieldAlert } from "lucide-react";

// Stream the report PDF with the bearer token attached (a plain <a href>/new-tab
// navigation can't send the header, so it 401s under auth-enabled builds).
// Module-level so both the header button and the per-incident dialog can use it.
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

// Tabular view of police-relevant alerts with filters (date / gate / severity /
// kind). "Export PDF" opens /api/reports/police?format=pdf (server-side
// Playwright) in a new tab — a one-page-per-incident report with evidence + a
// pre-filled e-Challan payload.
export default function PoliceReports() {
  const { t } = useTranslation();
  const [kind, setKind] = useState<string>("");
  const [gate, setGate] = useState<string>("");
  const [severity, setSeverity] = useState<string>("");
  const [since, setSince] = useState<string>("");
  const [until, setUntil] = useState<string>("");
  const [selected, setSelected] = useState<PoliceIncident | null>(null);
  // Driver Identity Verification opens the existing IdentityPanel in a centered
  // modal — no routing change, verification logic reused as-is.
  const [identityOpen, setIdentityOpen] = useState(false);
  // Vehicle Violation Detection — the orchestration console (ANPR + lookup +
  // rule engine + e-Challan) in a centered modal, beside Driver Identity.
  const [violationOpen, setViolationOpen] = useState(false);

  // Live enforcement toast + row highlight, driven by the gateway's
  // `violation_enforced` WS frame (the /api/violations/enforce pipeline). Lets
  // ANY operator's auto-enforced challan surface here in real time.
  const qc = useQueryClient();
  const { subscribe } = useSocket();
  const [flash, setFlash] = useState<{
    plate?: string | null;
    challan_no?: string | null;
    fine: number;
  } | null>(null);
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
    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
    };
  }, [flash]);

  const filters: Record<string, string | undefined> = {
    kind: kind || undefined,
    gate: gate || undefined,
    severity: severity || undefined,
    since: since ? new Date(since).toISOString() : undefined,
    until: until ? new Date(until).toISOString() : undefined,
  };

  const q = useQuery({
    queryKey: ["police", filters],
    queryFn: () => getAdapter().policeReport(filters),
    refetchInterval: 10_000,
  });
  const incidents = q.data ?? [];

  return (
    <div className="flex h-full flex-col">
      {/* Real-time enforcement toast — a new auto-enforced challan arrived. */}
      {flash && (
        <div className="flex items-center gap-2 border-b border-severity-warning/40 bg-severity-warning/10 px-4 py-2 text-xs">
          <ShieldAlert className="h-4 w-4 text-severity-warning" />
          <span className="font-semibold">
            {t("reports.enforcedToast", { defaultValue: "New challan enforced" })}
          </span>
          <span className="font-mono">{flash.plate ?? "—"}</span>
          {flash.challan_no && (
            <span className="font-mono text-muted-foreground">· {flash.challan_no}</span>
          )}
          <span className="ml-auto font-mono font-semibold text-severity-critical">
            ₹{flash.fine.toLocaleString("en-IN")}
          </span>
        </div>
      )}
      <div className="flex flex-wrap items-end justify-between gap-3 border-b border-border p-4">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-lg font-semibold">{t("nav.reports")}</h1>
            <LastUpdated at={q.dataUpdatedAt} isFetching={q.isFetching && !q.isLoading} />
          </div>
          <p className="text-sm text-muted-foreground">
            WRONG_WAY · ILLEGAL_PARKING · OVERSPEEDING · ROUTE_DEVIATION
          </p>
        </div>
        {/* Header actions — Driver Identity Verification before Export, same style. */}
        <div className="flex items-center gap-2">
          <Button onClick={() => setIdentityOpen(true)}>
            <ScanFace className="h-4 w-4" /> {t("reports.driverIdentityVerification")}
          </Button>
          <Button onClick={() => setViolationOpen(true)}>
            <ShieldAlert className="h-4 w-4" />{" "}
            {t("reports.vehicleViolationDetection", {
              defaultValue: "Vehicle Violation Detection",
            })}
          </Button>
          <Button onClick={() => void exportPolicePdf(filters)}>
            <FileDown className="h-4 w-4" /> {t("reports.exportPdf")}
          </Button>
        </div>
      </div>

      {/* filters */}
      <div className="grid grid-cols-2 gap-3 border-b border-border p-4 md:grid-cols-5">
        <FilterSelect label={t("reports.kind")} value={kind} onChange={setKind} options={KINDS} />
        <FilterSelect
          label={t("notifications.gate")}
          value={gate}
          onChange={setGate}
          options={GATES}
        />
        <FilterSelect
          label={t("notifications.severity")}
          value={severity}
          onChange={setSeverity}
          options={SEVERITIES}
        />
        <FilterDate label={t("reports.from")} value={since} onChange={setSince} />
        <FilterDate label={t("reports.to")} value={until} onChange={setUntil} />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <Card>
          <CardContent className="p-0">
            <AsyncBoundary
              status={q}
              isEmpty={incidents.length === 0}
              onRetry={() => q.refetch()}
              empty={<EmptyState>{t("reports.noIncidents")}</EmptyState>}
            >
              <table className="w-full text-sm">
                <thead className="border-b border-border text-left text-xs text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2">{t("notifications.time")}</th>
                    <th className="px-4 py-2">{t("reports.kind")}</th>
                    <th className="px-4 py-2">{t("notifications.severity")}</th>
                    <th className="px-4 py-2">{t("reports.plate")}</th>
                    <th className="px-4 py-2">{t("notifications.gate")}</th>
                    <th className="px-4 py-2">{t("reports.owner")}</th>
                    <th className="px-4 py-2 text-right">{t("reports.evidence")}</th>
                  </tr>
                </thead>
                <tbody>
                  {incidents.map((inc) => (
                    // Tagged by incident kind so the guided tour can ring the
                    // EXACT row (e.g. the WRONG_WAY e-Challan), not the table.
                    <tr
                      key={inc.id}
                      data-guided-id={`report-${inc.kind}`}
                      onClick={() => setSelected(inc)}
                      className={`cursor-pointer border-b border-border/50 hover:bg-muted/40 ${
                        inc.plate && inc.plate === highlightPlate
                          ? "animate-pulse bg-severity-warning/10"
                          : ""
                      }`}
                    >
                      <td className="px-4 py-2 tabular-nums">{fmtDateTimeIST(inc.ts)}</td>
                      <td className="px-4 py-2">{inc.kind}</td>
                      <td className="px-4 py-2">
                        <Badge colour={severityColour(inc.severity)}>{inc.severity}</Badge>
                      </td>
                      <td className="px-4 py-2 font-mono text-xs">{inc.plate ?? "—"}</td>
                      <td className="px-4 py-2">{inc.gate_id?.replace("G-", "") ?? "—"}</td>
                      <td className="px-4 py-2 text-xs text-muted-foreground">
                        {inc.rc?.owner_name_masked ?? "—"}
                      </td>
                      <td className="px-4 py-2 text-right">
                        {inc.evidence_url ? (
                          <span className="text-severity-info">{t("reports.photo")}</span>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </AsyncBoundary>
          </CardContent>
        </Card>
      </div>

      <IncidentDialog incident={selected} onClose={() => setSelected(null)} />

      {/* Driver Identity Verification — the existing IdentityPanel (capability
          C2) inside a centered modal. Verification logic is reused untouched;
          the shared Dialog provides the scrim, Esc + click-outside close, and
          the entrance animation. */}
      <Dialog open={identityOpen} onOpenChange={setIdentityOpen}>
        <DialogContent className="max-w-xl p-3">
          <DialogTitle className="sr-only">{t("reports.driverIdentityVerification")}</DialogTitle>
          <IdentityPanel />
        </DialogContent>
      </Dialog>

      {/* Vehicle Violation Detection — orchestration console. Same Dialog
          chrome; the panel reuses ANPR + vehicle/driver lookup + the e-Challan
          schedule and files incidents into jnpa.alerts (this very table). */}
      <Dialog open={violationOpen} onOpenChange={setViolationOpen}>
        <DialogContent className="max-w-xl p-3">
          <DialogTitle className="sr-only">
            {t("reports.vehicleViolationDetection", {
              defaultValue: "Vehicle Violation Detection",
            })}
          </DialogTitle>
          <ViolationDetectionPanel />
        </DialogContent>
      </Dialog>
    </div>
  );
}

function FilterSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: string[];
}) {
  const { t } = useTranslation();
  return (
    <label className="text-[11px] text-muted-foreground">
      {label}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 text-xs"
      >
        <option value="">{t("reports.all")}</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}

function FilterDate({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <label className="text-[11px] text-muted-foreground">
      {label}
      <input
        type="datetime-local"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 text-xs"
      />
    </label>
  );
}

function IncidentDialog({
  incident,
  onClose,
}: {
  incident: PoliceIncident | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const sev = incident ? severityColour(incident.severity) : undefined;
  const challan = incident?.challan;
  return (
    <Dialog open={!!incident} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        {incident && (
          <>
            <DialogHeader className="flex items-center gap-3">
              {/* Left severity rail — consistent with the alert evidence dialog. */}
              <span
                className="h-9 w-1 shrink-0 rounded-full"
                style={{ backgroundColor: sev }}
                aria-hidden
              />
              <DialogTitle className="flex min-w-0 flex-col gap-0.5">
                <span className="truncate text-base font-semibold leading-tight tracking-tight">
                  {t(`alertKind.${incident.kind}`, { defaultValue: incident.kind })}
                </span>
                <span className="font-mono text-xs font-medium text-muted-foreground">
                  {incident.plate ?? "—"}
                </span>
              </DialogTitle>
            </DialogHeader>
            <div className="space-y-4 p-5">
              <div className="grid grid-cols-2 gap-x-4 gap-y-3.5">
                <KV k={t("reports.incidentId")} v={incident.id} />
                <KV k={t("notifications.time")} v={fmtDateTimeIST(incident.ts)} />
                <KV k={t("reports.ownerMasked")} v={incident.rc?.owner_name_masked ?? "—"} />
                <KV k={t("reports.vehicleClass")} v={incident.rc?.vehicle_class ?? "—"} />
                <KV
                  k={t("reports.rtoState")}
                  v={`${incident.rc?.rto_code ?? "—"} / ${incident.rc?.state ?? "—"}`}
                />
                <KV k={t("reports.fastag")} v={incident.rc?.fastag_status ?? "—"} />
              </div>
              {incident.evidence_url && (
                <img
                  src={incident.evidence_url}
                  alt={t("reports.evidenceAlt")}
                  className="w-full rounded-lg border border-border shadow-sm"
                  onError={(e) => ((e.target as HTMLImageElement).style.display = "none")}
                />
              )}

              {/* Recommended action — the e-Challan payload as a readable list. */}
              {challan && Object.keys(challan).length > 0 && (
                <div className="overflow-hidden rounded-lg border border-severity-warning/40">
                  <div className="flex items-center gap-1.5 border-b border-severity-warning/30 bg-severity-warning/10 px-3.5 py-2.5 text-xs font-semibold">
                    <ReceiptText className="h-4 w-4 text-severity-warning" aria-hidden />
                    {t("reports.recommendedAction")}
                  </div>
                  <dl>
                    {Object.entries(challan).map(([key, value]) => (
                      <div
                        key={key}
                        className="flex items-start justify-between gap-3 border-b border-border/60 px-3.5 py-2 text-xs last:border-b-0"
                      >
                        <dt className="shrink-0 font-medium text-muted-foreground">
                          {humanizeKey(key)}
                        </dt>
                        <dd
                          className="min-w-0 break-words text-right font-mono font-medium text-foreground"
                          title={formatChallanValue(key, value)}
                        >
                          {formatChallanValue(key, value)}
                        </dd>
                      </div>
                    ))}
                  </dl>
                </div>
              )}

              <button
                type="button"
                onClick={() => void exportPolicePdf({ id: incident.id })}
                className="inline-flex w-full items-center justify-center gap-2 rounded-lg border border-severity-info/40 bg-severity-info/10 px-4 py-2.5 text-sm font-semibold text-severity-info transition-colors hover:bg-severity-info/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-severity-info/40"
              >
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
      <div className="mb-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
        {k}
      </div>
      <div className="break-words text-sm text-foreground">{v}</div>
    </div>
  );
}

/** "echallan_id" → "Echallan id" — readable label from a payload key. */
function humanizeKey(key: string): string {
  const spaced = key.replace(/[_-]+/g, " ").trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** Render a challan value readably — INR amounts as ₹, objects as JSON. */
function formatChallanValue(key: string, value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "number" && /amount|inr|fee|fine/i.test(key)) {
    return `₹${value.toLocaleString("en-IN")}`;
  }
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
