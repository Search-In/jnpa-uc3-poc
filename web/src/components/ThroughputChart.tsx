import { useQuery } from "@tanstack/react-query";
import {
  Area,
  AreaChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "@/lib/api";
import { fmtTimeIST } from "@/lib/utils";

// Compact gate-throughput trend (recharts) from the materialised KPI view
// jnpa.kpi_gate_throughput. Buckets are hourly; we sum reads across gates per
// bucket to show corridor-wide throughput over the last 24 h.
export function ThroughputChart() {
  const q = useQuery({ queryKey: ["kpi"], queryFn: api.kpi, refetchInterval: 30_000 });
  const rows = (q.data?.views?.throughput ?? []) as { bucket: string; reads: number }[];

  // Aggregate reads per bucket (the view is one row per (bucket, gate)).
  const byBucket = new Map<string, number>();
  for (const r of rows) byBucket.set(r.bucket, (byBucket.get(r.bucket) ?? 0) + Number(r.reads || 0));
  const data = [...byBucket.entries()]
    .map(([bucket, reads]) => ({ bucket, reads }))
    .sort((a, b) => a.bucket.localeCompare(b.bucket))
    .slice(-24);

  return (
    <div className="h-full w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: -18 }}>
          <defs>
            <linearGradient id="tp" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#56B4E9" stopOpacity={0.6} />
              <stop offset="100%" stopColor="#56B4E9" stopOpacity={0} />
            </linearGradient>
          </defs>
          <XAxis
            dataKey="bucket"
            tickFormatter={(v) => fmtTimeIST(v).slice(0, 5)}
            tick={{ fontSize: 9, fill: "#8aa" }}
            interval="preserveStartEnd"
          />
          <YAxis tick={{ fontSize: 9, fill: "#8aa" }} width={28} />
          <Tooltip
            contentStyle={{ background: "#0b1220", border: "1px solid #2a3344", fontSize: 11 }}
            labelFormatter={(v) => fmtTimeIST(String(v))}
            formatter={(v) => [`${v} reads`, "throughput"]}
          />
          <Area type="monotone" dataKey="reads" stroke="#56B4E9" fill="url(#tp)" strokeWidth={2} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
