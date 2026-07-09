// Workflow Composer (audit closure) — author IF/THEN automation rules WITHOUT
// code changes, test them against a sample event, and see the execution audit
// trail. Backed by /api/workflows/* (Postgres-persisted, in-memory fallback).
//
//   IF vehicle_speed > 60  THEN create_violation, notify_officer, suggest_reroute

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Workflow, Plus, Trash2, Play, ShieldCheck } from "lucide-react";
import { api, type WfRule } from "@/lib/api";
import { PageContainer, PageHeader, StatusChip } from "@/components/ui/dtccc";
import { Card } from "@/components/ui/card";
import { EmptyState, LoadingState } from "@/components/ui/misc";
import { STATUS } from "@/lib/tokens";
import { fmtDateTimeIST } from "@/lib/utils";

export default function WorkflowComposer() {
  const qc = useQueryClient();
  const catalogQ = useQuery({ queryKey: ["wf-catalog"], queryFn: () => api.wfCatalog() });
  const rulesQ = useQuery({ queryKey: ["wf-rules"], queryFn: () => api.wfRules() });
  const execQ = useQuery({ queryKey: ["wf-executions"], queryFn: () => api.wfExecutions(30) });

  const fields = catalogQ.data?.fields ?? [];
  const operators = catalogQ.data?.operators ?? [];
  const actions = catalogQ.data?.actions ?? [];

  // --- new-rule form state ---
  const [name, setName] = useState("");
  const [field, setField] = useState("");
  const [op, setOp] = useState(">");
  const [value, setValue] = useState("");
  const [picked, setPicked] = useState<string[]>([]);

  const create = useMutation({
    mutationFn: () => api.wfCreateRule({ name, field, op, value, actions: picked }),
    onSuccess: () => {
      setName(""); setField(""); setValue(""); setPicked([]);
      qc.invalidateQueries({ queryKey: ["wf-rules"] });
    },
  });
  const del = useMutation({
    mutationFn: (id: string) => api.wfDeleteRule(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["wf-rules"] }),
  });
  const toggle = useMutation({
    mutationFn: (r: WfRule) => api.wfUpdateRule(r.id, { enabled: !r.enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["wf-rules"] }),
  });

  // --- test/evaluate state ---
  const [testEvent, setTestEvent] = useState<Record<string, string>>({});
  const evaluate = useMutation({
    mutationFn: () => {
      const ev: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(testEvent)) {
        if (v === "") continue;
        const f = fields.find((x) => x.key === k);
        ev[k] = f?.type === "number" ? Number(v) : v;
      }
      return api.wfEvaluate(ev);
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["wf-executions"] }),
  });

  const canSave = name.trim() && field && value !== "" && picked.length > 0;
  const unreachable = rulesQ.isError && catalogQ.isError;

  const lastEval = evaluate.data;
  const actionLabel = useMemo(() => {
    const m = new Map(actions.map((a) => [a.key, a.label]));
    return (k: string) => m.get(k) ?? k;
  }, [actions]);

  return (
    <PageContainer>
      <PageHeader
        title="Workflow Composer"
        subtitle="Author IF/THEN automation rules — no code changes"
        icon={Workflow}
      />

      {unreachable ? (
        <div className="px-4 py-3">
          <EmptyState>
            Workflow service unreachable — start the gateway to author and run rules.
          </EmptyState>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 px-4 py-3 lg:grid-cols-2">
          {/* ---------------- Rule authoring ---------------- */}
          <Card className="p-4">
            <div className="mb-3 flex items-center gap-2">
              <Plus size={15} />
              <h3 className="text-sm font-semibold">New rule</h3>
            </div>
            <div className="space-y-3 text-sm">
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Rule name (e.g. Over-speed → violation)"
                className="w-full rounded-md border border-border bg-card px-2 py-1.5 outline-none"
              />
              <div>
                <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide" style={{ color: STATUS.warning }}>
                  IF
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <select
                    value={field}
                    onChange={(e) => setField(e.target.value)}
                    className="rounded-md border border-border bg-card px-2 py-1.5"
                  >
                    <option value="">field…</option>
                    {fields.map((f) => (
                      <option key={f.key} value={f.key}>
                        {f.label}{f.unit ? ` (${f.unit})` : ""}
                      </option>
                    ))}
                  </select>
                  <select
                    value={op}
                    onChange={(e) => setOp(e.target.value)}
                    className="rounded-md border border-border bg-card px-2 py-1.5 font-mono"
                  >
                    {operators.map((o) => (
                      <option key={o} value={o}>{o}</option>
                    ))}
                  </select>
                  <input
                    value={value}
                    onChange={(e) => setValue(e.target.value)}
                    placeholder="value"
                    className="w-28 rounded-md border border-border bg-card px-2 py-1.5"
                  />
                </div>
              </div>
              <div>
                <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide" style={{ color: STATUS.info }}>
                  THEN
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {actions.map((a) => {
                    const on = picked.includes(a.key);
                    return (
                      <button
                        key={a.key}
                        onClick={() =>
                          setPicked((p) => (on ? p.filter((k) => k !== a.key) : [...p, a.key]))
                        }
                        className={`rounded-md border px-2 py-1 text-[12px] ${
                          on
                            ? "border-primary bg-primary/10 text-foreground"
                            : "border-border bg-muted/40 text-muted-foreground"
                        }`}
                      >
                        {a.label}
                      </button>
                    );
                  })}
                </div>
              </div>
              <button
                disabled={!canSave || create.isPending}
                onClick={() => create.mutate()}
                className="rounded-md bg-primary px-3 py-1.5 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              >
                {create.isPending ? "Saving…" : "Save rule"}
              </button>
              {create.isError && (
                <div className="text-[11px]" style={{ color: STATUS.critical }}>
                  {(create.error as Error)?.message}
                </div>
              )}
            </div>
          </Card>

          {/* ---------------- Test / evaluate ---------------- */}
          <Card className="p-4">
            <div className="mb-3 flex items-center gap-2">
              <Play size={15} />
              <h3 className="text-sm font-semibold">Test an event</h3>
            </div>
            <div className="grid grid-cols-2 gap-2 text-sm">
              {fields.map((f) => (
                <label key={f.key} className="flex flex-col gap-0.5">
                  <span className="text-[10px] text-muted-foreground">
                    {f.label}{f.unit ? ` (${f.unit})` : ""}
                  </span>
                  <input
                    value={testEvent[f.key] ?? ""}
                    onChange={(e) => setTestEvent((s) => ({ ...s, [f.key]: e.target.value }))}
                    placeholder={f.type === "number" ? "number" : "text"}
                    className="rounded-md border border-border bg-card px-2 py-1 text-[13px] outline-none"
                  />
                </label>
              ))}
            </div>
            <button
              disabled={evaluate.isPending}
              onClick={() => evaluate.mutate()}
              className="mt-3 rounded-md border border-border px-3 py-1.5 text-[13px] font-semibold hover:bg-muted disabled:opacity-50"
            >
              {evaluate.isPending ? "Evaluating…" : "Evaluate"}
            </button>
            {lastEval && (
              <div className="mt-3 space-y-1">
                <div className="text-[12px]">
                  <strong>{lastEval.matched_count}</strong> rule(s) fired
                </div>
                {lastEval.results.filter((r) => r.matched).map((r) => (
                  <div key={r.rule_id} className="rounded-md border px-2 py-1 text-[11px]"
                       style={{ borderColor: STATUS.ok + "66" }}>
                    <span className="font-medium">{r.name}</span>
                    <span className="text-muted-foreground"> · {r.condition} → </span>
                    {r.actions_fired.map((a) => actionLabel(a)).join(", ")}
                  </div>
                ))}
              </div>
            )}
          </Card>

          {/* ---------------- Rule list ---------------- */}
          <Card className="p-4 lg:col-span-2">
            <div className="mb-3 flex items-center gap-2">
              <ShieldCheck size={15} />
              <h3 className="text-sm font-semibold">Active rules</h3>
              <span className="text-[11px] text-muted-foreground">({rulesQ.data?.count ?? 0})</span>
            </div>
            {rulesQ.isLoading ? (
              <LoadingState />
            ) : !rulesQ.data?.rules.length ? (
              <EmptyState>No rules yet — author one above.</EmptyState>
            ) : (
              <div className="space-y-2">
                {rulesQ.data.rules.map((r) => (
                  <div key={r.id} className="flex flex-wrap items-center gap-2 rounded-md border border-border p-2 text-[13px]">
                    <StatusChip label={r.enabled ? "on" : "off"} tone={r.enabled ? "ok" : "neutral"} />
                    <span className="font-medium">{r.name}</span>
                    <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px]">
                      IF {r.field} {r.op} {r.value}
                    </span>
                    <span className="text-muted-foreground">THEN {r.actions.map(actionLabel).join(", ")}</span>
                    <div className="ml-auto flex items-center gap-2">
                      <button
                        onClick={() => toggle.mutate(r)}
                        className="text-[11px] text-muted-foreground hover:text-foreground"
                      >
                        {r.enabled ? "disable" : "enable"}
                      </button>
                      <button
                        onClick={() => del.mutate(r.id)}
                        className="text-muted-foreground hover:text-foreground"
                        title="Delete"
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Card>

          {/* ---------------- Execution audit trail ---------------- */}
          <Card className="p-4 lg:col-span-2">
            <div className="mb-3 flex items-center gap-2">
              <h3 className="text-sm font-semibold">Execution log</h3>
              <span className="text-[11px] text-muted-foreground">audit trail</span>
            </div>
            {execQ.isLoading ? (
              <LoadingState />
            ) : !execQ.data?.executions.length ? (
              <EmptyState>No executions yet — test an event above.</EmptyState>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[560px] border-collapse text-[12px]">
                  <thead>
                    <tr className="text-left text-muted-foreground">
                      <th className="py-1 pr-3 font-medium">When</th>
                      <th className="py-1 pr-3 font-medium">Event</th>
                      <th className="py-1 pr-3 font-medium">Matched</th>
                      <th className="py-1 pr-3 font-medium">Actions fired</th>
                    </tr>
                  </thead>
                  <tbody>
                    {execQ.data.executions.map((ex, i) => {
                      const fired = ex.results.filter((r) => r.matched);
                      return (
                        <tr key={ex.ts + i} className="border-t border-border align-top">
                          <td className="py-1.5 pr-3 whitespace-nowrap text-muted-foreground">
                            {fmtDateTimeIST(ex.ts)}
                          </td>
                          <td className="py-1.5 pr-3 font-mono text-[11px]">{JSON.stringify(ex.event)}</td>
                          <td className="py-1.5 pr-3 tabular-nums">{ex.matched_count}</td>
                          <td className="py-1.5 pr-3 text-muted-foreground">
                            {fired.flatMap((r) => r.actions_fired).map(actionLabel).join(", ") || "—"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </div>
      )}
    </PageContainer>
  );
}
