// Before vs After chart.
//
// Each scenario answers a different shape of question, so the series are derived
// per scenario from the backend `result` — never from a fixture, and never
// recomputed here. This component only RESHAPES what the API returned:
//
//   modal-shift      hourly gate profile, baseline vs shifted   (+ capacity line)
//   gate-slotting    hourly arrivals, observed vs slotted       (+ capacity line)
//   berth-cascade    delay hours per displaced vessel
//   crane-productivity  berth-queue delay per displaced vessel
//   driver-shortage  trips baseline vs after the shortage
//
// If a scenario returns no series (empty window), the caller renders nothing
// rather than an axis with no bars.

import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { SimulationResult } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { LIMIT, buildSeries } from "./whatifSeries";

export function BeforeAfterChart({ result }: { result: SimulationResult }) {
  const series = buildSeries(result);
  if (!series) return null;

  const vertical = series.layout === "vertical";
  const height = vertical ? Math.max(200, series.data.length * 34 + 60) : 300;

  return (
    <Card className="p-3">
      <h3 className="mb-2 text-[13px] font-semibold text-foreground">{series.title}</h3>
      <div style={{ width: "100%", height }}>
        <ResponsiveContainer>
          <BarChart
            data={series.data}
            layout={vertical ? "vertical" : "horizontal"}
            margin={{ top: 4, right: 16, bottom: vertical ? 4 : 28, left: vertical ? 8 : 0 }}
            barGap={2}
          >
            <CartesianGrid strokeDasharray="3 3" stroke="currentColor" className="text-border" />
            {vertical ? (
              <>
                <XAxis type="number" tick={{ fontSize: 11 }} />
                <YAxis
                  type="category"
                  dataKey={series.xKey}
                  width={150}
                  tick={{ fontSize: 10.5 }}
                  interval={0}
                />
              </>
            ) : (
              <>
                <XAxis
                  dataKey={series.xKey}
                  tick={{ fontSize: 10 }}
                  angle={-45}
                  textAnchor="end"
                  height={52}
                  interval="preserveStartEnd"
                />
                <YAxis tick={{ fontSize: 11 }} />
              </>
            )}
            <Tooltip
              contentStyle={{ fontSize: 12, borderRadius: 8 }}
              cursor={{ fill: "currentColor", opacity: 0.06 }}
            />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            {series.reference && (
              <ReferenceLine
                {...(vertical ? { x: series.reference.value } : { y: series.reference.value })}
                stroke={LIMIT}
                strokeDasharray="4 3"
                label={{
                  value: series.reference.label,
                  fontSize: 10,
                  fill: LIMIT,
                  position: "insideTopRight",
                }}
              />
            )}
            {series.bars.map((b) => (
              <Bar
                key={b.key}
                dataKey={b.key}
                name={b.name}
                fill={b.colour}
                radius={[3, 3, 0, 0]}
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>
      {series.footnote && (
        <p className="mt-1.5 text-[10.5px] leading-snug text-muted-foreground">{series.footnote}</p>
      )}
    </Card>
  );
}
