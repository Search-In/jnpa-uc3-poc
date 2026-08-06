// Cargo What-If Dashboard — the operator surface for the JNPA What-If Notice
// (05 August 2026) scenarios.
//
// Every figure on this screen comes from /api/cargo/simulate/*. Nothing is
// computed, defaulted or mocked in the browser: the backend owns the arithmetic
// and returns the Notice §1 contract (method / result+figures / assumptions /
// queries / recommendations), and this screen lays that contract out.
//
// Layout, per the brief:
//     top     scenario selector (built from the backend catalog)
//     left    input parameters   right   result summary cards
//     below   before/after chart · scenario detail · assumptions ·
//             query trace · recommendations
//
// Distinct from /what-if (WhatIfConsole), which triggers the TFC-1/2/3 live
// injection scenarios and paints a WebSocket storyline. That console MUTATES
// state and needs a reset; this one is read-only and answers "what would it
// cost". They are different tools and deliberately live on different routes.

import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { FlaskConical, FileText } from "lucide-react";
import { api, type SimulationResult } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { PageContainer, PageHeader, DataTable, StatusChip, type Column } from "@/components/ui/dtccc";
import { ErrorState, LoadingState } from "@/components/ui/misc";
import { ScenarioSelector } from "@/components/whatif/ScenarioSelector";
import { ScenarioInputPanel } from "@/components/whatif/ScenarioInputPanel";
import { ResultCards } from "@/components/whatif/ResultCards";
import { BeforeAfterChart } from "@/components/whatif/BeforeAfterChart";
import { AssumptionsPanel } from "@/components/whatif/AssumptionsPanel";
import { QueryTracePanel } from "@/components/whatif/QueryTracePanel";
import { RecommendationList } from "@/components/whatif/RecommendationList";

function ts(v: unknown): string {
  if (!v) return "—";
  const d = new Date(String(v));
  if (Number.isNaN(d.getTime())) return String(v);
  return d.toISOString().slice(0, 16).replace("T", " ");
}

function num(v: unknown, digits = 2): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  return Number.isInteger(n) ? n.toLocaleString() : n.toFixed(digits);
}

/** Per-scenario detail tables — the rows behind the headline figures. */
function ScenarioDetail({ result }: { result: SimulationResult }) {
  const r = result.result ?? {};

  if (result.scenario === "berth-cascade") {
    const rows = (r.displaced_calls ?? []) as any[];
    if (!rows.length) return null;
    const columns: Column<any>[] = [
      { key: "vessel", header: "Vessel", render: (x) => x.vessel ?? "—" },
      { key: "voyage", header: "Voyage", render: (x) => x.voyage ?? "—" },
      { key: "berth", header: "Berth", render: (x) => x.berth ?? "—" },
      { key: "orig", header: "Original time", render: (x) => ts(x.original_time) },
      { key: "new", header: "New time", render: (x) => ts(x.new_time) },
      {
        key: "delay",
        header: "Delay (h)",
        align: "right",
        render: (x) => <span className="font-semibold tabular-nums">{num(x.delay_hours)}</span>,
      },
      {
        key: "assumed",
        header: "Duration",
        render: (x) =>
          x.duration_assumed ? <StatusChip label="assumed" tone="warn" /> : <span className="text-muted-foreground">reported</span>,
      },
    ];
    return (
      <Card className="p-3">
        <h3 className="mb-2 text-[13px] font-semibold text-foreground">
          Displaced calls <span className="text-muted-foreground">({rows.length})</span>
        </h3>
        <DataTable columns={columns} rows={rows} rowKey={(x) => `${x.vessel}-${x.voyage}`} pageSize={10} />
      </Card>
    );
  }

  if (result.scenario === "crane-productivity") {
    const before = r.before ?? {};
    const after = r.after ?? {};
    const target = r.target_call ?? {};
    const calls = (r.baseline_by_call ?? []) as any[];
    const columns: Column<any>[] = [
      { key: "vessel", header: "Vessel", render: (x) => x.vessel_name ?? "—" },
      { key: "berth", header: "Berth", render: (x) => x.berth_number ?? "—" },
      { key: "moves", header: "Gross moves", align: "right", render: (x) => num(x.gross_moves, 0) },
      { key: "hours", header: "Hours worked", align: "right", render: (x) => num(x.hours_worked) },
      {
        key: "rate",
        header: "Moves/hour",
        align: "right",
        render: (x) =>
          x.moves_per_hour === null || x.moves_per_hour === undefined ? (
            <StatusChip label="not derivable" tone="warn" />
          ) : (
            <span className="font-semibold tabular-nums">{num(x.moves_per_hour)}</span>
          ),
      },
      {
        key: "origin",
        header: "Moves source",
        render: (x) =>
          x.moves_data_origin ? (
            <StatusChip label={x.moves_data_origin} tone={x.moves_data_origin === "DERIVED" ? "info" : "ok"} />
          ) : (
            "—"
          ),
      },
    ];
    return (
      <div className="flex flex-col gap-3">
        <Card className="p-3">
          <h3 className="mb-2 text-[13px] font-semibold text-foreground">
            {target.vessel_name ?? "Target call"} — before vs after a {num(result.figures.turnaround_increase_pct, 1)}% longer operation
          </h3>
          <div className="grid gap-3 sm:grid-cols-2">
            {[
              { label: "Before", data: before, tone: "text-emerald-600" },
              { label: "After", data: after, tone: "text-amber-600" },
            ].map((side) => (
              <div key={side.label} className="rounded-md border border-border p-2.5">
                <div className={`mb-1 text-[11px] font-semibold uppercase ${side.tone}`}>{side.label}</div>
                <dl className="flex flex-col gap-1">
                  <div className="flex justify-between text-[12px]">
                    <dt className="text-muted-foreground">Moves per hour</dt>
                    <dd className="font-semibold tabular-nums">{num(side.data.moves_per_hour)}</dd>
                  </div>
                  <div className="flex justify-between text-[12px]">
                    <dt className="text-muted-foreground">Hours worked</dt>
                    <dd className="font-semibold tabular-nums">{num(side.data.hours_worked)}</dd>
                  </div>
                  <div className="flex justify-between text-[12px]">
                    <dt className="text-muted-foreground">Operation end</dt>
                    <dd className="font-semibold tabular-nums">{ts(side.data.operation_end)}</dd>
                  </div>
                </dl>
              </div>
            ))}
          </div>
        </Card>
        {calls.length > 0 && (
          <Card className="p-3">
            <h3 className="mb-2 text-[13px] font-semibold text-foreground">
              Effective productivity by call <span className="text-muted-foreground">({calls.length})</span>
            </h3>
            <DataTable
              columns={columns}
              rows={calls}
              rowKey={(x) => String(x.berthing_record_id ?? x.vessel_name)}
              pageSize={10}
            />
          </Card>
        )}
      </div>
    );
  }

  if (result.scenario === "modal-shift") {
    const first = r.first_constraint;
    const saturated = (r.saturated_hours ?? []) as any[];
    return (
      <Card className="flex flex-col gap-2 p-3">
        <h3 className="text-[13px] font-semibold text-foreground">Saturation</h3>
        {r.gate_absorbs_load ? (
          <p className="text-[12px] text-emerald-600">
            The gate absorbs the additional load — no hour exceeds the sustained rate after the shift.
          </p>
        ) : (
          <>
            {first && (
              <div className="rounded-md border border-severity-critical/40 bg-severity-critical/5 p-2.5">
                <div className="text-[11px] font-semibold uppercase text-severity-critical">
                  First constraint to saturate
                </div>
                <div className="mt-0.5 text-[13px] font-semibold text-foreground">
                  {String(first.constraint).replace(/_/g, " ")} at {ts(first.hour)}
                </div>
                <div className="text-[11.5px] text-muted-foreground">
                  ceiling {num(first.ceiling)} · load {num(first.load)} · excess {num(first.excess)}
                </div>
              </div>
            )}
            {saturated.length > 0 && (
              <div>
                <div className="mb-1 text-[11px] font-medium text-muted-foreground">
                  Saturated hours ({saturated.length})
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {saturated.slice(0, 24).map((s, i) => (
                    <span
                      key={i}
                      className="rounded bg-severity-critical/10 px-1.5 py-0.5 text-[10.5px] text-severity-critical"
                      title={`load ${num(s.load)} vs ceiling ${num(s.ceiling)}`}
                    >
                      {ts(s.hour).slice(5)} +{num(s.excess, 0)}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </Card>
    );
  }

  if (result.scenario === "gate-slotting") {
    const pattern = r.arrival_pattern ?? {};
    const saturated = (r.saturated_periods ?? []) as any[];
    return (
      <Card className="flex flex-col gap-2 p-3">
        <h3 className="text-[13px] font-semibold text-foreground">Arrival pattern</h3>
        <div className="flex flex-wrap gap-4 text-[12px]">
          <div>
            <span className="text-muted-foreground">Shape: </span>
            <StatusChip label={pattern.shape ?? "—"} tone={pattern.shape === "PEAKED" ? "warn" : "neutral"} />
          </div>
          <div>
            <span className="text-muted-foreground">Peak hour: </span>
            <span className="font-semibold">{ts(pattern.peak_hour)}</span>
          </div>
          <div>
            <span className="text-muted-foreground">Peak: </span>
            <span className="font-semibold tabular-nums">{num(pattern.peak_arrivals, 0)}</span>
          </div>
          <div>
            <span className="text-muted-foreground">Peak / mean: </span>
            <span className="font-semibold tabular-nums">{num(pattern.peak_to_mean_ratio)}×</span>
          </div>
        </div>
        {saturated.length > 0 && (
          <div>
            <div className="mb-1 text-[11px] font-medium text-muted-foreground">
              Periods where arrivals exceed the sustained rate ({saturated.length})
            </div>
            <div className="flex flex-wrap gap-1.5">
              {saturated.slice(0, 30).map((s, i) => (
                <span
                  key={i}
                  className="rounded bg-amber-100 px-1.5 py-0.5 text-[10.5px] text-amber-800 dark:bg-amber-950/40 dark:text-amber-300"
                  title={`${num(s.arrivals, 0)} arrivals vs ${num(s.sustained_rate)} sustained`}
                >
                  {ts(s.hour).slice(5)} +{num(s.excess, 0)}
                </span>
              ))}
            </div>
          </div>
        )}
      </Card>
    );
  }

  if (result.scenario === "driver-shortage") {
    const exposed = r.exposed_transporters ?? {};
    const absolute = (exposed.by_absolute_loss ?? []) as any[];
    const structural = (exposed.by_structural_dependence ?? []) as any[];
    const flows = (r.exposed_cargo_flows ?? []) as any[];
    const state = r.state_on_report_date ?? {};

    const tCols: Column<any>[] = [
      { key: "t", header: "Transporter", render: (x) => x.transporter },
      { key: "v", header: "Vehicles", align: "right", render: (x) => num(x.vehicles, 0) },
      { key: "trips", header: "Trips", align: "right", render: (x) => num(x.trips, 0) },
      { key: "lost", header: "Trips lost", align: "right",
        render: (x) => <span className="font-semibold tabular-nums text-severity-critical">{num(x.trips_lost, 0)}</span> },
      { key: "pct", header: "Loss %", align: "right", render: (x) => `${num(x.trips_lost_pct, 1)}%` },
      { key: "tpv", header: "Trips / vehicle-day", align: "right", render: (x) => num(x.trips_per_vehicle_day) },
    ];
    const fCols: Column<any>[] = [
      { key: "flow", header: "Cargo flow", render: (x) => x.flow ?? "—" },
      { key: "fac", header: "Facility", render: (x) => x.facility ?? "—" },
      { key: "trips", header: "Trips", align: "right", render: (x) => num(x.trips, 0) },
      { key: "lost", header: "Trips lost", align: "right",
        render: (x) => <span className="font-semibold tabular-nums text-severity-critical">{num(x.trips_lost, 0)}</span> },
      { key: "veh", header: "Vehicles", align: "right", render: (x) => num(x.vehicles, 0) },
    ];

    return (
      <div className="flex flex-col gap-3">
        <Card className="p-3">
          <h3 className="mb-2 text-[13px] font-semibold text-foreground">
            State on {String(r.state_date ?? "the report date")}
          </h3>
          <div className="grid gap-2 sm:grid-cols-3">
            {[
              ["Containers not evacuated", state.containers_not_evacuated],
              ["Already pending in port", state.already_pending_in_port],
              ["Projected awaiting evacuation", state.projected_total_awaiting_evacuation],
            ].map(([label, v]) => (
              <div key={String(label)} className="rounded-md border border-border p-2.5">
                <div className="text-[18px] font-bold tabular-nums text-foreground">{num(v, 0)}</div>
                <div className="text-[11px] text-muted-foreground">{String(label)}</div>
              </div>
            ))}
          </div>
        </Card>

        {absolute.length > 0 && (
          <Card className="p-3">
            <h3 className="mb-1 text-[13px] font-semibold text-foreground">
              Most exposed transporters — by absolute trips lost
            </h3>
            <p className="mb-2 text-[11px] text-muted-foreground">
              The biggest contributors to the shortfall.
            </p>
            <DataTable columns={tCols} rows={absolute} rowKey={(x) => `abs-${x.transporter}`} pageSize={5} />
          </Card>
        )}

        {structural.length > 0 && (
          <Card className="p-3">
            <h3 className="mb-1 text-[13px] font-semibold text-foreground">
              Most exposed transporters — by structural dependence
            </h3>
            <p className="mb-2 text-[11px] text-muted-foreground">
              Highest trips per vehicle-day: a one-third cut removes a whole cycle here.
            </p>
            <DataTable columns={tCols} rows={structural} rowKey={(x) => `str-${x.transporter}`} pageSize={5} />
          </Card>
        )}

        {flows.length > 0 && (
          <Card className="p-3">
            <h3 className="mb-2 text-[13px] font-semibold text-foreground">
              Most exposed cargo flows
            </h3>
            <DataTable columns={fCols} rows={flows} rowKey={(x) => `${x.flow}-${x.facility}`} pageSize={5} />
          </Card>
        )}
      </div>
    );
  }

  return null;
}

export default function CargoWhatIf() {
  const catalog = useQuery({
    queryKey: ["cargo-simulate", "scenarios"],
    queryFn: api.simulateScenarios,
    staleTime: 5 * 60_000,
  });

  // Memoised so the identity is stable across renders — an inline `?? []` would
  // be a new array every time and re-run every dependent hook.
  const scenarios = useMemo(() => catalog.data?.scenarios ?? [], [catalog.data]);
  const [selected, setSelected] = useState<string | null>(null);
  const active = selected ?? scenarios[0]?.scenario ?? null;
  const entry = useMemo(
    () => scenarios.find((s) => s.scenario === active),
    [scenarios, active],
  );

  const run = useMutation({
    mutationFn: (vars: { scenario: string; body: Record<string, unknown> }) =>
      api.simulate(vars.scenario, vars.body),
  });

  // The result belongs to the scenario it was run for — switching scenario must
  // not leave a stale answer sitting under a different question.
  const result =
    run.data && run.variables?.scenario === active ? (run.data as SimulationResult) : null;

  return (
    <PageContainer>
      <PageHeader
        icon={FlaskConical}
        title="Cargo What-If"
        subtitle="JNPA What-If Notice scenarios — every answer carries its method, assumptions and query trace"
      />

      <div className="flex flex-col gap-3 p-4">
        {catalog.isLoading && <LoadingState label="Loading scenario catalog…" />}
        {catalog.isError && (
          <ErrorState onRetry={() => catalog.refetch()} detail={String(catalog.error)} />
        )}

        {scenarios.length > 0 && (
          <>
            <ScenarioSelector
              scenarios={scenarios}
              value={active}
              onChange={(s) => {
                setSelected(s);
                run.reset();
              }}
              disabled={run.isPending}
            />

            <div className="grid gap-3 lg:grid-cols-[320px_minmax(0,1fr)]">
              <div className="flex flex-col gap-3">
                {active && (
                  <ScenarioInputPanel
                    scenario={active}
                    entry={entry}
                    running={run.isPending}
                    onRun={(body) => active && run.mutate({ scenario: active, body })}
                  />
                )}
              </div>

              <div className="flex min-w-0 flex-col gap-3">
                {run.isPending && <LoadingState label="Running simulation…" />}

                {run.isError && (
                  <ErrorState onRetry={() => run.reset()} detail={String(run.error)} />
                )}

                {!run.isPending && !run.isError && !result && (
                  <Card className="flex flex-col items-center gap-1.5 p-8 text-center">
                    <FlaskConical className="h-7 w-7 text-muted-foreground" />
                    <p className="text-[13px] font-medium text-foreground">
                      Set the parameters and run the scenario
                    </p>
                    <p className="max-w-md text-[11.5px] leading-snug text-muted-foreground">
                      {entry?.question}
                    </p>
                  </Card>
                )}

                {result && (
                  <>
                    <Card className="flex items-start gap-2 p-3">
                      <FileText className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                      <div>
                        <h3 className="text-[12px] font-semibold text-foreground">Method</h3>
                        <p className="mt-0.5 text-[11.5px] leading-snug text-muted-foreground">
                          {result.method}
                        </p>
                      </div>
                    </Card>
                    <ResultCards result={result} />
                  </>
                )}
              </div>
            </div>

            {result && (
              <div className="flex flex-col gap-3">
                {result.data_available && <BeforeAfterChart result={result} />}
                {result.data_available && <ScenarioDetail result={result} />}
                <AssumptionsPanel assumptions={result.assumptions} />
                <QueryTracePanel queries={result.queries} />
                <RecommendationList recommendations={result.recommendations} />
              </div>
            )}
          </>
        )}
      </div>
    </PageContainer>
  );
}
