import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { getAdapter } from "@/data";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Spinner, EmptyState } from "@/components/ui/misc";
import { STATUS, OKABE_ITO } from "@/lib/tokens";

// Compact carbon-footprint tile (capability C6): total CO2e (kg + tonnes),
// vehicle_count, a moving/idle split bar from by_source, and a by_class
// breakdown. Emission factors are documented IPCC/GHG-Protocol constants.

// A stable colour ramp (tokens only) for the by_class breakdown bars.
const CLASS_COLOURS = [
  OKABE_ITO.blue,
  OKABE_ITO.skyBlue,
  OKABE_ITO.reddishPurple,
  OKABE_ITO.grey,
  OKABE_ITO.orange,
] as const;

export function CarbonTile() {
  const { t } = useTranslation();
  const q = useQuery({ queryKey: ["carbon-rollup"], queryFn: () => getAdapter().carbonRollup() });
  const c = q.data;

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("panels.carbon.title")}</CardTitle>
        <p className="text-[11px] text-muted-foreground">{t("panels.carbon.subtitle")}</p>
      </CardHeader>
      <CardContent className="space-y-3">
        {q.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner /> {t("common.loading")}
          </div>
        ) : !c ? (
          <EmptyState>{t("panels.carbon.empty")}</EmptyState>
        ) : (
          <>
            <div className="flex items-end justify-between">
              <div>
                <div className="text-[11px] text-muted-foreground">{t("panels.carbon.total")}</div>
                <div className="text-2xl font-semibold tabular-nums">
                  {(c.total_kg / 1000).toFixed(2)}
                  <span className="ml-1 text-xs font-normal text-muted-foreground">t CO₂e</span>
                </div>
                <div className="text-[10px] text-muted-foreground tabular-nums">
                  {c.total_kg.toLocaleString()} kg
                </div>
              </div>
              <div className="text-right">
                <div className="text-lg font-semibold tabular-nums">{c.vehicle_count}</div>
                <div className="text-[10px] text-muted-foreground">
                  {t("panels.carbon.vehicles")}
                </div>
              </div>
            </div>

            {/* moving vs idle split */}
            <div>
              <div className="mb-1 flex justify-between text-[10px] text-muted-foreground">
                <span style={{ color: STATUS.ok }}>
                  {t("panels.carbon.moving")} {Math.round((c.by_source.moving / c.total_kg) * 100)}%
                </span>
                <span style={{ color: STATUS.warning }}>
                  {t("panels.carbon.idle")} {Math.round((c.by_source.idle / c.total_kg) * 100)}%
                </span>
              </div>
              <div className="flex h-2 w-full overflow-hidden rounded-full bg-muted">
                <div
                  style={{
                    width: `${(c.by_source.moving / c.total_kg) * 100}%`,
                    backgroundColor: STATUS.ok,
                  }}
                />
                <div
                  style={{
                    width: `${(c.by_source.idle / c.total_kg) * 100}%`,
                    backgroundColor: STATUS.warning,
                  }}
                />
              </div>
            </div>

            {/* by class */}
            <div>
              <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
                {t("panels.carbon.byClass")}
              </div>
              <div className="space-y-1">
                {Object.entries(c.by_class).map(([cls, kg], i) => {
                  const max = Math.max(...Object.values(c.by_class), 1);
                  const colour = CLASS_COLOURS[i % CLASS_COLOURS.length];
                  return (
                    <div key={cls} className="flex items-center gap-2">
                      <span className="w-14 shrink-0 text-[11px]">{cls}</span>
                      <div className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
                        <div
                          className="h-full"
                          style={{ width: `${(kg / max) * 100}%`, backgroundColor: colour }}
                        />
                      </div>
                      <span className="w-14 shrink-0 text-right text-[10px] tabular-nums text-muted-foreground">
                        {kg.toLocaleString()} kg
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

export default CarbonTile;
