/**
 * UC-3 Lifecycle Console — the single operational screen for the truck & gate
 * lifecycle described in the client documents (F-U3) and the empty-repositioning
 * chain (F-Y1).
 *
 * One journey view, no page jumping: search a container / truck / job, then work
 * the lifecycle from the same screen — assign, gate-in, yard pickup/drop, scan,
 * gate-out — with every step clickable for its detail, timestamps, documents and
 * available action.
 *
 * Deliberately independent of the Follow-The-Box screen (removed from scope): it
 * reads the real UC-III surface (/api/jobs, /api/gate, /api/yard, /api/scan,
 * /api/gate-docs, /api/cfs-ecy/chains) rather than the display-only journey
 * timeline.
 */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRight,
  Boxes,
  CheckCircle2,
  ClipboardList,
  Clock,
  DoorOpen,
  FileText,
  Loader2,
  PackageSearch,
  ScanLine,
  Search,
  Truck,
  XCircle,
} from "lucide-react";

import { api } from "../lib/api";
import type { ContainerJob, JobEvent, JobStatus } from "../lib/api";

import GateDocUploadPanel from "./gatedocs/UploadPanel";

type Tab = "lifecycle" | "documents" | "chains" | "upload";

const STEPS: { key: string; label: string; icon: typeof Truck; statuses: JobStatus[] }[] = [
  { key: "assignment", label: "Truck Assignment", icon: Truck, statuses: ["ASSIGNED", "ACCEPTED"] },
  { key: "gate_in", label: "Gate In (BAT lane)", icon: DoorOpen, statuses: ["AT_GATE"] },
  { key: "yard", label: "Yard Pickup / Drop", icon: Boxes, statuses: ["IN_YARD", "PICKED_UP", "DROPPED"] },
  { key: "scan", label: "RMS Scanner", icon: ScanLine, statuses: [] },
  { key: "gate_out", label: "Gate Out", icon: ArrowRight, statuses: ["COMPLETED"] },
];

const STATUS_RANK: Record<JobStatus, number> = {
  ASSIGNED: 1,
  ACCEPTED: 2,
  AT_GATE: 3,
  IN_YARD: 4,
  PICKED_UP: 5,
  DROPPED: 5,
  COMPLETED: 6,
  CANCELLED: 0,
};

function statusTone(s: JobStatus): string {
  if (s === "COMPLETED") return "bg-emerald-500/15 text-emerald-300 border-emerald-500/30";
  if (s === "CANCELLED") return "bg-rose-500/15 text-rose-300 border-rose-500/30";
  return "bg-sky-500/15 text-sky-300 border-sky-500/30";
}

function fmt(ts: string | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? String(ts) : d.toLocaleString();
}

export default function Uc3Lifecycle() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("lifecycle");
  const [term, setTerm] = useState("");
  const [selectedJob, setSelectedJob] = useState<number | null>(null);
  const [openStep, setOpenStep] = useState<string | null>(null);

  const jobsQ = useQuery({
    queryKey: ["uc3-jobs", term],
    queryFn: () =>
      api.jobs(term ? { container: term.toUpperCase(), limit: 25 } : { limit: 25 }),
  });

  const jobQ = useQuery({
    queryKey: ["uc3-job", selectedJob],
    queryFn: () => api.job(selectedJob as number),
    enabled: selectedJob !== null,
  });

  const job = jobQ.data;
  const container = job?.container_number || (term ? term.toUpperCase() : "");

  const scanQ = useQuery({
    queryKey: ["uc3-scan", container],
    queryFn: () => api.scanStatus(container),
    enabled: Boolean(container),
    retry: false,
  });

  const docsQ = useQuery({
    queryKey: ["uc3-docs", container],
    queryFn: () => api.gateDocsForContainer(container),
    enabled: Boolean(container) && tab === "documents",
  });

  const gateQ = useQuery({
    queryKey: ["uc3-gate", selectedJob],
    queryFn: () => api.gateEvents({ job_id: selectedJob as number }),
    enabled: selectedJob !== null,
  });

  const yardQ = useQuery({
    queryKey: ["uc3-yard", selectedJob],
    queryFn: () => api.yardMovements({ job_id: selectedJob as number }),
    enabled: selectedJob !== null,
  });

  const chainsQ = useQuery({
    queryKey: ["uc3-chains"],
    queryFn: () => api.ecyCfsChains({ limit: 50 }),
    enabled: tab === "chains",
  });

  const chainStatsQ = useQuery({
    queryKey: ["uc3-chain-stats"],
    queryFn: () => api.ecyCfsChainStats(),
    enabled: tab === "chains",
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["uc3-job"] });
    qc.invalidateQueries({ queryKey: ["uc3-jobs"] });
    qc.invalidateQueries({ queryKey: ["uc3-gate"] });
    qc.invalidateQueries({ queryKey: ["uc3-yard"] });
    qc.invalidateQueries({ queryKey: ["uc3-scan"] });
  };

  const act = useMutation({
    mutationFn: async (action: string) => {
      if (!job) throw new Error("no job selected");
      switch (action) {
        case "accept":
          return api.jobAccept(job.id);
        case "gate_in":
          return api.gateEventCreate({
            event_type: "GATE_IN",
            plate: job.vehicle_no || job.vehicle_id,
            gate_id: job.gate || undefined,
            job_id: job.id,
          });
        case "pickup":
          return api.yardMovementCreate({ movement_type: "YARD_PICKUP", job_id: job.id });
        case "drop":
          return api.yardMovementCreate({ movement_type: "YARD_DROP", job_id: job.id });
        case "scan_clean":
          return api.scanRecord({
            container_number: job.container_number as string,
            result: "SCANNED_CLEAN",
            machine_code: scanQ.data?.machine_code || undefined,
            job_id: job.id,
          });
        case "gate_out":
          return api.gateEventCreate({
            event_type: "GATE_OUT",
            plate: job.vehicle_no || job.vehicle_id,
            gate_id: job.gate || undefined,
            job_id: job.id,
          });
        default:
          throw new Error(`unknown action ${action}`);
      }
    },
    onSuccess: invalidate,
  });

  const rebuild = useMutation({
    mutationFn: () => api.ecyCfsChainRebuild(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["uc3-chains"] });
      qc.invalidateQueries({ queryKey: ["uc3-chain-stats"] });
    },
  });

  const rank = job ? STATUS_RANK[job.status] : 0;

  const stepState = useMemo(() => {
    const scan = scanQ.data;
    return STEPS.map((s, i) => {
      let done = false;
      let detail = "";
      if (s.key === "assignment") {
        done = Boolean(job);
        detail = job ? `${job.vehicle_no || job.vehicle_id} · ${job.move_type}` : "no job";
      } else if (s.key === "gate_in") {
        done = rank >= 3;
        const ev = gateQ.data?.items?.find((e: any) => e.event_type === "GATE_IN");
        detail = ev ? `${ev.gate_id || "gate"} · BAT ${ev.bat_lane || "—"}` : "not recorded";
      } else if (s.key === "yard") {
        done = rank >= 5;
        const m = yardQ.data?.items?.[0];
        detail = m ? `${m.movement_type} @ ${m.yard_location || "—"}` : "no movement";
      } else if (s.key === "scan") {
        done = Boolean(scan && scan.cleared);
        detail = scan
          ? scan.scan_required
            ? `${scan.result} · ${scan.machine_code || "machine n/a"}`
            : "not RMS-selected"
          : "unknown";
      } else if (s.key === "gate_out") {
        done = rank >= 6;
        const ev = gateQ.data?.items?.find((e: any) => e.event_type === "GATE_OUT");
        detail = ev ? `${ev.gate_id || "gate"} · ${fmt(ev.ts)}` : "not recorded";
      }
      return { ...s, done, detail, index: i };
    });
  }, [job, rank, scanQ.data, gateQ.data, yardQ.data]);

  const nextAction = useMemo(() => {
    if (!job || job.status === "COMPLETED" || job.status === "CANCELLED") return null;
    if (job.status === "ASSIGNED") return { action: "accept", label: "Accept job" };
    if (job.status === "ACCEPTED") return { action: "gate_in", label: "Record gate-in" };
    if (job.status === "AT_GATE" || job.status === "IN_YARD")
      return job.move_type === "EXPORT_DROP" || job.move_type === "EMPTY_DROP"
        ? { action: "drop", label: "Confirm yard drop" }
        : { action: "pickup", label: "Confirm yard pickup" };
    if (job.status === "PICKED_UP" || job.status === "DROPPED") {
      if (scanQ.data?.scan_required && !scanQ.data?.cleared)
        return { action: "scan_clean", label: "Record scan clean" };
      return { action: "gate_out", label: "Record gate-out" };
    }
    return null;
  }, [job, scanQ.data]);

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">UC-3 Lifecycle Console</h1>
          <p className="text-sm text-slate-400">
            Transporter → PDP → vehicle → job → gate document → gate-in → yard → scanner →
            release → gate-out, in one view.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {(["lifecycle", "documents", "chains", "upload"] as Tab[]).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`rounded-md px-3 py-1.5 text-sm capitalize ${
                tab === t
                  ? "bg-sky-500/20 text-sky-200 ring-1 ring-sky-500/40"
                  : "text-slate-400 hover:text-slate-200"
              }`}
            >
              {t === "chains" ? "ECY → CFS chains" : t === "upload" ? "Data upload" : t}
            </button>
          ))}
        </div>
      </header>

      {tab === "upload" && <GateDocUploadPanel />}

      {(tab === "lifecycle" || tab === "documents") && (
        <div className="flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2">
          <Search className="h-4 w-4 text-slate-500" />
          <input
            value={term}
            onChange={(e) => setTerm(e.target.value)}
            placeholder="Search container (e.g. MRKU5014206) or truck (MH43BX1488)"
            className="w-full bg-transparent text-sm text-slate-200 outline-none placeholder:text-slate-600"
          />
        </div>
      )}

      {tab === "lifecycle" && (
        <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
          {/* ---------------------------------------------------------- job list */}
          <section className="rounded-lg border border-slate-800 bg-slate-900/60">
            <h2 className="border-b border-slate-800 px-3 py-2 text-sm font-medium text-slate-300">
              <ClipboardList className="mr-1.5 inline h-4 w-4" />
              Container jobs
            </h2>
            <ul className="max-h-[520px] divide-y divide-slate-800/70 overflow-y-auto">
              {jobsQ.isLoading && (
                <li className="px-3 py-6 text-center text-sm text-slate-500">
                  <Loader2 className="mx-auto h-4 w-4 animate-spin" />
                </li>
              )}
              {jobsQ.data?.items?.length === 0 && (
                <li className="px-3 py-6 text-center text-sm text-slate-500">
                  No jobs. Assign a truck from Vehicle Management.
                </li>
              )}
              {jobsQ.data?.items?.map((j: ContainerJob) => (
                <li key={j.id}>
                  <button
                    onClick={() => {
                      setSelectedJob(j.id);
                      setOpenStep(null);
                    }}
                    className={`w-full px-3 py-2 text-left hover:bg-slate-800/50 ${
                      selectedJob === j.id ? "bg-slate-800/70" : ""
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono text-sm text-slate-200">
                        {j.container_number || j.group_code || `job #${j.id}`}
                      </span>
                      <span className={`rounded border px-1.5 py-0.5 text-[10px] ${statusTone(j.status)}`}>
                        {j.status}
                      </span>
                    </div>
                    <div className="mt-0.5 text-xs text-slate-500">
                      {j.vehicle_no || j.vehicle_id} · {j.move_type}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          </section>

          {/* ------------------------------------------------------- lifecycle */}
          <section className="rounded-lg border border-slate-800 bg-slate-900/60 p-4">
            {!job && (
              <p className="py-16 text-center text-sm text-slate-500">
                <PackageSearch className="mx-auto mb-2 h-6 w-6" />
                Select a job to open its lifecycle.
              </p>
            )}

            {job && (
              <>
                <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <div className="font-mono text-lg text-slate-100">
                      {job.container_number || job.group_code}
                    </div>
                    <div className="text-xs text-slate-400">
                      job #{job.id} · {job.vehicle_no || job.vehicle_id} ·{" "}
                      {job.driver_licence || "no driver"} · {job.terminal || "terminal n/a"}
                    </div>
                  </div>
                  {nextAction && (
                    <button
                      onClick={() => act.mutate(nextAction.action)}
                      disabled={act.isPending}
                      className="rounded-md bg-sky-500/20 px-3 py-1.5 text-sm text-sky-200 ring-1 ring-sky-500/40 hover:bg-sky-500/30 disabled:opacity-50"
                    >
                      {act.isPending ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        nextAction.label
                      )}
                    </button>
                  )}
                </div>

                {act.isError && (
                  <p className="mb-3 rounded border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
                    {String((act.error as Error).message)}
                  </p>
                )}

                <ol className="space-y-2">
                  {stepState.map((s) => {
                    const Icon = s.icon;
                    return (
                      <li key={s.key}>
                        <button
                          onClick={() => setOpenStep(openStep === s.key ? null : s.key)}
                          className="flex w-full items-center gap-3 rounded-md border border-slate-800 bg-slate-950/40 px-3 py-2 text-left hover:border-slate-700"
                        >
                          <span
                            className={`flex h-7 w-7 items-center justify-center rounded-full ${
                              s.done ? "bg-emerald-500/20 text-emerald-300" : "bg-slate-800 text-slate-500"
                            }`}
                          >
                            {s.done ? <CheckCircle2 className="h-4 w-4" /> : <Icon className="h-4 w-4" />}
                          </span>
                          <span className="flex-1">
                            <span className="block text-sm text-slate-200">{s.label}</span>
                            <span className="block text-xs text-slate-500">{s.detail}</span>
                          </span>
                        </button>

                        {openStep === s.key && (
                          <div className="mt-1 rounded-md border border-slate-800 bg-slate-950/70 px-3 py-2 text-xs text-slate-400">
                            {s.key === "assignment" && (
                              <dl className="grid grid-cols-2 gap-1">
                                <dt>Assigned</dt>
                                <dd className="text-slate-300">{fmt(job.assigned_at)}</dd>
                                <dt>Accepted</dt>
                                <dd className="text-slate-300">{fmt(job.accepted_at)}</dd>
                                <dt>Move type</dt>
                                <dd className="text-slate-300">{job.move_type}</dd>
                                <dt>Document</dt>
                                <dd className="text-slate-300">
                                  {job.document_type
                                    ? `${job.document_type} ${job.document_reference || ""}`
                                    : "—"}
                                </dd>
                              </dl>
                            )}
                            {s.key === "gate_in" && (
                              <ul className="space-y-1">
                                {gateQ.data?.items?.map((e: any) => (
                                  <li key={e.id} className="font-mono">
                                    {e.event_type} · {fmt(e.ts)} · gate {e.gate_id || "—"} · BAT{" "}
                                    {e.bat_lane || "—"}
                                  </li>
                                )) || <li>no crossings recorded</li>}
                              </ul>
                            )}
                            {s.key === "yard" && (
                              <ul className="space-y-1">
                                {yardQ.data?.items?.map((m: any) => (
                                  <li key={m.id} className="font-mono">
                                    {m.movement_type} · {fmt(m.occurred_at)} ·{" "}
                                    {m.yard_location || "location n/a"}
                                  </li>
                                )) || <li>no yard movements</li>}
                              </ul>
                            )}
                            {s.key === "scan" && (
                              <dl className="grid grid-cols-2 gap-1">
                                <dt>RMS selected</dt>
                                <dd className="text-slate-300">
                                  {scanQ.data?.scan_required ? "yes" : "no"}
                                </dd>
                                <dt>Machine</dt>
                                <dd className="text-slate-300">
                                  {scanQ.data?.machine_code || "—"} (
                                  {scanQ.data?.machine_class || "n/a"})
                                </dd>
                                <dt>Result</dt>
                                <dd className="text-slate-300">{scanQ.data?.result || "—"}</dd>
                              </dl>
                            )}
                            {s.key === "gate_out" && (
                              <dl className="grid grid-cols-2 gap-1">
                                <dt>Completed</dt>
                                <dd className="text-slate-300">{fmt(job.completed_at)}</dd>
                                <dt>Status</dt>
                                <dd className="text-slate-300">{job.status}</dd>
                              </dl>
                            )}
                          </div>
                        )}
                      </li>
                    );
                  })}
                </ol>

                <h3 className="mt-4 text-xs font-medium uppercase tracking-wide text-slate-500">
                  Audit history
                </h3>
                <ul className="mt-1 space-y-1 text-xs text-slate-400">
                  {job.events?.map((e: JobEvent) => (
                    <li key={e.id} className="flex items-center gap-2">
                      <Clock className="h-3 w-3 text-slate-600" />
                      <span className="font-mono text-slate-300">{e.event}</span>
                      {e.old_status && (
                        <span>
                          {e.old_status} → {e.new_status}
                        </span>
                      )}
                      <span className="text-slate-600">{fmt(e.created_at)}</span>
                      {e.actor && <span className="text-slate-600">by {e.actor}</span>}
                    </li>
                  ))}
                </ul>
              </>
            )}
          </section>
        </div>
      )}

      {/* ------------------------------------------------------------ documents */}
      {tab === "documents" && (
        <section className="rounded-lg border border-slate-800 bg-slate-900/60 p-4">
          {!container && (
            <p className="py-12 text-center text-sm text-slate-500">
              Search a container number to see its gate documents.
            </p>
          )}
          {container && docsQ.isLoading && (
            <Loader2 className="mx-auto h-5 w-5 animate-spin text-slate-500" />
          )}
          {docsQ.data && (
            <div className="space-y-4">
              {(["eir", "pin", "form13"] as const).map((kind) => (
                <div key={kind}>
                  <h3 className="mb-1 text-sm font-medium uppercase tracking-wide text-slate-400">
                    <FileText className="mr-1 inline h-4 w-4" />
                    {kind === "form13" ? "Form 13" : kind.toUpperCase()} ({docsQ.data[kind].length})
                  </h3>
                  {docsQ.data[kind].length === 0 ? (
                    <p className="text-xs text-slate-600">none</p>
                  ) : (
                    <div className="overflow-x-auto">
                      <table className="w-full text-left text-xs">
                        <tbody className="divide-y divide-slate-800">
                          {docsQ.data[kind].map((d: any) => (
                            <tr key={d.id} className="text-slate-300">
                              <td className="py-1 pr-3 font-mono">
                                {d.eir_no || d.pin_number || d.form13_no || `#${d.id}`}
                              </td>
                              <td className="py-1 pr-3">{d.terminal || "—"}</td>
                              <td className="py-1 pr-3 font-mono">
                                {d.truck_no || d.vehicle_no || "—"}
                              </td>
                              <td className="py-1 pr-3">
                                {d.tat_minutes != null ? `${d.tat_minutes} min TAT` : ""}
                                {d.yard_location ? `yard ${d.yard_location}` : ""}
                                {d.visit_id ? `visit ${d.visit_id}` : ""}
                              </td>
                              <td className="py-1 text-slate-500">
                                {fmt(d.truck_in_time || d.issued_at)}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </section>
      )}

      {/* --------------------------------------------------------------- chains */}
      {tab === "chains" && (
        <section className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              {[
                ["Chains", chainStatsQ.data?.chains],
                ["Complete", chainStatsQ.data?.complete_chains],
                ["Anomalies", chainStatsQ.data?.anomaly_chains],
                ["Avg cycle (h)", chainStatsQ.data?.avg_cycle_hours],
              ].map(([label, value]) => (
                <div
                  key={String(label)}
                  className="rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2"
                >
                  <div className="text-xs text-slate-500">{label}</div>
                  <div className="text-lg text-slate-100">{value ?? "—"}</div>
                </div>
              ))}
            </div>
            <button
              onClick={() => rebuild.mutate()}
              disabled={rebuild.isPending}
              className="rounded-md bg-sky-500/20 px-3 py-1.5 text-sm text-sky-200 ring-1 ring-sky-500/40 hover:bg-sky-500/30 disabled:opacity-50"
            >
              {rebuild.isPending ? "Rebuilding…" : "Rebuild chains"}
            </button>
          </div>

          <div className="overflow-x-auto rounded-lg border border-slate-800 bg-slate-900/60">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-slate-800 text-xs uppercase text-slate-500">
                <tr>
                  <th className="px-3 py-2">Container</th>
                  <th className="px-3 py-2">ECY out</th>
                  <th className="px-3 py-2">CFS in</th>
                  <th className="px-3 py-2">CFS out</th>
                  <th className="px-3 py-2">Transit h</th>
                  <th className="px-3 py-2">Dwell h</th>
                  <th className="px-3 py-2">Cycle h</th>
                  <th className="px-3 py-2">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {chainsQ.data?.items?.map((c) => (
                  <tr key={c.id} className="text-slate-300">
                    <td className="px-3 py-1.5 font-mono">
                      {c.container_number}
                      {c.has_anomaly && (
                        <span
                          title={(c.anomaly_labels || c.anomaly_codes).join("; ")}
                          className="ml-1.5 inline-flex items-center rounded border border-amber-500/40 bg-amber-500/10 px-1 text-[10px] text-amber-300"
                        >
                          <AlertTriangle className="mr-0.5 h-3 w-3" />
                          {c.anomaly_codes.length}
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-1.5 text-xs">{fmt(c.ecy_out_ts)}</td>
                    <td className="px-3 py-1.5 text-xs">{fmt(c.cfs_in_ts)}</td>
                    <td className="px-3 py-1.5 text-xs">{fmt(c.cfs_out_ts)}</td>
                    <td className="px-3 py-1.5">{c.transit_hours ?? "—"}</td>
                    <td className="px-3 py-1.5">{c.dwell_hours ?? "—"}</td>
                    <td className="px-3 py-1.5">{c.cycle_hours ?? "—"}</td>
                    <td className="px-3 py-1.5">
                      <span
                        className={`rounded border px-1.5 py-0.5 text-[10px] ${
                          c.chain_status === "COMPLETE"
                            ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
                            : "border-slate-600 bg-slate-800 text-slate-400"
                        }`}
                      >
                        {c.chain_status}
                      </span>
                    </td>
                  </tr>
                ))}
                {chainsQ.data?.items?.length === 0 && (
                  <tr>
                    <td colSpan={8} className="px-3 py-8 text-center text-sm text-slate-500">
                      No chains yet — run “Rebuild chains” after importing CODECO movements.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {chainStatsQ.data?.by_anomaly?.length ? (
            <div className="rounded-lg border border-slate-800 bg-slate-900/60 p-3 text-xs text-slate-400">
              <div className="mb-1 font-medium text-slate-300">
                <XCircle className="mr-1 inline h-3.5 w-3.5" />
                Detected anomalies
              </div>
              <ul className="space-y-0.5">
                {chainStatsQ.data.by_anomaly.map((a) => (
                  <li key={a.code}>
                    <span className="font-mono text-amber-300">{a.code}</span> — {a.chains} chain(s):{" "}
                    {chainStatsQ.data?.anomaly_labels?.[a.code]}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </section>
      )}
    </div>
  );
}
