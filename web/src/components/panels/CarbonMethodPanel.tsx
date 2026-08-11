// T-11 — Carbon method panel + idle-CO₂e scenario delta (UC3-036).
//
// Tender UC3-R6 asks for carbon calculated from fleet/CPP data; WS4 KPI 11 and
// EC-7 ask for the idle-CO₂e delta against do-nothing. The acceptance bar is
// specific: an evaluator must get from the headline number to the FACTOR, its
// SOURCE and the ASSUMPTION in two clicks.
//
// So this panel renders /api/carbon/method rather than a copy of the numbers.
// The factors used to be duplicated in this file as a TS literal "kept in sync"
// with carbon/factors.py — which is a promise, not a mechanism. Reading them from
// the service that applies them makes drift impossible: there is one number, in
// one place, and the panel shows the arithmetic that produced it beside the
// published source that justifies it.
//
// Provenance is stated plainly: the FACTORS are published (IPCC / DEFRA / GLEC /
// EPA), the ACTIVITY DATA is simulated. No fleet-transporter fuel API exists
// pre-award, so every derived figure is tagged simulated.
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { BookOpen, Fuel, TrendingDown } from "lucide-react";

import { api } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { StatusChip } from "@/components/ui/dtccc";

/**
 * Monsoon Friday idle minutes (WS5 chain output), used as the default arms of
 * the delta. They are INPUTS shown on screen, not a hard-coded result: the panel
 * renders whatever CO₂e the service computes from them with the published
 * factors, so an evaluator can change the minutes and check the arithmetic.
 */
const MONSOON_FRIDAY = {
  scenario: "monsoon_friday",
  baseline_idle_minutes: 3582,
  scenario_idle_minutes: 2687,
  vehicle_class: "HGV",
};

function FactorRow({
  factor,
  sources,
}: {
  factor: {
    vehicle_class: string;
    value: number;
    unit: string;
    source: string;
    derivation: string;
  };
  sources: Record<string, string>;
}) {
  const [open, setOpen] = useState(false);
  return (
    <li className="min-w-0 border-b border-border/60 py-1.5 last:border-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full min-w-0 items-baseline justify-between gap-2 text-left"
      >
        <span className="text-[12px] font-medium">{factor.vehicle_class}</span>
        <span className="shrink-0 text-[12px] tabular-nums">
          {factor.value} <span className="text-[10px] text-muted-foreground">{factor.unit}</span>
        </span>
      </button>
      <p className="mt-0.5 break-words text-[10px] leading-snug text-muted-foreground">
        {factor.derivation}
      </p>
      {/* Second click: the published source behind the number. */}
      {open && (
        <p className="mt-1 break-words rounded-md bg-muted/50 p-1.5 text-[10px] leading-snug text-muted-foreground">
          <span className="font-semibold text-foreground">{factor.source}</span> —{" "}
          {sources[factor.source] ?? "source not recorded"}
        </p>
      )}
    </li>
  );
}

export default function CarbonMethodPanel() {
  const methodQ = useQuery({
    queryKey: ["carbon-method"],
    queryFn: () => api.carbonMethod(),
    staleTime: 60 * 60 * 1000, // published constants; they do not move
  });
  const deltaQ = useQuery({
    queryKey: ["carbon-idle-delta", MONSOON_FRIDAY.scenario],
    queryFn: () => api.carbonIdleDelta(MONSOON_FRIDAY),
  });

  const method = methodQ.data;
  const delta = deltaQ.data;

  return (
    <div className="flex min-w-0 flex-col gap-3">
      {methodQ.isLoading && <p className="text-[12px] text-muted-foreground">Loading method…</p>}
      {methodQ.isError && (
        <p className="text-[12px] text-severity-critical">
          Method unavailable: {(methodQ.error as Error).message}
        </p>
      )}

      {/* --- idle CO2e scenario delta (EC-7) --- */}
      <Card className="min-w-0 p-3">
        <h3 className="flex flex-wrap items-center gap-1.5 text-sm font-semibold">
          <TrendingDown className="h-4 w-4 shrink-0" aria-hidden />
          Idle CO₂e — scenario vs do-nothing
          <StatusChip label="SIMULATED" tone="warn" />
        </h3>
        {deltaQ.isLoading && <p className="mt-2 text-[12px] text-muted-foreground">Computing…</p>}
        {deltaQ.isError && (
          <p className="mt-2 text-[12px] text-severity-critical">
            {(deltaQ.error as Error).message}
          </p>
        )}
        {delta && (
          <>
            <dl className="mt-2 grid grid-cols-3 gap-2">
              <div className="min-w-0">
                <dt className="text-[10px] uppercase text-muted-foreground">Do-nothing</dt>
                <dd className="text-lg font-semibold tabular-nums">
                  {Math.round(delta.baseline.idle_co2e_kg)}
                  <span className="ml-1 text-[10px] font-normal text-muted-foreground">kg</span>
                </dd>
                <p className="text-[10px] text-muted-foreground">
                  {delta.baseline.idle_minutes} idle min
                </p>
              </div>
              <div className="min-w-0">
                <dt className="text-[10px] uppercase text-muted-foreground">
                  {delta.scenario ?? "scenario"}
                </dt>
                <dd className="text-lg font-semibold tabular-nums">
                  {Math.round(delta.scenario_run.idle_co2e_kg)}
                  <span className="ml-1 text-[10px] font-normal text-muted-foreground">kg</span>
                </dd>
                <p className="text-[10px] text-muted-foreground">
                  {delta.scenario_run.idle_minutes} idle min
                </p>
              </div>
              <div className="min-w-0">
                <dt className="text-[10px] uppercase text-muted-foreground">Delta</dt>
                <dd
                  className={
                    "text-lg font-semibold tabular-nums " +
                    (delta.improvement
                      ? "text-emerald-600 dark:text-emerald-400"
                      : "text-severity-critical")
                  }
                >
                  {delta.delta_kg > 0 ? "+" : ""}
                  {Math.round(delta.delta_kg)}
                  <span className="ml-1 text-[10px] font-normal text-muted-foreground">kg</span>
                </dd>
                {delta.delta_pct !== null && (
                  <p className="text-[10px] text-muted-foreground">{delta.delta_pct}%</p>
                )}
              </div>
            </dl>
            <p className="mt-2 break-words rounded-md bg-muted/40 p-1.5 font-mono text-[10px] leading-snug text-muted-foreground">
              {delta.method.formula} · idle factor {delta.idle_factor_gco2e_per_min} gCO₂e/min (
              {delta.vehicle_class})
            </p>
          </>
        )}
      </Card>

      {/* --- the factors, each with its source (click 2) --- */}
      {method && (
        <>
          <Card className="min-w-0 p-3">
            <h3 className="flex items-center gap-1.5 text-sm font-semibold">
              <Fuel className="h-4 w-4 shrink-0" aria-hidden />
              Idle emission factors
            </h3>
            <p className="mt-1 break-words font-mono text-[10px] text-muted-foreground">
              {method.idle.formula}
            </p>
            <ul className="mt-1.5 flex flex-col">
              {method.idle.factors.map((f) => (
                <FactorRow key={f.vehicle_class} factor={f} sources={method.sources} />
              ))}
            </ul>
            {method.idle.constants?.map((c) => (
              <p key={c.key} className="mt-1.5 text-[10px] leading-snug text-muted-foreground">
                <span className="font-mono">{c.key}</span> = {c.value} {c.unit} — {c.basis}
              </p>
            ))}
          </Card>

          <Card className="min-w-0 p-3">
            <h3 className="flex items-center gap-1.5 text-sm font-semibold">
              <Fuel className="h-4 w-4 shrink-0" aria-hidden />
              Moving emission factors
            </h3>
            <p className="mt-1 break-words font-mono text-[10px] text-muted-foreground">
              {method.moving.formula}
            </p>
            <ul className="mt-1.5 flex flex-col">
              {method.moving.factors.map((f) => (
                <FactorRow key={f.vehicle_class} factor={f} sources={method.sources} />
              ))}
            </ul>
          </Card>

          {/* --- the assumption the whole method rests on --- */}
          <Card className="min-w-0 border-amber-500/40 bg-amber-500/5 p-3">
            <h3 className="flex flex-wrap items-center gap-1.5 text-sm font-semibold">
              <BookOpen className="h-4 w-4 shrink-0" aria-hidden />
              Assumption {method.assumption_ref}
            </h3>
            <p className="mt-1 break-words text-[11px] leading-snug text-muted-foreground">
              {method.assumption_text}
            </p>
            <div className="mt-2 flex flex-wrap gap-1.5">
              <StatusChip
                label={method.factors_are_published ? "FACTORS: PUBLISHED" : "FACTORS: UNKNOWN"}
                tone="ok"
              />
              <StatusChip
                label={
                  method.activity_data_is_simulated
                    ? "ACTIVITY DATA: SIMULATED"
                    : "ACTIVITY DATA: MEASURED"
                }
                tone="warn"
              />
            </div>
          </Card>

          <Card className="min-w-0 p-3">
            <h3 className="text-sm font-semibold">Sources</h3>
            <dl className="mt-1.5 flex flex-col gap-1.5">
              {Object.entries(method.sources).map(([key, text]) => (
                <div key={key} className="min-w-0">
                  <dt className="font-mono text-[10px] font-semibold">{key}</dt>
                  <dd className="break-words text-[10px] leading-snug text-muted-foreground">
                    {text}
                  </dd>
                </div>
              ))}
            </dl>
          </Card>
        </>
      )}
    </div>
  );
}
