// Accident Handling (Feature 1) — RDS-backed incident dashboard over
// /api/accidents/*. Report a premises/enroute accident, triage severity,
// drive the investigation → resolution lifecycle, and read the per-accident
// timeline (status/investigation/resolution audit trail). Built on the DTCCC
// kit (KPI cards, badges, tables) to match the rest of the console.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { TriangleAlert, Plus, ListChecks, Activity, ShieldAlert } from "lucide-react";
import { api } from "@/lib/api";
import {
  PageContainer,
  PageHeader,
  StatGrid,
  StatCard,
  StatusChip,
  type Tone,
} from "@/components/ui/dtccc";
import { Card } from "@/components/ui/card";
import { EmptyState, LoadingState } from "@/components/ui/misc";
import { STATUS } from "@/lib/tokens";
import { fmtDateTimeIST } from "@/lib/utils";

const ACCIDENT_TYPES = ["PREMISES", "ENROUTE"] as const;
const SEVERITIES = ["MINOR", "MODERATE", "MAJOR", "FATAL"] as const;

// Severity → badge tone. FATAL/MAJOR read as critical, MODERATE warns, MINOR ok.
function severityTone(sev?: string | null): Tone {
  const s = String(sev ?? "").toUpperCase();
  if (s === "FATAL" || s === "MAJOR") return "critical";
  if (s === "MODERATE") return "warn";
  if (s === "MINOR") return "ok";
  return "neutral";
}

// Status → badge tone. OPEN/REPORTED are active, RESOLVED/CLOSED are done.
function statusTone(status?: string | null): Tone {
  const s = String(status ?? "").toUpperCase();
  if (s === "RESOLVED" || s === "CLOSED") return "ok";
  if (s === "UNDER_INVESTIGATION" || s === "INVESTIGATING") return "info";
  if (s === "OPEN" || s === "REPORTED") return "warn";
  return "neutral";
}

export default function Accidents() {
  const qc = useQueryClient();

  const [statusFilter, setStatusFilter] = useState<string>("");
  const [selectedId, setSelectedId] = useState<string | number | null>(null);

  const dashQ = useQuery({
    queryKey: ["accident-dashboard"],
    queryFn: () => api.accidentDashboard(),
  });
  const listQ = useQuery({
    queryKey: ["accidents", statusFilter],
    queryFn: () => api.accidents(statusFilter ? { status: statusFilter } : {}),
  });
  const detailQ = useQuery({
    queryKey: ["accident", selectedId],
    queryFn: () => api.accident(selectedId as string | number),
    enabled: selectedId != null,
  });

  const dash = dashQ.data;
  const accidents: any[] = listQ.data?.accidents ?? [];
  const unreachable = dashQ.isError && listQ.isError;

  const majorFatal = useMemo(() => {
    const bySev = dash?.by_severity ?? {};
    return (bySev.MAJOR ?? 0) + (bySev.FATAL ?? 0);
  }, [dash]);

  const topType = useMemo(() => {
    const byType = dash?.by_type ?? {};
    const entries = Object.entries(byType) as [string, number][];
    if (!entries.length) return null;
    return entries.sort((a, b) => b[1] - a[1])[0];
  }, [dash]);

  function invalidateAll() {
    void qc.invalidateQueries({ queryKey: ["accident-dashboard"] });
    void qc.invalidateQueries({ queryKey: ["accidents"] });
    if (selectedId != null) void qc.invalidateQueries({ queryKey: ["accident", selectedId] });
  }

  // ---- Report accident form ----
  const [form, setForm] = useState({
    accident_type: "PREMISES",
    severity: "MINOR",
    plate: "",
    vehicle_id: "",
    description: "",
  });
  function setField(k: keyof typeof form, v: string) {
    setForm((f) => ({ ...f, [k]: v }));
  }
  const report = useMutation({
    mutationFn: () =>
      api.accidentReport({
        accident_type: form.accident_type,
        severity: form.severity,
        plate: form.plate || undefined,
        vehicle_id: form.vehicle_id || undefined,
        description: form.description || undefined,
      }),
    onSuccess: () => {
      setForm({
        accident_type: "PREMISES",
        severity: "MINOR",
        plate: "",
        vehicle_id: "",
        description: "",
      });
      invalidateAll();
    },
  });

  // ---- Lifecycle mutations (operate on selected accident) ----
  const startInvestigation = useMutation({
    mutationFn: (id: string | number) =>
      api.accidentInvestigation(id, {
        investigation_status: "IN_PROGRESS",
        note: "Investigation started",
      }),
    onSuccess: () => invalidateAll(),
  });
  const [resolution, setResolution] = useState("");
  const resolve = useMutation({
    mutationFn: (id: string | number) =>
      api.accidentResolve(id, { resolution: resolution || "Resolved", actor: "console" }),
    onSuccess: () => {
      setResolution("");
      invalidateAll();
    },
  });

  return (
    <PageContainer>
      <PageHeader
        icon={ShieldAlert}
        title="Accident Handling"
        subtitle="Premises & en-route incidents · investigation → resolution lifecycle"
        updatedAt={dashQ.dataUpdatedAt}
        isFetching={dashQ.isFetching && !dashQ.isLoading}
        onRefresh={invalidateAll}
      />

      {unreachable ? (
        <div className="px-4 py-3">
          <EmptyState>
            Accident service unreachable — start the gateway to report and triage accidents.
          </EmptyState>
        </div>
      ) : (
        <>
          {/* ---------------- KPI strip ---------------- */}
          <div className="px-4 pt-3">
            <StatGrid className="lg:grid-cols-4">
              <StatCard
                icon={TriangleAlert}
                label="Total Accidents"
                value={dash?.total ?? "—"}
                tone="info"
                loading={dashQ.isLoading}
              />
              <StatCard
                icon={Activity}
                label="Open"
                value={dash?.open ?? "—"}
                tone={(dash?.open ?? 0) > 0 ? "warn" : "ok"}
                loading={dashQ.isLoading}
              />
              <StatCard
                icon={ShieldAlert}
                label="Major / Fatal"
                value={majorFatal}
                tone={majorFatal > 0 ? "critical" : "ok"}
                loading={dashQ.isLoading}
              />
              <StatCard
                icon={ListChecks}
                label="Top Type"
                value={topType ? topType[0] : "—"}
                sub={topType ? `${topType[1]} incident(s)` : undefined}
                tone="neutral"
                loading={dashQ.isLoading}
              />
            </StatGrid>
          </div>

          <div className="grid grid-cols-1 gap-3 px-4 py-3 lg:grid-cols-3">
            {/* ---------------- Report accident ---------------- */}
            <Card className="p-4">
              <div className="mb-3 flex items-center gap-2">
                <Plus size={15} />
                <h3 className="text-sm font-semibold">Report accident</h3>
              </div>
              <div className="space-y-3 text-sm">
                <div className="grid grid-cols-2 gap-2">
                  <label className="flex flex-col gap-0.5">
                    <span className="text-[10px] text-muted-foreground">Type</span>
                    <select
                      value={form.accident_type}
                      onChange={(e) => setField("accident_type", e.target.value)}
                      className="rounded-md border border-border bg-card px-2 py-1.5"
                    >
                      {ACCIDENT_TYPES.map((t) => (
                        <option key={t} value={t}>
                          {t}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="flex flex-col gap-0.5">
                    <span className="text-[10px] text-muted-foreground">Severity</span>
                    <select
                      value={form.severity}
                      onChange={(e) => setField("severity", e.target.value)}
                      className="rounded-md border border-border bg-card px-2 py-1.5"
                    >
                      {SEVERITIES.map((s) => (
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
                <label className="flex flex-col gap-0.5">
                  <span className="text-[10px] text-muted-foreground">Plate</span>
                  <input
                    value={form.plate}
                    onChange={(e) => setField("plate", e.target.value)}
                    placeholder="e.g. MH04AB1234"
                    className="rounded-md border border-border bg-card px-2 py-1.5 outline-none"
                  />
                </label>
                <label className="flex flex-col gap-0.5">
                  <span className="text-[10px] text-muted-foreground">Vehicle ID</span>
                  <input
                    value={form.vehicle_id}
                    onChange={(e) => setField("vehicle_id", e.target.value)}
                    placeholder="internal vehicle id"
                    className="rounded-md border border-border bg-card px-2 py-1.5 outline-none"
                  />
                </label>
                <label className="flex flex-col gap-0.5">
                  <span className="text-[10px] text-muted-foreground">Description</span>
                  <textarea
                    value={form.description}
                    onChange={(e) => setField("description", e.target.value)}
                    placeholder="What happened?"
                    rows={3}
                    className="rounded-md border border-border bg-card px-2 py-1.5 outline-none"
                  />
                </label>
                <button
                  disabled={report.isPending}
                  onClick={() => report.mutate()}
                  className="rounded-md bg-primary px-3 py-1.5 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                >
                  {report.isPending ? "Reporting…" : "Report accident"}
                </button>
                {report.isError && (
                  <div className="text-[11px]" style={{ color: STATUS.critical }}>
                    {(report.error as Error)?.message}
                  </div>
                )}
              </div>
            </Card>

            {/* ---------------- Accident list ---------------- */}
            <Card className="p-4 lg:col-span-2">
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <ListChecks size={15} />
                <h3 className="text-sm font-semibold">Accidents</h3>
                <span className="text-[11px] text-muted-foreground">
                  ({listQ.data?.count ?? accidents.length})
                </span>
                <div className="ml-auto">
                  <select
                    value={statusFilter}
                    onChange={(e) => setStatusFilter(e.target.value)}
                    className="rounded-md border border-border bg-card px-2 py-1 text-[12px]"
                  >
                    <option value="">All statuses</option>
                    <option value="OPEN">Open</option>
                    <option value="UNDER_INVESTIGATION">Under investigation</option>
                    <option value="RESOLVED">Resolved</option>
                    <option value="CLOSED">Closed</option>
                  </select>
                </div>
              </div>
              {listQ.isLoading ? (
                <LoadingState />
              ) : !accidents.length ? (
                <EmptyState>No accidents for this filter.</EmptyState>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[640px] border-collapse text-[12px]">
                    <thead>
                      <tr className="text-left text-muted-foreground">
                        <th className="py-1 pr-3 font-medium">Ref</th>
                        <th className="py-1 pr-3 font-medium">When</th>
                        <th className="py-1 pr-3 font-medium">Type</th>
                        <th className="py-1 pr-3 font-medium">Severity</th>
                        <th className="py-1 pr-3 font-medium">Status</th>
                        <th className="py-1 pr-3 font-medium">Plate</th>
                        <th className="py-1 pr-3 font-medium"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {accidents.map((a) => (
                        <tr key={a.id} className="border-t border-border align-top">
                          <td className="py-1.5 pr-3 font-mono text-[11px]">
                            {a.accident_ref ?? a.id}
                          </td>
                          <td className="py-1.5 pr-3 whitespace-nowrap text-muted-foreground">
                            {a.occurred_at ? fmtDateTimeIST(a.occurred_at) : "—"}
                          </td>
                          <td className="py-1.5 pr-3">{a.accident_type ?? "—"}</td>
                          <td className="py-1.5 pr-3">
                            <StatusChip label={a.severity ?? "—"} tone={severityTone(a.severity)} />
                          </td>
                          <td className="py-1.5 pr-3">
                            <StatusChip label={a.status ?? "—"} tone={statusTone(a.status)} />
                          </td>
                          <td className="py-1.5 pr-3 font-mono text-[11px]">{a.plate ?? "—"}</td>
                          <td className="py-1.5 pr-3">
                            <button
                              onClick={() => setSelectedId(a.id)}
                              className="text-[11px] font-semibold text-primary hover:underline"
                            >
                              View
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          </div>

          {/* ---------------- Selected accident detail + timeline ---------------- */}
          {selectedId != null && (
            <div className="px-4 pb-4">
              <Card className="p-4">
                <div className="mb-3 flex flex-wrap items-center gap-2">
                  <Activity size={15} />
                  <h3 className="text-sm font-semibold">Accident detail</h3>
                  <button
                    onClick={() => setSelectedId(null)}
                    className="ml-auto text-[11px] text-muted-foreground hover:text-foreground"
                  >
                    Close
                  </button>
                </div>

                {detailQ.isLoading ? (
                  <LoadingState />
                ) : detailQ.isError || !detailQ.data?.accident ? (
                  <EmptyState>Could not load this accident.</EmptyState>
                ) : (
                  <AccidentDetail
                    detail={detailQ.data}
                    onStartInvestigation={() => startInvestigation.mutate(selectedId)}
                    startPending={startInvestigation.isPending}
                    resolution={resolution}
                    setResolution={setResolution}
                    onResolve={() => resolve.mutate(selectedId)}
                    resolvePending={resolve.isPending}
                  />
                )}
              </Card>
            </div>
          )}
        </>
      )}
    </PageContainer>
  );
}

function AccidentDetail({
  detail,
  onStartInvestigation,
  startPending,
  resolution,
  setResolution,
  onResolve,
  resolvePending,
}: {
  detail: any;
  onStartInvestigation: () => void;
  startPending: boolean;
  resolution: string;
  setResolution: (v: string) => void;
  onResolve: () => void;
  resolvePending: boolean;
}) {
  const a = detail.accident ?? {};
  const timeline: any[] = detail.timeline ?? [];
  const isResolved = ["RESOLVED", "CLOSED"].includes(String(a.status ?? "").toUpperCase());

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
      {/* Summary + actions */}
      <div className="space-y-3 lg:col-span-1">
        <div className="flex flex-wrap items-center gap-2">
          <StatusChip label={a.severity ?? "—"} tone={severityTone(a.severity)} />
          <StatusChip label={a.status ?? "—"} tone={statusTone(a.status)} />
        </div>
        <div className="space-y-1 text-[12px]">
          <div>
            <span className="text-muted-foreground">Ref: </span>
            <span className="font-mono">{a.accident_ref ?? a.id}</span>
          </div>
          <div>
            <span className="text-muted-foreground">Type: </span>
            {a.accident_type ?? "—"}
          </div>
          <div>
            <span className="text-muted-foreground">When: </span>
            {a.occurred_at ? fmtDateTimeIST(a.occurred_at) : "—"}
          </div>
          <div>
            <span className="text-muted-foreground">Plate: </span>
            <span className="font-mono">{a.plate ?? "—"}</span>
          </div>
          <div>
            <span className="text-muted-foreground">Investigation: </span>
            {a.investigation_status ?? "—"}
          </div>
          {a.description && (
            <div className="pt-1 text-foreground">{a.description}</div>
          )}
          {a.resolution && (
            <div className="pt-1">
              <span className="text-muted-foreground">Resolution: </span>
              {a.resolution}
            </div>
          )}
        </div>

        <div className="space-y-2 border-t border-border pt-3">
          <button
            disabled={startPending || isResolved}
            onClick={onStartInvestigation}
            className="w-full rounded-md border border-border px-3 py-1.5 text-[13px] font-semibold hover:bg-muted disabled:opacity-50"
          >
            {startPending ? "Starting…" : "Start Investigation"}
          </button>
          <div className="space-y-1.5">
            <input
              value={resolution}
              onChange={(e) => setResolution(e.target.value)}
              placeholder="Resolution summary"
              disabled={isResolved}
              className="w-full rounded-md border border-border bg-card px-2 py-1.5 text-[13px] outline-none disabled:opacity-50"
            />
            <button
              disabled={resolvePending || isResolved}
              onClick={onResolve}
              className="w-full rounded-md bg-primary px-3 py-1.5 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {resolvePending ? "Resolving…" : isResolved ? "Resolved" : "Resolve"}
            </button>
          </div>
        </div>
      </div>

      {/* Timeline */}
      <div className="lg:col-span-2">
        <h4 className="mb-2 text-[12px] font-semibold text-muted-foreground uppercase tracking-wide">
          Timeline
        </h4>
        {!timeline.length ? (
          <EmptyState>No timeline entries yet.</EmptyState>
        ) : (
          <ol className="space-y-3">
            {timeline.map((t, i) => (
              <li key={i} className="relative pl-5">
                <span
                  className="absolute left-0 top-1.5 h-2 w-2 rounded-full"
                  style={{ background: STATUS.info }}
                />
                {i < timeline.length - 1 && (
                  <span className="absolute left-[3px] top-3.5 bottom-[-12px] w-px bg-border" />
                )}
                <div className="text-[12px]">
                  <span className="font-medium">{t.action ?? "update"}</span>
                  {(t.old_status || t.new_status) && (
                    <span className="text-muted-foreground">
                      {" "}
                      · {t.old_status ?? "—"} → {t.new_status ?? "—"}
                    </span>
                  )}
                </div>
                {t.note && <div className="text-[11px] text-muted-foreground">{t.note}</div>}
                <div className="text-[10px] text-muted-foreground">
                  {t.actor ? `${t.actor} · ` : ""}
                  {t.created_at ? fmtDateTimeIST(t.created_at) : ""}
                </div>
              </li>
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}
