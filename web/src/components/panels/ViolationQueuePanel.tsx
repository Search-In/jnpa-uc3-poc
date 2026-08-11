// T-06 — the violation QUEUE (UC3-028) and the hash-chained AUDIT TRAIL (UC3-029).
//
// One panel, because they are one operator task: you pick a case off the queue,
// look at its evidence, walk its lifecycle, and prove the audit chain. Splitting
// them would mean selecting the same case twice on two screens.
//
// It mounts inside the existing Reports & Enforcement console beside
// ViolationDetectionPanel (which FILES cases); this is the surface that WORKS
// them afterwards. No new route, no second enforcement screen.
//
// The two claims it has to survive being probed on:
//
//  * **Evidence is written once and referenced by its hash (UI-113).** The case
//    row carries the SHA-256, and the panel shows it in full rather than
//    truncated-only, so an evaluator can compare it against the stored object.
//    A swapped frame no longer matches the case.
//  * **The audit chain is append-only and verifiable (UC3-029).** "Verify chain"
//    recomputes every entry server-side and reports the first broken link by
//    name. The lifecycle buttons only offer LEGAL next states, and an illegal
//    one attempted anyway is rejected by the server (409) and surfaced verbatim
//    — the rejection is the demo, so it is shown, not swallowed.
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  Fingerprint,
  Link2,
  BellRing,
  ListChecks,
  ShieldCheck,
} from "lucide-react";

import { api } from "@/lib/api";
import { cn, fmtDateTimeIST } from "@/lib/utils";
import { Card } from "@/components/ui/card";
import { DataTable, StatusChip, type Column, type Tone } from "@/components/ui/dtccc";
import ChallanSimulatedBadge from "@/components/panels/ChallanSimulatedBadge";
import type { EscalateResult, ViolationCaseBundle, ViolationCaseRow } from "@/lib/types";

const STATUS_TONE: Record<string, Tone> = {
  DETECTED: "warn",
  REVIEWED: "info",
  CONFIRMED: "info",
  CHALLAN_ISSUED: "critical",
  PAID: "ok",
  CLOSED: "neutral",
  DISPUTED: "warn",
};

const SEVERITY_TONE: Record<string, Tone> = {
  critical: "critical",
  warning: "warn",
  info: "info",
};

/**
 * Legal next states per current state — the same map the server enforces.
 *
 * Mirrored here ONLY to decide which buttons to offer. The server remains the
 * authority: an illegal transition attempted anyway (deep link, stale tab, a
 * demo deliberately proving the point) is rejected with 409 and the error is
 * rendered. The UI never decides a transition is legal.
 */
const NEXT_STATES: Record<string, string[]> = {
  DETECTED: ["REVIEWED", "CLOSED"],
  REVIEWED: ["CONFIRMED", "DETECTED", "CLOSED"],
  CONFIRMED: ["CHALLAN_ISSUED", "CLOSED"],
  CHALLAN_ISSUED: ["PAID", "DISPUTED", "CLOSED"],
  PAID: ["CLOSED"],
  DISPUTED: ["CHALLAN_ISSUED", "CLOSED"],
  CLOSED: [],
};

/** Every lifecycle state, so the stepper shows the whole ladder not just the past. */
const LADDER = ["DETECTED", "REVIEWED", "CONFIRMED", "CHALLAN_ISSUED", "PAID", "CLOSED"];

/** Delivery status tone. UNAVAILABLE is neutral: it is a truthful outcome, not a bug. */
const DELIVERY_TONE: Record<string, Tone> = {
  DELIVERED: "ok",
  SENT: "info",
  QUEUED: "info",
  FAILED: "critical",
  UNAVAILABLE: "neutral",
};

/**
 * The N/2N/3N ladder and its per-channel delivery log (UC3-028, UI-114).
 *
 * Delivery status is shown per channel rather than as one "notified" flag,
 * because "SMS unavailable, email sent" is the situation an enforcement audit
 * actually asks about and a boolean cannot express it. UNAVAILABLE means no
 * provider is configured — recorded honestly, because writing SENT would assert
 * a delivery that never happened.
 */
function EscalationSection({ caseId }: { caseId: string }) {
  const qc = useQueryClient();
  const [dwell, setDwell] = useState(6);
  const [lastRun, setLastRun] = useState<EscalateResult | null>(null);

  const notifQ = useQuery({
    queryKey: ["case-notifications", caseId],
    queryFn: () => api.violationNotifications(caseId),
  });

  const escalateM = useMutation({
    mutationFn: () => api.violationEscalate(caseId, { dwell_minutes: dwell }),
    onSuccess: (d) => {
      setLastRun(d);
      void qc.invalidateQueries({ queryKey: ["case-notifications", caseId] });
    },
  });

  const fieldM = useMutation({
    mutationFn: () => api.violationFieldVerification(caseId),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["case-notifications", caseId] }),
  });

  const n = notifQ.data;

  return (
    <div className="min-w-0">
      <h4 className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        <BellRing className="h-3.5 w-3.5" aria-hidden />
        Escalation ladder (N / 2N / 3N)
      </h4>

      <div className="mt-1.5 flex flex-wrap items-end gap-2">
        <label className="text-[10px] text-muted-foreground">
          Observed dwell (min)
          <input
            type="number"
            min={0}
            value={dwell}
            onChange={(e) => setDwell(Number(e.target.value))}
            className="ml-1 w-20 rounded-md border border-border bg-background px-2 py-1 text-[11px] text-foreground"
          />
        </label>
        <button
          type="button"
          onClick={() => escalateM.mutate()}
          disabled={escalateM.isPending}
          className="rounded-md border border-border bg-background px-2.5 py-1 text-[11px] font-medium hover:bg-muted disabled:opacity-50"
        >
          {escalateM.isPending ? "Evaluating…" : "Evaluate ladder"}
        </button>
        <button
          type="button"
          onClick={() => fieldM.mutate()}
          disabled={fieldM.isPending}
          className="rounded-md border border-border bg-background px-2.5 py-1 text-[11px] font-medium hover:bg-muted disabled:opacity-50"
        >
          Plate unreadable → marshal task
        </button>
      </div>

      {lastRun && (
        <p className="mt-1.5 text-[10px] leading-snug text-muted-foreground">
          N = {lastRun.n_minutes} min · schedule{" "}
          {lastRun.schedule.map((r) => `${r.rung}:${r.due_after_min}m`).join(" · ")} · fired{" "}
          {lastRun.rungs_fired.join(", ") || "none"}
          {lastRun.rungs_already_fired.length > 0 &&
            ` · already fired ${lastRun.rungs_already_fired.join(", ")} (not resent)`}
          {" · "}
          <span
            className={
              lastRun.within_budget
                ? "text-emerald-600 dark:text-emerald-400"
                : "text-severity-critical"
            }
          >
            {lastRun.elapsed_ms} ms / {lastRun.latency_budget_ms} ms F-08 budget
          </span>
        </p>
      )}

      {n && n.escalations.length > 0 && (
        <ul className="mt-1.5 flex flex-wrap gap-1.5">
          {n.escalations.map((e) => (
            <li key={e.escalation_id}>
              <StatusChip
                label={`${e.rung}× — ${e.rung_label} @ ${e.due_after_min}m`}
                tone="warn"
              />
            </li>
          ))}
        </ul>
      )}

      {n && n.deliveries.length > 0 && (
        <table className="mt-2 w-full text-[10px]">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="py-1 pr-2 font-medium">Rung</th>
              <th className="py-1 pr-2 font-medium">Channel</th>
              <th className="py-1 pr-2 font-medium">Recipient</th>
              <th className="py-1 pr-2 font-medium">Status</th>
            </tr>
          </thead>
          <tbody>
            {n.deliveries.map((d) => (
              <tr key={d.delivery_id} className="border-t border-border/60">
                <td className="py-1 pr-2 tabular-nums">{d.rung}</td>
                <td className="py-1 pr-2">{d.channel}</td>
                <td className="min-w-0 py-1 pr-2">
                  <span className="break-all">{d.recipient ?? "—"}</span>
                  <span className="ml-1 text-muted-foreground">({d.recipient_role})</span>
                </td>
                <td className="py-1 pr-2">
                  <StatusChip label={d.status} tone={DELIVERY_TONE[d.status] ?? "neutral"} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {n?.deliveries.some((d) => d.status === "UNAVAILABLE") && (
        <p className="mt-1 text-[10px] leading-snug text-muted-foreground">
          UNAVAILABLE means no provider is configured for that channel (a declared post-award
          integration). The recipient was resolved from the transporter master, but nothing was sent
          — recorded as such rather than as SENT.
        </p>
      )}

      {n?.field_verification_task && (
        <p className="mt-1.5 rounded-md border border-amber-500/40 bg-amber-500/10 p-1.5 text-[10px] leading-snug text-amber-700 dark:text-amber-400">
          Field-verification task #{n.field_verification_task.task_id} —{" "}
          {n.field_verification_task.reason} → {n.field_verification_task.assigned_to} (
          {n.field_verification_task.status}). The plate could not be read, so no owner is notified;
          a marshal verifies with the evidence photo.
        </p>
      )}
    </div>
  );
}

function CaseDetail({ caseId }: { caseId: string }) {
  const qc = useQueryClient();
  const [chain, setChain] = useState<{
    valid: boolean;
    length: number;
    broken_at?: unknown;
  } | null>(null);
  const [txError, setTxError] = useState<string | null>(null);

  const bundleQ = useQuery({
    queryKey: ["violation-case", caseId],
    queryFn: () => api.violationCase(caseId),
  });

  const verifyM = useMutation({
    mutationFn: () => api.violationVerifyChain(caseId),
    onSuccess: (d) => setChain(d),
  });

  const transitionM = useMutation({
    mutationFn: (to: string) => api.violationTransition(caseId, to),
    onSuccess: () => {
      setTxError(null);
      setChain(null); // the chain grew; make the auditor re-verify rather than trust a stale green
      void qc.invalidateQueries({ queryKey: ["violation-case", caseId] });
      void qc.invalidateQueries({ queryKey: ["violation-queue"] });
    },
    // The server's rejection reason IS the demo for UC3-029, so it is shown.
    onError: (e: Error) => setTxError(e.message),
  });

  if (bundleQ.isLoading) {
    return (
      <p className="p-3 text-[12px] text-muted-foreground" role="status">
        Loading case…
      </p>
    );
  }
  if (bundleQ.isError) {
    return (
      <p className="p-3 text-[12px] text-severity-critical" role="alert">
        Case unavailable: {(bundleQ.error as Error).message}
      </p>
    );
  }
  const bundle = bundleQ.data as ViolationCaseBundle | undefined;
  if (!bundle?.case) {
    return <p className="p-3 text-[12px] text-muted-foreground">Case not found.</p>;
  }

  const c = bundle.case;
  const legal = NEXT_STATES[c.status] ?? [];

  return (
    <div className="flex min-w-0 flex-col gap-3 rounded-lg border border-border bg-muted/20 p-3">
      {/* ---- lifecycle ladder ---- */}
      <div className="min-w-0">
        <h4 className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          Lifecycle
        </h4>
        <div className="mt-1.5 flex flex-wrap items-center gap-1">
          {LADDER.map((st, i) => {
            const reached = LADDER.indexOf(c.status) >= i;
            const current = c.status === st;
            return (
              <span key={st} className="flex shrink-0 items-center gap-1">
                <StatusChip
                  label={st}
                  tone={current ? (STATUS_TONE[st] ?? "info") : reached ? "ok" : "neutral"}
                />
                {i < LADDER.length - 1 && (
                  <span className="text-muted-foreground/50" aria-hidden>
                    →
                  </span>
                )}
              </span>
            );
          })}
          {c.status === "DISPUTED" && <StatusChip label="DISPUTED" tone="warn" />}
        </div>
      </div>

      {/* ---- transitions: only legal next states are offered ---- */}
      <div className="min-w-0">
        <h4 className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          Advance case
        </h4>
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {legal.length === 0 && (
            <span className="text-[11px] text-muted-foreground">
              {c.status} is terminal — no further transition is legal.
            </span>
          )}
          {legal.map((to) => (
            <button
              key={to}
              type="button"
              onClick={() => transitionM.mutate(to)}
              disabled={transitionM.isPending}
              className="rounded-md border border-border bg-background px-2.5 py-1 text-[11px] font-medium hover:bg-muted disabled:opacity-50"
            >
              → {to}
            </button>
          ))}
        </div>
        {txError && (
          <p className="mt-1.5 flex items-start gap-1.5 text-[11px] text-severity-critical">
            <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden />
            Rejected by the server: {txError}
          </p>
        )}
      </div>

      {/* ---- evidence, referenced by hash ---- */}
      <div className="min-w-0">
        <h4 className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          <Fingerprint className="h-3.5 w-3.5" aria-hidden />
          Evidence
        </h4>
        {c.evidence_sha256 ? (
          <>
            <p className="mt-1 break-all font-mono text-[10px] text-foreground">
              sha256:{c.evidence_sha256}
            </p>
            <p className="mt-0.5 text-[10px] leading-snug text-muted-foreground">
              Written once and referenced by this hash. A different frame produces a different hash
              and no longer matches the case.
            </p>
          </>
        ) : (
          <p className="mt-1 text-[11px] text-muted-foreground">
            No evidence hash recorded on this case.
          </p>
        )}
        {c.evidence_url && (
          <img
            src={c.evidence_url}
            alt={`Evidence frame for case ${caseId}`}
            className="mt-1.5 max-h-48 w-auto max-w-full rounded border border-border object-contain"
            loading="lazy"
          />
        )}
      </div>

      {/* ---- challan, always badged ---- */}
      {bundle.challan && (
        <div className="min-w-0">
          <h4 className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            e-Challan
          </h4>
          <p className="mt-1 font-mono text-[12px]">{String(bundle.challan.challan_no ?? "—")}</p>
          <ChallanSimulatedBadge
            challanNo={String(bundle.challan.challan_no ?? "")}
            disclosure={bundle.challan}
            className="mt-1.5"
          />
        </div>
      )}

      {/* ---- UC3-028 escalation ladder + per-channel delivery ---- */}
      <EscalationSection caseId={caseId} />

      {/* ---- hash-chained audit trail ---- */}
      <div className="min-w-0">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h4 className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            <Link2 className="h-3.5 w-3.5" aria-hidden />
            Audit trail ({bundle.audit?.length ?? 0} entries, append-only)
          </h4>
          <button
            type="button"
            onClick={() => verifyM.mutate()}
            disabled={verifyM.isPending}
            className="shrink-0 rounded-md border border-border bg-background px-2.5 py-1 text-[11px] font-medium hover:bg-muted disabled:opacity-50"
          >
            {verifyM.isPending ? "Verifying…" : "Verify chain"}
          </button>
        </div>

        {chain && (
          <p
            className={cn(
              "mt-1.5 flex items-start gap-1.5 rounded-md border p-2 text-[11px]",
              chain.valid
                ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
                : "border-severity-critical/40 bg-severity-critical/10 text-severity-critical",
            )}
            role="status"
          >
            {chain.valid ? (
              <CheckCircle2 className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden />
            ) : (
              <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden />
            )}
            <span className="min-w-0">
              {chain.valid
                ? `Chain intact — ${chain.length} entries recomputed, every hash matches.`
                : `Chain BROKEN at ${JSON.stringify(chain.broken_at)} — an entry was altered after it was written.`}
            </span>
          </p>
        )}

        <ol className="mt-1.5 flex flex-col gap-1">
          {(bundle.audit ?? []).map((a, i) => (
            <li
              key={`${a.ts}-${i}`}
              className="min-w-0 rounded-md border border-border bg-background p-1.5"
            >
              <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px]">
                <span className="font-medium">{a.event}</span>
                {a.from_status && (
                  <span className="text-muted-foreground">
                    {a.from_status} → {a.to_status}
                  </span>
                )}
                <span className="text-muted-foreground">{fmtDateTimeIST(a.ts)}</span>
                <span className="text-muted-foreground">actor: {a.actor ?? "—"}</span>
              </div>
              <p className="mt-0.5 break-all font-mono text-[9px] text-muted-foreground">
                {a.hash}
              </p>
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}

export default function ViolationQueuePanel() {
  const [selected, setSelected] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [kindFilter, setKindFilter] = useState<string>("");

  const queueQ = useQuery({
    queryKey: ["violation-queue", statusFilter, kindFilter],
    queryFn: () => api.violationQueue({ status: statusFilter, kind: kindFilter }),
    refetchInterval: 15_000,
  });

  const data = queueQ.data;
  const cases = data?.cases ?? [];

  const columns: Column<ViolationCaseRow>[] = [
    {
      key: "vehicle_number",
      header: "Vehicle",
      render: (c) => <span className="font-mono text-[12px]">{c.vehicle_number ?? "—"}</span>,
    },
    {
      key: "kinds",
      header: "Violation",
      render: (c) =>
        c.kinds.length ? (
          <div className="flex flex-wrap gap-1">
            {c.kinds.map((k) => (
              <StatusChip key={k} label={k} tone="warn" />
            ))}
          </div>
        ) : (
          <span className="text-muted-foreground">—</span>
        ),
    },
    {
      key: "severity",
      header: "Severity",
      render: (c) => (
        <StatusChip label={c.severity} tone={SEVERITY_TONE[c.severity] ?? "neutral"} />
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (c) => <StatusChip label={c.status} tone={STATUS_TONE[c.status] ?? "neutral"} />,
    },
    {
      key: "total_fine",
      header: "Fine",
      align: "right",
      render: (c) => `₹${c.total_fine.toLocaleString("en-IN")}`,
    },
    {
      key: "evidence_sha256",
      header: "Evidence",
      render: (c) =>
        c.evidence_sha256 ? (
          <span className="font-mono text-[10px]" title={c.evidence_sha256}>
            {c.evidence_sha256.slice(0, 10)}…
          </span>
        ) : (
          <span className="text-muted-foreground">none</span>
        ),
    },
    {
      key: "first_detected_at",
      header: "Detected",
      render: (c) => fmtDateTimeIST(c.first_detected_at),
    },
  ];

  return (
    <div className="flex min-w-0 flex-col gap-3">
      <Card className="min-w-0 p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="min-w-0">
            <h3 className="flex items-center gap-1.5 text-sm font-semibold">
              <ListChecks className="h-4 w-4 shrink-0" aria-hidden />
              Violation queue
            </h3>
            <p className="mt-0.5 text-[11px] leading-snug text-muted-foreground">
              Cases filed by the enforcement pipeline. Select one to view its evidence, advance its
              lifecycle and verify its audit chain.
            </p>
          </div>
          <div className="flex shrink-0 flex-wrap gap-2">
            <label className="text-[11px] text-muted-foreground">
              <span className="sr-only">Filter by status</span>
              <select
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
                className="rounded-md border border-border bg-background px-2 py-1 text-[11px] text-foreground"
              >
                <option value="">All statuses</option>
                {(data?.lifecycle ?? []).map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-[11px] text-muted-foreground">
              <span className="sr-only">Filter by violation type</span>
              <select
                value={kindFilter}
                onChange={(e) => setKindFilter(e.target.value)}
                className="rounded-md border border-border bg-background px-2 py-1 text-[11px] text-foreground"
              >
                <option value="">All types</option>
                {(data?.violation_types ?? []).map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
            </label>
          </div>
        </div>
        {data?.evidence_policy && (
          <p className="mt-2 flex items-start gap-1.5 text-[10px] leading-snug text-muted-foreground">
            <ShieldCheck className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden />
            {data.evidence_policy.note}
          </p>
        )}
      </Card>

      <DataTable<ViolationCaseRow>
        columns={columns}
        rows={cases}
        rowKey={(c) => c.case_id}
        status={queueQ}
        onRetry={() => void queueQ.refetch()}
        emptyLabel="No violation cases match this filter."
        onRowClick={(c) => setSelected(selected === c.case_id ? null : c.case_id)}
        isRowActive={(c) => c.case_id === selected}
      />

      {selected && <CaseDetail caseId={selected} />}
    </div>
  );
}
