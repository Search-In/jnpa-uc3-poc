import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { getAdapter } from "@/data";
import type { KpiResult } from "@/lib/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Spinner, EmptyState } from "@/components/ui/misc";
import { STATUS } from "@/lib/tokens";

// Empty-container repositioning board (capability C3): a table of probable depot
// allocations from emptyAllocations() plus the TRT-from-ECD KPI card from
// emptyTrtKpi(). Demand → supply-depot matches are explainable (distance + TRT).

function TrtKpiCard({ kpi }: { kpi: KpiResult }) {
  const colour = kpi.onTarget ? STATUS.ok : STATUS.warning;
  const sign = kpi.deltaPct > 0 ? "+" : "";
  return (
    <div className="flex items-center justify-between rounded-md border border-border bg-background px-3 py-2">
      <div>
        <div className="text-[11px] text-muted-foreground">{kpi.label}</div>
        <div className="text-lg font-semibold tabular-nums">
          {kpi.value}
          <span className="ml-1 text-[11px] font-normal text-muted-foreground">{kpi.unit}</span>
        </div>
      </div>
      <Badge colour={colour}>
        {sign}
        {kpi.deltaPct}%
      </Badge>
    </div>
  );
}

export function EmptyContainerBoard({ limit = 8 }: { limit?: number }) {
  const { t } = useTranslation();
  const allocQ = useQuery({
    queryKey: ["empty-allocations"],
    queryFn: () => getAdapter().emptyAllocations(),
  });
  const kpiQ = useQuery({ queryKey: ["empty-trt-kpi"], queryFn: () => getAdapter().emptyTrtKpi() });

  const allocations = (allocQ.data ?? []).slice(0, limit);

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("panels.empty.title")}</CardTitle>
        <p className="text-[11px] text-muted-foreground">{t("panels.empty.subtitle")}</p>
      </CardHeader>
      <CardContent className="space-y-3">
        {kpiQ.data && <TrtKpiCard kpi={kpiQ.data} />}
        {allocQ.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner /> {t("common.loading")}
          </div>
        ) : allocations.length === 0 ? (
          <EmptyState>{t("panels.empty.empty")}</EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="border-b border-border text-left text-[10px] uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th className="px-2 py-1.5">{t("panels.empty.demand")}</th>
                  <th className="px-2 py-1.5">{t("panels.empty.depot")}</th>
                  <th className="px-2 py-1.5">{t("panels.empty.containerType")}</th>
                  <th className="px-2 py-1.5">{t("panels.empty.cargoType")}</th>
                  <th className="px-2 py-1.5 text-right">{t("panels.empty.distance")}</th>
                  <th className="px-2 py-1.5 text-right">{t("panels.empty.estTrt")}</th>
                  <th className="px-2 py-1.5 text-right">{t("panels.empty.confidence")}</th>
                </tr>
              </thead>
              <tbody>
                {allocations.map((a) => (
                  <tr key={a.demand_id} className="border-b border-border/50 hover:bg-muted/40">
                    <td className="px-2 py-1.5 font-mono">{a.demand_id}</td>
                    <td className="px-2 py-1.5">{a.supply_depot}</td>
                    <td className="px-2 py-1.5 font-mono">{a.container_type}</td>
                    <td className="px-2 py-1.5">{a.cargo_type}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums">{a.distance_km} km</td>
                    <td className="px-2 py-1.5 text-right tabular-nums">{a.est_trt_min} min</td>
                    <td className="px-2 py-1.5 text-right tabular-nums">
                      {a.confidence != null ? `${Math.round(a.confidence * 100)}%` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default EmptyContainerBoard;
