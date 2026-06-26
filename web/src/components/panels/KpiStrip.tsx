import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { Area, AreaChart, ResponsiveContainer } from "recharts";
import { getAdapter } from "@/data";
import type { KpiResult } from "@/lib/types";
import { Card, CardContent } from "@/components/ui/card";
import { Spinner, EmptyState } from "@/components/ui/misc";
import { STATUS } from "@/lib/tokens";

// A row of KPI cards rendered from getAdapter().kpiStrip(). Each card shows the
// label, value+unit, target, a coloured Δ% (direction + onTarget aware) and a
// tiny trend sparkline (recharts area). Colours come from tokens.ts only.

/** Δ% colour: a move "the right way" + on-target reads green, otherwise amber/red. */
function deltaColour(k: KpiResult): string {
  const improving = k.direction === "lower_is_better" ? k.deltaPct < 0 : k.deltaPct > 0;
  if (k.onTarget) return STATUS.ok;
  return improving ? STATUS.warning : STATUS.critical;
}

function Sparkline({ trend, colour }: { trend: number[]; colour: string }) {
  const data = trend.map((v, i) => ({ i, v }));
  if (data.length < 2) return null;
  const gradId = `kpi-spark-${colour.replace("#", "")}`;
  return (
    <div className="h-8 w-full" aria-hidden>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 2, right: 0, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={colour} stopOpacity={0.5} />
              <stop offset="100%" stopColor={colour} stopOpacity={0} />
            </linearGradient>
          </defs>
          <Area
            type="monotone"
            dataKey="v"
            stroke={colour}
            strokeWidth={1.5}
            fill={`url(#${gradId})`}
            isAnimationActive={false}
            dot={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

function KpiCard({ k }: { k: KpiResult }) {
  const { t } = useTranslation();
  const colour = deltaColour(k);
  const sign = k.deltaPct > 0 ? "+" : "";
  return (
    <Card>
      <CardContent className="flex flex-col gap-1.5 py-3">
        <span
          className="truncate text-[11px] font-medium text-muted-foreground"
          title={t(`kpiLabel.${k.key}`, k.label)}
        >
          {t(`kpiLabel.${k.key}`, k.label)}
        </span>
        <div className="flex items-baseline gap-1">
          <span className="text-xl font-semibold tabular-nums">{k.value}</span>
          <span className="text-[11px] text-muted-foreground">{t(`kpiUnit.${k.unit}`, k.unit)}</span>
          <span
            className="ml-auto text-xs font-medium tabular-nums"
            style={{ color: colour }}
            title={k.onTarget ? t("kpi.onTarget") : t("kpi.offTarget")}
          >
            {sign}
            {k.deltaPct}%
          </span>
        </div>
        <Sparkline trend={k.trend} colour={colour} />
        <div className="text-[10px] text-muted-foreground tabular-nums">
          {t("kpi.target")} {k.target} · {t("kpi.baseline")} {k.baseline}
        </div>
      </CardContent>
    </Card>
  );
}

export function KpiStrip({ className }: { className?: string }) {
  const { t } = useTranslation();
  const q = useQuery({ queryKey: ["kpi-strip"], queryFn: () => getAdapter().kpiStrip() });
  const kpis = q.data ?? [];

  if (q.isLoading) {
    return (
      <div className="flex items-center gap-2 p-3 text-sm text-muted-foreground">
        <Spinner /> {t("common.loading")}
      </div>
    );
  }
  if (q.isError || kpis.length === 0) {
    return <EmptyState>{t("kpi.empty")}</EmptyState>;
  }

  return (
    <div className={className ?? "grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7"}>
      {kpis.map((k) => (
        <KpiCard key={k.key} k={k} />
      ))}
    </div>
  );
}

export default KpiStrip;
