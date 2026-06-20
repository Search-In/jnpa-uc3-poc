import { useQuery } from "@tanstack/react-query";
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { getAdapter } from "@/data";
import { STATUS } from "@/lib/tokens";

// Compact gate-throughput trend (recharts) sourced from the typed adapter's KPI
// strip. The `gate_throughput` KPI carries an 8-point trend (oldest → newest,
// baseline → current) which we plot as a corridor-wide throughput sparkline.
export function ThroughputChart() {
  const q = useQuery({
    queryKey: ["kpi", "throughput-trend"],
    queryFn: () => getAdapter().kpiStrip(),
    refetchInterval: 30_000,
  });

  const kpi = (q.data ?? []).find((k) => k.key === "gate_throughput");
  const data = (kpi?.trend ?? []).map((reads, i) => ({ i, reads }));

  return (
    <div className="h-full w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: -18 }}>
          <defs>
            <linearGradient id="tp" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={STATUS.info} stopOpacity={0.6} />
              <stop offset="100%" stopColor={STATUS.info} stopOpacity={0} />
            </linearGradient>
          </defs>
          <XAxis dataKey="i" hide />
          <YAxis tick={{ fontSize: 9, fill: STATUS.unknown }} width={28} />
          <Tooltip
            contentStyle={{ fontSize: 11 }}
            formatter={(v) => [`${v} ${kpi?.unit ?? "vph"}`, "throughput"]}
          />
          <Area
            type="monotone"
            dataKey="reads"
            stroke={STATUS.info}
            fill="url(#tp)"
            strokeWidth={2}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
