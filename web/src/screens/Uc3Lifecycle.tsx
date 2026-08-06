// UC-3 Lifecycle Console — the single operational screen for the truck & gate
// lifecycle (F-U3) and the empty-repositioning chain (F-Y1).
//
// One journey view, no page jumping: search a container or truck, then work the
// lifecycle from the same screen — assign, gate-in, yard pickup/drop, scan,
// gate-out — with every step expandable for its detail, timestamps, documents
// and available action.
//
// Composes the DTCCC kit exactly like CfsEcyMovements / DriverMaster
// (PageContainer / PageHeader / StatGrid / StatCard / SegmentedTabs /
// SearchInput / StatusChip / Card / Button / LoadingState / ErrorState /
// EmptyState) — this screen defines NO design system of its own and uses only
// semantic theme tokens (bg-card, text-foreground, border-border, text-primary),
// never hard-coded palette classes.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRight,
  Boxes,
  CheckCircle2,
  ClipboardList,
  Container,
  DoorOpen,
  FileText,
  Inbox,
  Link2,
  PackageSearch,
  RotateCcw,
  ScanLine,
  Timer,
  Truck,
  UploadCloud,
  Workflow,
} from "lucide-react";

import {
  PageContainer,
  PageHeader,
  StatGrid,
  StatCard,
  SegmentedTabs,
  SearchInput,
  StatusChip,
  Embedded,
  type Tone,
} from "@/components/ui/dtccc";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/misc";

import { api } from "../lib/api";
import type { ContainerJob, EcyCfsChain, JobEvent, JobStatus } from "../lib/api";
import JobAssignPanel from "@/components/uc3/JobAssignPanel";
import GateDocUploadPanel from "./gatedocs/UploadPanel";
import DocumentOCR from "@/screens/DocumentOCR";

type Tab = "lifecycle" | "documents" | "chains" | "upload";

// --- lifecycle model ---------------------------------------------------------

const STEPS = [
  { key: "assignment", label: "Assignment", icon: Truck },
  { key: "gate_in", label: "Gate In", icon: DoorOpen },
  { key: "yard_pickup", label: "Yard Pickup", icon: Boxes },
  { key: "yard_drop", label: "Yard Drop", icon: Boxes },
  { key: "scan", label: "Scanner", icon: ScanLine },
  { key: "gate_out", label: "Gate Out", icon: ArrowRight },
] as const;

type StepKey = (typeof STEPS)[number]["key"];

const STATUS_RANK: Record<JobStatus, number> = {
  CANCELLED: 0,
  ASSIGNED: 1,
  ACCEPTED: 2,
  AT_GATE: 3,
  IN_YARD: 4,
  PICKED_UP: 5,
  DROPPED: 5,
  COMPLETED: 6,
};

function statusTone(s: JobStatus): Tone {
  if (s === "COMPLETED") return "ok";
  if (s === "CANCELLED") return "critical";
  if (s === "ASSIGNED") return "neutral";
  return "info";
}

function fmt(ts: string | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? String(ts) : d.toLocaleString();
}

function isDropMove(move?: string | null): boolean {
  return move === "EXPORT_DROP" || move === "EMPTY_DROP";
}

export default function Uc3Lifecycle() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("lifecycle");
  const [term, setTerm] = useState("");
  const [selectedJob, setSelectedJob] = useState<number | null>(null);
  const [openStep, setOpenStep] = useState<StepKey | null>(null);

  const jobsQ = useQuery({
    queryKey: ["uc3-jobs", term],
    queryFn: () => api.jobs(term ? { container: term.toUpperCase(), limit: 50 } : { limit: 50 }),
    placeholderData: keepPreviousData,
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
    ["uc3-job", "uc3-jobs", "uc3-gate", "uc3-yard", "uc3-scan"].forEach((k) =>
      qc.invalidateQueries({ queryKey: [k] }),
    );
  };

  const act = useMutation({
    mutationFn: async (action: string) => {
      if (!job) throw new Error("no job selected");
      const plate = job.vehicle_no || job.vehicle_id;
      switch (action) {
        case "accept":
          return api.jobAccept(job.id);
        case "gate_in":
          return api.gateEventCreate({
            event_type: "GATE_IN",
            plate,
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
            plate,
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
  const gateIn = gateQ.data?.items?.find((e: { event_type: string }) => e.event_type === "GATE_IN");
  const gateOut = gateQ.data?.items?.find(
    (e: { event_type: string }) => e.event_type === "GATE_OUT",
  );
  const pickup = yardQ.data?.items?.find(
    (m: { movement_type: string }) => m.movement_type === "YARD_PICKUP",
  );
  const drop = yardQ.data?.items?.find(
    (m: { movement_type: string }) => m.movement_type === "YARD_DROP",
  );

  const steps = useMemo(() => {
    const scan = scanQ.data;
    return STEPS.map((s) => {
      let done = false;
      let detail = "not recorded";
      switch (s.key) {
        case "assignment":
          done = Boolean(job);
          detail = job ? `${job.vehicle_no || job.vehicle_id} · ${job.move_type}` : "no job";
          break;
        case "gate_in":
          done = rank >= 3;
          if (gateIn) detail = `${gateIn.gate_id || "gate"} · BAT ${gateIn.bat_lane || "—"}`;
          break;
        case "yard_pickup":
          done = Boolean(pickup);
          detail = pickup ? `${pickup.yard_location || "location n/a"}` : "not recorded";
          break;
        case "yard_drop":
          done = Boolean(drop);
          detail = drop ? `${drop.yard_location || "location n/a"}` : "not recorded";
          break;
        case "scan":
          done = Boolean(scan && scan.cleared);
          detail = scan
            ? scan.scan_required
              ? `${scan.result} · ${scan.machine_code || "machine n/a"}`
              : "not RMS-selected"
            : "unknown";
          break;
        case "gate_out":
          done = rank >= 6;
          if (gateOut) detail = `${gateOut.gate_id || "gate"} · ${fmt(gateOut.ts)}`;
          break;
      }
      return { ...s, done, detail };
    });
  }, [job, rank, scanQ.data, gateIn, gateOut, pickup, drop]);

  const nextAction = useMemo(() => {
    if (!job || job.status === "COMPLETED" || job.status === "CANCELLED") return null;
    if (job.status === "ASSIGNED") return { action: "accept", label: "Accept job" };
    if (job.status === "ACCEPTED") return { action: "gate_in", label: "Record gate-in" };
    if (job.status === "AT_GATE" || job.status === "IN_YARD")
      return isDropMove(job.move_type)
        ? { action: "drop", label: "Confirm yard drop" }
        : { action: "pickup", label: "Confirm yard pickup" };
    if (job.status === "PICKED_UP" || job.status === "DROPPED") {
      if (scanQ.data?.scan_required && !scanQ.data?.cleared)
        return { action: "scan_clean", label: "Record scan clean" };
      return { action: "gate_out", label: "Record gate-out" };
    }
    return null;
  }, [job, scanQ.data]);

  const jobs = jobsQ.data?.items ?? [];
  const openJobs = jobs.filter((j) => j.status !== "COMPLETED" && j.status !== "CANCELLED").length;

  const TABS: { key: Tab; label: string; icon: typeof Workflow; count?: number }[] = [
    { key: "lifecycle", label: "Container Journey", icon: Workflow, count: jobs.length },
    { key: "documents", label: "Documents", icon: FileText },
    { key: "chains", label: "ECY → CFS Chains", icon: Link2 },
    { key: "upload", label: "Data Upload", icon: UploadCloud },
  ];

  return (
    <PageContainer>
      <PageHeader
        icon={Workflow}
        title="Container Operations Console"
        subtitle="Container Journey & Movement Management"
        isFetching={jobsQ.isFetching}
        onRefresh={() => qc.invalidateQueries({ queryKey: ["uc3-jobs"] })}
      />

      <div className="flex flex-col gap-3 p-3 sm:gap-4 sm:p-4">
        <SegmentedTabs<Tab> tabs={TABS} value={tab} onChange={setTab} />

        {/* ------------------------------------------------------- LIFECYCLE */}
        {tab === "lifecycle" && (
          <>
            <StatGrid>
              <StatCard icon={ClipboardList} label="Jobs" value={jobs.length} tone="info" />
              <StatCard icon={Timer} label="Open jobs" value={openJobs} tone="warn" />
              <StatCard
                icon={CheckCircle2}
                label="Completed"
                value={jobs.filter((j) => j.status === "COMPLETED").length}
                tone="ok"
              />
              <StatCard
                icon={ScanLine}
                label="Scan required"
                value={scanQ.data?.scan_required ? "Yes" : "No"}
                tone={scanQ.data?.scan_required && !scanQ.data?.cleared ? "warn" : "neutral"}
                sub={scanQ.data?.machine_code ?? undefined}
              />
            </StatGrid>

            {/* The CREATE step. Everything below operates on a job that already
                exists; this is where one is raised. On success it selects the
                new job so the stepper opens straight at Assignment -> Accept. */}
            <JobAssignPanel
              defaultContainer={term}
              onAssigned={(jobId) => {
                setSelectedJob(jobId);
                setOpenStep("assignment");
              }}
            />

            <SearchInput
              value={term}
              onChange={setTerm}
              placeholder="Search container (e.g. MAEU6123458) or truck (MH43BX1488)"
              className="max-w-xl"
            />

            <div className="grid gap-3 sm:gap-4 lg:grid-cols-[minmax(260px,340px)_1fr]">
              {/* -------------------------------------------------- job list */}
              <Card className="overflow-hidden">
                <CardHeader className="flex-row items-center gap-2 border-b border-border">
                  <ClipboardList className="h-4 w-4 text-muted-foreground" aria-hidden />
                  <CardTitle>Container Jobs</CardTitle>
                </CardHeader>
                {jobsQ.isLoading ? (
                  <LoadingState />
                ) : jobsQ.isError ? (
                  <ErrorState onRetry={() => jobsQ.refetch()} />
                ) : jobs.length === 0 ? (
                  <EmptyState>
                    <div className="flex flex-col items-center gap-2">
                      <Inbox className="h-6 w-6 text-muted-foreground" aria-hidden />
                      <div className="font-medium text-foreground">
                        No active container jobs found
                      </div>
                      <p className="max-w-[26ch] text-xs text-muted-foreground">
                        {term
                          ? "No job matches this container. Clear the search to see all jobs."
                          : "Assign a truck and driver to a container to start a container journey."}
                      </p>
                      {term && (
                        <Button variant="outline" size="sm" onClick={() => setTerm("")}>
                          <RotateCcw className="h-3.5 w-3.5" />
                          Clear search
                        </Button>
                      )}
                    </div>
                  </EmptyState>
                ) : (
                  <ul className="max-h-[26rem] divide-y divide-border overflow-y-auto lg:max-h-[32rem]">
                    {jobs.map((j: ContainerJob) => {
                      const active = selectedJob === j.id;
                      return (
                        <li key={j.id}>
                          <button
                            type="button"
                            onClick={() => {
                              setSelectedJob(j.id);
                              setOpenStep(null);
                            }}
                            aria-current={active}
                            className={
                              "flex w-full flex-col gap-1 px-3 py-2.5 text-left transition-colors " +
                              (active ? "bg-primary/10" : "hover:bg-muted/40")
                            }
                          >
                            <div className="flex items-center justify-between gap-2">
                              <span className="truncate font-mono text-[13px] font-medium text-foreground">
                                {j.container_number || j.group_code || `Job #${j.id}`}
                              </span>
                              <StatusChip label={j.status} tone={statusTone(j.status)} />
                            </div>
                            <span className="truncate text-[11px] text-muted-foreground">
                              {j.vehicle_no || j.vehicle_id} · {j.move_type.replace("_", " ")}
                            </span>
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </Card>

              {/* ------------------------------------------------- lifecycle */}
              <Card className="overflow-hidden">
                {!job ? (
                  <EmptyState>
                    <div className="flex flex-col items-center gap-2">
                      <PackageSearch className="h-6 w-6 text-muted-foreground" aria-hidden />
                      <div className="font-medium text-foreground">No job selected</div>
                      <p className="max-w-[34ch] text-xs text-muted-foreground">
                        {/* layout-neutral wording: the job list sits beside this
                            panel on desktop but above it on mobile/tablet. */}
                        Select a container job to open its lifecycle timeline.
                      </p>
                    </div>
                  </EmptyState>
                ) : (
                  <>
                    <CardHeader className="gap-2 border-b border-border sm:flex-row sm:items-center">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <Container className="h-4 w-4 text-muted-foreground" aria-hidden />
                          <span className="font-mono text-base font-semibold text-foreground">
                            {job.container_number || job.group_code}
                          </span>
                          <StatusChip label={job.status} tone={statusTone(job.status)} />
                        </div>
                        <p className="mt-0.5 truncate text-xs text-muted-foreground">
                          Job #{job.id} · {job.vehicle_no || job.vehicle_id} ·{" "}
                          {job.driver_licence || "no driver"} · {job.terminal || "terminal n/a"}
                        </p>
                      </div>
                      {nextAction && (
                        <div className="sm:ml-auto">
                          <Button
                            size="sm"
                            disabled={act.isPending}
                            onClick={() => act.mutate(nextAction.action)}
                          >
                            {act.isPending ? "Working…" : nextAction.label}
                          </Button>
                        </div>
                      )}
                    </CardHeader>

                    <CardContent className="space-y-3">
                      {act.isError && (
                        <div
                          role="alert"
                          className="flex items-start gap-2 rounded-md border border-severity-critical/40 bg-severity-critical/10 px-3 py-2 text-xs text-foreground"
                        >
                          <AlertTriangle
                            className="mt-0.5 h-3.5 w-3.5 shrink-0 text-severity-critical"
                            aria-hidden
                          />
                          <span>{String((act.error as Error).message)}</span>
                        </div>
                      )}

                      {/* timeline */}
                      <ol className="space-y-2">
                        {steps.map((s, i) => {
                          const Icon = s.icon;
                          const expanded = openStep === s.key;
                          return (
                            <li key={s.key} className="relative">
                              {/* Connector between step markers. Drawn in the gap
                                  BELOW the card (which is opaque bg-card), so it
                                  reads as one timeline rather than loose cards. */}
                              {i < steps.length - 1 && (
                                <span
                                  className="absolute -bottom-2 left-[30px] h-2 w-px bg-border"
                                  aria-hidden
                                />
                              )}
                              <button
                                type="button"
                                onClick={() => setOpenStep(expanded ? null : s.key)}
                                aria-expanded={expanded}
                                className="flex w-full items-center gap-3 rounded-lg border border-border bg-card px-3 py-2.5 text-left transition-colors hover:bg-muted/40"
                              >
                                <span
                                  className={
                                    "relative z-10 flex h-9 w-9 shrink-0 items-center justify-center rounded-full " +
                                    (s.done
                                      ? "bg-severity-ok/15 text-severity-ok"
                                      : "bg-muted text-muted-foreground")
                                  }
                                >
                                  {s.done ? (
                                    <CheckCircle2 className="h-4 w-4" aria-hidden />
                                  ) : (
                                    <Icon className="h-4 w-4" aria-hidden />
                                  )}
                                </span>
                                <span className="min-w-0 flex-1">
                                  <span className="block text-[13px] font-medium text-foreground">
                                    {s.label}
                                  </span>
                                  <span className="block truncate text-[11px] text-muted-foreground">
                                    {s.detail}
                                  </span>
                                </span>
                                <StatusChip
                                  label={s.done ? "Done" : "Pending"}
                                  tone={s.done ? "ok" : "neutral"}
                                />
                              </button>

                              {expanded && (
                                <div className="ml-[18px] mt-1 rounded-lg border border-border bg-muted/30 px-3 py-2">
                                  <StepDetail
                                    stepKey={s.key}
                                    job={job}
                                    gateIn={gateIn}
                                    gateOut={gateOut}
                                    pickup={pickup}
                                    drop={drop}
                                    scan={scanQ.data}
                                  />
                                </div>
                              )}
                            </li>
                          );
                        })}
                      </ol>

                      {/* audit history */}
                      <div>
                        <h3 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                          Audit history
                        </h3>
                        <ul className="space-y-1">
                          {(job.events ?? []).map((e: JobEvent) => (
                            <li
                              key={e.id}
                              className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-muted-foreground"
                            >
                              <span className="font-mono font-medium text-foreground">
                                {e.event}
                              </span>
                              {e.old_status && (
                                <span>
                                  {e.old_status} → {e.new_status}
                                </span>
                              )}
                              <span>{fmt(e.created_at)}</span>
                              {e.actor && <span>by {e.actor}</span>}
                            </li>
                          ))}
                        </ul>
                      </div>
                    </CardContent>
                  </>
                )}
              </Card>
            </div>
          </>
        )}

        {/* ------------------------------------------------------- DOCUMENTS */}
        {tab === "documents" && (
          <>
            <SearchInput
              value={term}
              onChange={setTerm}
              placeholder="Search container to list its EIR / PIN / Form-13 documents"
              className="max-w-xl"
            />
            <Card className="overflow-hidden">
              <CardHeader className="flex-row items-center gap-2 border-b border-border">
                <FileText className="h-4 w-4 text-muted-foreground" aria-hidden />
                <CardTitle>Gate documents{container ? ` · ${container}` : ""}</CardTitle>
              </CardHeader>
              {!container ? (
                <EmptyState>
                  <div className="flex flex-col items-center gap-2">
                    <PackageSearch className="h-6 w-6 text-muted-foreground" aria-hidden />
                    <div className="font-medium text-foreground">Search a container</div>
                    <p className="text-xs text-muted-foreground">
                      Enter a container number to see every EIR, PIN ticket and Form 13 for it.
                    </p>
                  </div>
                </EmptyState>
              ) : docsQ.isLoading ? (
                <LoadingState />
              ) : docsQ.isError ? (
                <ErrorState onRetry={() => docsQ.refetch()} />
              ) : (docsQ.data?.total ?? 0) === 0 ? (
                <EmptyState>
                  <div className="flex flex-col items-center gap-2">
                    <Inbox className="h-6 w-6 text-muted-foreground" aria-hidden />
                    <div className="font-medium text-foreground">No gate documents found</div>
                    <p className="text-xs text-muted-foreground">
                      Upload EIR / PIN / Form-13 files from the Data Upload tab.
                    </p>
                    <Button variant="outline" size="sm" onClick={() => setTab("upload")}>
                      <UploadCloud className="h-3.5 w-3.5" />
                      Go to Data Upload
                    </Button>
                  </div>
                </EmptyState>
              ) : (
                <CardContent className="space-y-4">
                  {(["eir", "pin", "form13"] as const).map((kind) => (
                    <DocSection key={kind} kind={kind} rows={docsQ.data?.[kind] ?? []} />
                  ))}
                </CardContent>
              )}
            </Card>

            {/* Document OCR belongs to the UC-3 document lifecycle: it extracts
                structured fields from the same transport documents (Form-13, LR,
                permit) this tab lists. Previously it was only reachable from
                Reports & Enforcement. */}
            <Embedded>
              <DocumentOCR />
            </Embedded>
          </>
        )}

        {/* ---------------------------------------------------------- CHAINS */}
        {tab === "chains" && (
          <>
            <StatGrid>
              <StatCard
                icon={Link2}
                label="Chains"
                value={chainStatsQ.data?.chains ?? "—"}
                tone="info"
              />
              <StatCard
                icon={CheckCircle2}
                label="Complete"
                value={chainStatsQ.data?.complete_chains ?? "—"}
                tone="ok"
              />
              <StatCard
                icon={AlertTriangle}
                label="With anomaly"
                value={chainStatsQ.data?.anomaly_chains ?? "—"}
                tone="warn"
              />
              <StatCard
                icon={Timer}
                label="Avg cycle (h)"
                value={chainStatsQ.data?.avg_cycle_hours ?? "—"}
                tone="neutral"
                sub="ECY-out → CFS-out"
              />
            </StatGrid>

            <Card className="overflow-hidden">
              <CardHeader className="flex-row items-center gap-2 border-b border-border">
                <Link2 className="h-4 w-4 text-muted-foreground" aria-hidden />
                <CardTitle>ECY → CFS repositioning chains</CardTitle>
                <div className="ml-auto">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={rebuild.isPending}
                    onClick={() => rebuild.mutate()}
                  >
                    <RotateCcw
                      className={"h-3.5 w-3.5" + (rebuild.isPending ? " animate-spin" : "")}
                    />
                    {rebuild.isPending ? "Rebuilding…" : "Rebuild chains"}
                  </Button>
                </div>
              </CardHeader>

              {chainsQ.isLoading ? (
                <LoadingState />
              ) : chainsQ.isError ? (
                <ErrorState onRetry={() => chainsQ.refetch()} />
              ) : (chainsQ.data?.items?.length ?? 0) === 0 ? (
                <EmptyState>
                  <div className="flex flex-col items-center gap-2">
                    <Inbox className="h-6 w-6 text-muted-foreground" aria-hidden />
                    <div className="font-medium text-foreground">No chains built yet</div>
                    <p className="text-xs text-muted-foreground">
                      Run “Rebuild chains” after importing CODECO movements.
                    </p>
                  </div>
                </EmptyState>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-[13px]">
                    <thead className="border-b border-border bg-muted/40 text-[11px] uppercase tracking-wide text-muted-foreground">
                      <tr>
                        <th className="px-3 py-2 font-semibold">Container</th>
                        <th className="px-3 py-2 font-semibold">ECY out</th>
                        <th className="px-3 py-2 font-semibold">CFS in</th>
                        <th className="px-3 py-2 font-semibold">CFS out</th>
                        <th className="px-3 py-2 text-right font-semibold">Transit h</th>
                        <th className="px-3 py-2 text-right font-semibold">Dwell h</th>
                        <th className="px-3 py-2 text-right font-semibold">Cycle h</th>
                        <th className="px-3 py-2 font-semibold">Status</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {chainsQ.data?.items?.map((c: EcyCfsChain) => (
                        <tr key={c.id} className="hover:bg-muted/40">
                          <td className="px-3 py-2">
                            <div className="flex items-center gap-1.5">
                              <span className="font-mono text-foreground">
                                {c.container_number}
                              </span>
                              {c.has_anomaly && (
                                <span
                                  title={(c.anomaly_labels || c.anomaly_codes).join("; ")}
                                  className="inline-flex items-center gap-1 rounded-full bg-severity-warning/15 px-1.5 py-0.5 text-[10px] font-semibold text-severity-warning"
                                >
                                  <AlertTriangle className="h-3 w-3" aria-hidden />
                                  {c.anomaly_codes.length}
                                </span>
                              )}
                            </div>
                          </td>
                          <td className="px-3 py-2 text-xs text-muted-foreground">
                            {fmt(c.ecy_out_ts)}
                          </td>
                          <td className="px-3 py-2 text-xs text-muted-foreground">
                            {fmt(c.cfs_in_ts)}
                          </td>
                          <td className="px-3 py-2 text-xs text-muted-foreground">
                            {fmt(c.cfs_out_ts)}
                          </td>
                          <td className="px-3 py-2 text-right tabular-nums">
                            {c.transit_hours ?? "—"}
                          </td>
                          <td className="px-3 py-2 text-right tabular-nums">
                            {c.dwell_hours ?? "—"}
                          </td>
                          <td className="px-3 py-2 text-right tabular-nums">
                            {c.cycle_hours ?? "—"}
                          </td>
                          <td className="px-3 py-2">
                            <StatusChip
                              label={c.chain_status}
                              tone={c.chain_status === "COMPLETE" ? "ok" : "neutral"}
                            />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>

            {chainStatsQ.data?.by_anomaly?.length ? (
              <Card>
                <CardHeader className="flex-row items-center gap-2">
                  <AlertTriangle className="h-4 w-4 text-severity-warning" aria-hidden />
                  <CardTitle>Detected anomalies</CardTitle>
                </CardHeader>
                <CardContent className="space-y-1">
                  {chainStatsQ.data.by_anomaly.map((a) => (
                    <div key={a.code} className="flex flex-wrap items-baseline gap-x-2 text-xs">
                      <span className="font-mono font-semibold text-severity-warning">
                        {a.code}
                      </span>
                      <span className="tabular-nums text-foreground">{a.chains} chain(s)</span>
                      <span className="text-muted-foreground">
                        {chainStatsQ.data?.anomaly_labels?.[a.code]}
                      </span>
                    </div>
                  ))}
                </CardContent>
              </Card>
            ) : null}
          </>
        )}

        {/* ---------------------------------------------------------- UPLOAD */}
        {tab === "upload" && <GateDocUploadPanel />}
      </div>
    </PageContainer>
  );
}

// --- step detail -------------------------------------------------------------

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-0.5">
      <dt className="shrink-0 text-[11px] text-muted-foreground">{label}</dt>
      <dd className="truncate text-right text-[11px] font-medium text-foreground">{value}</dd>
    </div>
  );
}

function StepDetail({
  stepKey,
  job,
  gateIn,
  gateOut,
  pickup,
  drop,
  scan,
}: {
  stepKey: StepKey;
  job: ContainerJob;
  gateIn?: Record<string, string | null>;
  gateOut?: Record<string, string | null>;
  pickup?: Record<string, string | null>;
  drop?: Record<string, string | null>;
  scan?: {
    scan_required: boolean;
    machine_code: string | null;
    machine_class: string | null;
    result: string | null;
  } | null;
}) {
  if (stepKey === "assignment") {
    return (
      <dl>
        <Field label="Assigned" value={fmt(job.assigned_at)} />
        <Field label="Accepted" value={fmt(job.accepted_at)} />
        <Field label="Move type" value={job.move_type.replace("_", " ")} />
        <Field
          label="Document"
          value={job.document_type ? `${job.document_type} ${job.document_reference ?? ""}` : "—"}
        />
      </dl>
    );
  }
  if (stepKey === "gate_in" || stepKey === "gate_out") {
    const ev = stepKey === "gate_in" ? gateIn : gateOut;
    if (!ev) return <p className="text-[11px] text-muted-foreground">No crossing recorded yet.</p>;
    return (
      <dl>
        <Field label="Time" value={fmt(ev.ts)} />
        <Field label="Gate" value={ev.gate_id || "—"} />
        <Field label="BAT lane" value={ev.bat_lane || "—"} />
        <Field label="Document" value={ev.document_reference || "—"} />
      </dl>
    );
  }
  if (stepKey === "yard_pickup" || stepKey === "yard_drop") {
    const m = stepKey === "yard_pickup" ? pickup : drop;
    if (!m) return <p className="text-[11px] text-muted-foreground">No yard movement recorded.</p>;
    return (
      <dl>
        <Field label="Time" value={fmt(m.occurred_at)} />
        <Field label="Yard location" value={m.yard_location || "—"} />
        <Field label="Terminal" value={m.terminal || "—"} />
      </dl>
    );
  }
  return (
    <dl>
      <Field label="RMS selected" value={scan?.scan_required ? "Yes" : "No"} />
      <Field label="Machine" value={scan?.machine_code || "—"} />
      <Field label="Class" value={scan?.machine_class || "—"} />
      <Field label="Result" value={scan?.result || "—"} />
    </dl>
  );
}

// --- document section --------------------------------------------------------

const DOC_LABEL: Record<string, string> = { eir: "EIR", pin: "PIN ticket", form13: "Form 13" };

function DocSection({ kind, rows }: { kind: string; rows: Record<string, unknown>[] }) {
  return (
    <div>
      <h3 className="mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {DOC_LABEL[kind]}
        <span className="rounded-full bg-muted px-1.5 text-[10px] tabular-nums">{rows.length}</span>
      </h3>
      {rows.length === 0 ? (
        <p className="text-xs text-muted-foreground">None</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-left text-[12px]">
            <tbody className="divide-y divide-border">
              {rows.map((d, i) => (
                <tr key={String(d.id ?? i)} className="hover:bg-muted/40">
                  <td className="px-2.5 py-1.5 font-mono text-foreground">
                    {String(d.eir_no || d.pin_number || d.form13_no || `#${d.id}`)}
                  </td>
                  <td className="px-2.5 py-1.5 text-muted-foreground">
                    {String(d.terminal || "—")}
                  </td>
                  <td className="px-2.5 py-1.5 font-mono text-muted-foreground">
                    {String(d.truck_no || d.vehicle_no || "—")}
                  </td>
                  <td className="px-2.5 py-1.5 text-muted-foreground">
                    {d.tat_minutes != null ? `${d.tat_minutes} min TAT` : ""}
                    {d.yard_location ? `yard ${d.yard_location}` : ""}
                    {d.visit_id ? `visit ${d.visit_id}` : ""}
                  </td>
                  <td className="px-2.5 py-1.5 text-muted-foreground">
                    {fmt((d.truck_in_time || d.issued_at) as string | null)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
