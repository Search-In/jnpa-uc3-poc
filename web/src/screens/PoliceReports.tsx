import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getAdapter } from "@/data";
import type { PoliceIncident } from "@/lib/types";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Spinner, EmptyState } from "@/components/ui/misc";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { severityColour } from "@/lib/palette";
import { fmtDateTimeIST } from "@/lib/utils";
import { FileDown, ExternalLink } from "lucide-react";

const KINDS = ["WRONG_WAY", "ILLEGAL_PARKING", "OVERSPEEDING", "ROUTE_DEVIATION"];
const GATES = ["G-NSICT", "G-JNPCT", "G-NSIGT", "G-BMCT"];
const SEVERITIES = ["info", "warning", "critical", "REPORT_TO_POLICE"];

// Tabular view of police-relevant alerts with filters (date / gate / severity /
// kind). "Export PDF" opens /api/reports/police?format=pdf (server-side
// Playwright) in a new tab — a one-page-per-incident report with evidence + a
// pre-filled e-Challan payload.
export default function PoliceReports() {
  const [kind, setKind] = useState<string>("");
  const [gate, setGate] = useState<string>("");
  const [severity, setSeverity] = useState<string>("");
  const [since, setSince] = useState<string>("");
  const [until, setUntil] = useState<string>("");
  const [selected, setSelected] = useState<PoliceIncident | null>(null);

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
      <div className="flex flex-wrap items-end justify-between gap-3 border-b border-border p-4">
        <div>
          <h1 className="text-lg font-semibold">Traffic-Police Reports</h1>
          <p className="text-sm text-muted-foreground">
            WRONG_WAY · ILLEGAL_PARKING · OVERSPEEDING · ROUTE_DEVIATION
          </p>
        </div>
        <a href={getAdapter().policePdfUrl(filters)} target="_blank" rel="noreferrer">
          <Button>
            <FileDown className="h-4 w-4" /> Export PDF
          </Button>
        </a>
      </div>

      {/* filters */}
      <div className="grid grid-cols-2 gap-3 border-b border-border p-4 md:grid-cols-5">
        <FilterSelect label="Kind" value={kind} onChange={setKind} options={KINDS} />
        <FilterSelect label="Gate" value={gate} onChange={setGate} options={GATES} />
        <FilterSelect label="Severity" value={severity} onChange={setSeverity} options={SEVERITIES} />
        <FilterDate label="From" value={since} onChange={setSince} />
        <FilterDate label="To" value={until} onChange={setUntil} />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <Card>
          <CardContent className="p-0">
            {q.isLoading ? (
              <div className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
                <Spinner /> loading incidents…
              </div>
            ) : incidents.length === 0 ? (
              <EmptyState>No incidents match these filters.</EmptyState>
            ) : (
              <table className="w-full text-sm">
                <thead className="border-b border-border text-left text-xs text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2">Time (IST)</th>
                    <th className="px-4 py-2">Kind</th>
                    <th className="px-4 py-2">Severity</th>
                    <th className="px-4 py-2">Plate</th>
                    <th className="px-4 py-2">Gate</th>
                    <th className="px-4 py-2">Owner</th>
                    <th className="px-4 py-2 text-right">Evidence</th>
                  </tr>
                </thead>
                <tbody>
                  {incidents.map((inc) => (
                    <tr
                      key={inc.id}
                      onClick={() => setSelected(inc)}
                      className="cursor-pointer border-b border-border/50 hover:bg-muted/40"
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
                        {inc.evidence_url ? <span className="text-severity-info">photo</span> : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </CardContent>
        </Card>
      </div>

      <IncidentDialog incident={selected} onClose={() => setSelected(null)} />
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
  return (
    <label className="text-[11px] text-muted-foreground">
      {label}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 text-xs"
      >
        <option value="">All</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}

function FilterDate({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
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

function IncidentDialog({ incident, onClose }: { incident: PoliceIncident | null; onClose: () => void }) {
  return (
    <Dialog open={!!incident} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        {incident && (
          <>
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <Badge colour={severityColour(incident.severity)}>{incident.kind}</Badge>
                <span className="font-mono text-sm">{incident.plate ?? "—"}</span>
              </DialogTitle>
            </DialogHeader>
            <div className="space-y-3 p-4 text-xs">
              <div className="grid grid-cols-2 gap-2">
                <KV k="Incident ID" v={incident.id} />
                <KV k="Time (IST)" v={fmtDateTimeIST(incident.ts)} />
                <KV k="Owner (masked)" v={incident.rc?.owner_name_masked ?? "—"} />
                <KV k="Vehicle class" v={incident.rc?.vehicle_class ?? "—"} />
                <KV k="RTO / State" v={`${incident.rc?.rto_code ?? "—"} / ${incident.rc?.state ?? "—"}`} />
                <KV k="FASTag" v={incident.rc?.fastag_status ?? "—"} />
              </div>
              {incident.evidence_url && (
                <img
                  src={incident.evidence_url}
                  alt="evidence"
                  className="w-full rounded-md border border-border"
                  onError={(e) => ((e.target as HTMLImageElement).style.display = "none")}
                />
              )}
              <div className="rounded-md border border-border p-3">
                <div className="mb-1 font-semibold">Recommended action — e-Challan</div>
                <pre className="overflow-auto rounded bg-muted p-2 text-[11px]">
                  {JSON.stringify(incident.challan, null, 2)}
                </pre>
              </div>
              <a
                href={getAdapter().policePdfUrl({ kind: incident.kind })}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-severity-info hover:underline"
              >
                <ExternalLink className="h-3.5 w-3.5" /> Open PDF for this kind
              </a>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

function KV({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{k}</div>
      <div className="break-all">{v}</div>
    </div>
  );
}
