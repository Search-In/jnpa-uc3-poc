// UC3-035 — daily average, median, P90 and peak-hour ratio per KPI.
//
// Sits beside the existing KpiStrip on /live rather than on a second dashboard:
// the strip answers "where is this KPI now", this panel answers "what does its
// distribution look like", and they are the same KPIs read two ways.
//
// Every figure is computed in the database over per-trip rows (see
// /api/kpi/distribution). Nothing here recomputes a KPI in the browser — a
// number an operator may act on should not depend on which client rendered it.
//
// WHAT IS NOT RENDERED HERE. The payload also carries two engineering
// explanations: `skew_warning` (why a window's mean sits above its P90 — the
// outliers behind it are a known data-quality artifact) and `note` (that every
// figure is computed over per-trip rows and an unmeasured KPI reports null, not
// zero). Both REMAIN in /api/kpi/distribution for the reporting and diagnostic
// clients that need them; neither belongs on a control-room wallboard, where the
// operator acts on the value against its target. The DQ finding is still
// surfaced — through the API and the reports that read it — not deleted.
//
// No figure, threshold or computation changes with that omission: the panel
// renders exactly the numbers the endpoint returns, unmeasured KPIs still show
// an em-dash with samples: 0, and nothing is recomputed in the browser.
import { useQuery } from "@tanstack/react-query";
import { BarChart3 } from "lucide-react";

import { api } from "@/lib/api";
import { fmtDateTimeIST } from "@/lib/utils";
import { Card } from "@/components/ui/card";
import { StatusChip } from "@/components/ui/dtccc";
import type { KpiDistributionEntry } from "@/lib/types";

function Figure({
  label,
  value,
  unit,
  hint,
}: {
  label: string;
  value: number | null;
  unit: string;
  hint?: string;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="text-sm font-semibold tabular-nums">
        {/* An unmeasured KPI shows "—". A zero would read as a good measurement. */}
        {value === null ? "—" : value}
        {value !== null && (
          <span className="ml-1 text-[10px] font-normal text-muted-foreground">{unit}</span>
        )}
      </dd>
      {hint && <p className="text-[9px] leading-tight text-muted-foreground">{hint}</p>}
    </div>
  );
}

function Row({ d }: { d: KpiDistributionEntry }) {
  return (
    <li className="min-w-0 rounded-lg border border-border bg-muted/20 p-2.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <h4 className="text-[12px] font-semibold">{d.label}</h4>
        <StatusChip
          label={d.source === "live" ? `LIVE · ${d.samples} trips` : "BASELINE — no events yet"}
          tone={d.source === "live" ? "ok" : "neutral"}
        />
      </div>

      <dl className="mt-1.5 grid grid-cols-2 gap-2 sm:grid-cols-5">
        <Figure label="Daily average" value={d.daily_average} unit={d.unit} />
        <Figure label="Median" value={d.median} unit={d.unit} />
        <Figure label="P90" value={d.p90} unit={d.unit} />
        <Figure
          label="Peak-hour ratio"
          value={d.peak_hour_ratio}
          unit="×"
          hint={d.peak_hour_utc ? `peak ${fmtDateTimeIST(d.peak_hour_utc)}` : undefined}
        />
        <Figure label="Target" value={d.target} unit={d.unit} hint={`baseline ${d.baseline}`} />
      </dl>
    </li>
  );
}

export default function KpiDistributionPanel() {
  const q = useQuery({
    queryKey: ["kpi-distribution"],
    queryFn: () => api.kpiDistribution(24),
    refetchInterval: 30_000,
  });

  const entries = Object.values(q.data?.distribution ?? {});

  return (
    <Card className="min-w-0 p-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold">
          <BarChart3 className="h-4 w-4 shrink-0" aria-hidden />
          KPI distribution
        </h3>
        {q.data && <StatusChip label={`${q.data.window_hours}h window`} tone="info" />}
      </div>

      {q.isLoading && (
        <p className="mt-2 text-[12px] text-muted-foreground" role="status">
          Loading distribution…
        </p>
      )}
      {q.isError && (
        <p className="mt-2 text-[12px] text-severity-critical" role="alert">
          Distribution unavailable: {(q.error as Error).message}
        </p>
      )}
      {!q.isLoading && !q.isError && entries.length === 0 && (
        <p className="mt-2 text-[12px] text-muted-foreground">
          No KPI distribution available for this window.
        </p>
      )}

      {entries.length > 0 && (
        <>
          <ul className="mt-2 flex flex-col gap-2">
            {entries.map((d) => (
              <Row key={d.key} d={d} />
            ))}
          </ul>
        </>
      )}
    </Card>
  );
}
