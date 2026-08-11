import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { Info } from "lucide-react";
import { getAdapter } from "@/data";
import { CollapsibleCard } from "@/components/ui/CollapsibleCard";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Spinner, EmptyState } from "@/components/ui/misc";
import { STATUS, OKABE_ITO } from "@/lib/tokens";
import CarbonMethodPanel from "@/components/panels/CarbonMethodPanel";
import type { CarbonRollup } from "@/lib/types";

// "How CO₂ is calculated" — a plain-language methodology dialog so a new user can
// see exactly how the numbers are derived (no black box). Opened from the ⓘ button
// in the Carbon Footprint card header.
function CarbonMethodologyDialog() {
  const { t } = useTranslation();

  return (
    <Dialog>
      <DialogTrigger asChild>
        <button
          type="button"
          // stop the click bubbling to the CollapsibleCard header (which toggles collapse)
          onClick={(e) => e.stopPropagation()}
          aria-label={t("panels.carbon.howCalculated", {
            defaultValue: "How CO₂ is calculated",
          })}
          title={t("panels.carbon.howCalculated", { defaultValue: "How CO₂ is calculated" })}
          className="flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground transition hover:bg-muted hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Info className="h-4 w-4" />
        </button>
      </DialogTrigger>
      <DialogContent onClick={(e) => e.stopPropagation()}>
        <DialogHeader>
          <DialogTitle>
            {t("panels.carbon.howCalculated", { defaultValue: "How CO₂ is calculated" })}
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-4 p-4 text-sm">
          <p className="text-muted-foreground">
            Each truck's CO₂e is the distance it travelled multiplied by an emission factor for its
            vehicle class, plus any idle-engine emissions.
          </p>

          <div className="rounded-md border border-border bg-muted/40 p-3 font-mono text-[13px]">
            CO₂ = Distance&nbsp;×&nbsp;Vehicle emission factor&nbsp;(+
            idle&nbsp;time&nbsp;×&nbsp;idle&nbsp;factor)
          </div>

          {/* UC3-036: factors, sources and the assumption come from
              /api/carbon/method — the same constants the service applies — so the
              panel cannot drift from the calculation. */}
          <CarbonMethodPanel />
        </div>
      </DialogContent>
    </Dialog>
  );
}

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

// Colour a vehicle class by its position in the rollup's by_class key order, so
// the breakdown table and the by-class bars below always agree on a category's
// colour even though they sort their rows differently.
function classColour(cls: string, order: string[]): string {
  const i = order.indexOf(cls);
  return CLASS_COLOURS[(i < 0 ? 0 : i) % CLASS_COLOURS.length];
}

// Fleet composition behind the CO₂e figure: which vehicle categories were
// counted, and how many of each. Sourced from `vehicles_by_class` on the rollup
// (carbon/calculator.py). When the upstream response omits it, the section says
// so plainly instead of inferring or hard-coding a split — the operator must be
// able to tell "no breakdown available" from "breakdown is this".
function VehicleBreakdown({ rollup }: { rollup: CarbonRollup }) {
  const { t } = useTranslation();
  const counts = rollup.vehicles_by_class ?? {};
  // Descending by count so the dominant category reads first.
  const rows = Object.entries(counts)
    .filter(([, n]) => Number.isFinite(n))
    .sort((a, b) => b[1] - a[1]);
  const breakdownTotal = rows.reduce((sum, [, n]) => sum + n, 0);
  const classOrder = Object.keys(rollup.by_class);

  return (
    <div>
      <div className="mb-1.5 text-[10px] uppercase tracking-wide text-muted-foreground">
        {t("panels.carbon.vehicleBreakdown", { defaultValue: "Vehicle breakdown" })}
      </div>

      {rows.length === 0 ? (
        // No category data on this response — state it, do not invent a split.
        <div className="rounded-md border border-dashed border-border bg-muted/30 px-3 py-2 text-[11px] text-muted-foreground">
          {t("panels.carbon.breakdownUnavailable", {
            defaultValue: "Category-wise breakdown not available from this data source.",
          })}
        </div>
      ) : (
        <div className="overflow-hidden rounded-md border border-border">
          <table className="w-full text-[12px] tabular-nums">
            <thead>
              <tr className="bg-muted/40 text-left text-[10px] uppercase tracking-wide text-muted-foreground">
                <th className="px-3 py-1.5 font-medium">
                  {t("panels.carbon.colCategory", { defaultValue: "Category" })}
                </th>
                <th className="px-3 py-1.5 text-right font-medium">
                  {t("panels.carbon.colVehicles", { defaultValue: "Vehicles" })}
                </th>
                <th className="px-3 py-1.5 text-right font-medium">
                  {t("panels.carbon.colShare", { defaultValue: "Share" })}
                </th>
                <th className="px-3 py-1.5 text-right font-medium">CO₂e</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(([cls, n]) => {
                const kg = rollup.by_class[cls];
                const share = breakdownTotal > 0 ? (n / breakdownTotal) * 100 : 0;
                return (
                  <tr key={cls} className="border-t border-border/50">
                    <td className="px-3 py-1.5">
                      <span className="flex items-center gap-2">
                        <span
                          aria-hidden
                          className="h-2 w-2 shrink-0 rounded-full"
                          style={{ backgroundColor: classColour(cls, classOrder) }}
                        />
                        <span className="font-medium">{cls}</span>
                      </span>
                    </td>
                    <td className="px-3 py-1.5 text-right">
                      {n.toLocaleString()}{" "}
                      <span className="text-[10px] font-normal text-muted-foreground">
                        {t("panels.carbon.vehiclesUnit", { defaultValue: "vehicles" })}
                      </span>
                    </td>
                    <td className="px-3 py-1.5 text-right text-muted-foreground">
                      {share.toFixed(1)}%
                    </td>
                    <td className="px-3 py-1.5 text-right text-muted-foreground">
                      {kg != null ? `${Math.round(kg).toLocaleString()} kg` : "—"}
                    </td>
                  </tr>
                );
              })}
              <tr className="border-t border-border bg-muted/30 font-semibold">
                <td className="px-3 py-1.5">
                  {t("panels.carbon.totalRow", { defaultValue: "Total" })}
                </td>
                <td className="px-3 py-1.5 text-right">
                  {breakdownTotal.toLocaleString()}{" "}
                  <span className="text-[10px] font-normal text-muted-foreground">
                    {t("panels.carbon.vehiclesUnit", { defaultValue: "vehicles" })}
                  </span>
                </td>
                <td className="px-3 py-1.5 text-right text-muted-foreground">100%</td>
                <td className="px-3 py-1.5 text-right">
                  {Math.round(rollup.total_kg).toLocaleString()} kg
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function fmtTs(iso?: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

// Persisted per-vehicle emission ledger (jnpa.carbon_emission via
// GET /api/carbon/history). Shows Vehicle / Distance / CO2 / Timestamp / Source —
// factual rows only, no improvement claims.
function CarbonLedger() {
  const { t } = useTranslation();
  const q = useQuery({ queryKey: ["carbon-history"], queryFn: () => getAdapter().carbonHistory() });
  const rows = q.data ?? [];
  if (q.isLoading) return null;
  if (!rows.length) return null;
  return (
    <div>
      <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
        {t("panels.carbon.ledger", { defaultValue: "Recent emissions" })}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-[11px] tabular-nums">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="py-0.5 pr-2 font-medium">
                {t("panels.carbon.colVehicle", { defaultValue: "Vehicle" })}
              </th>
              <th className="py-0.5 pr-2 text-right font-medium">
                {t("panels.carbon.colDistance", { defaultValue: "Distance" })}
              </th>
              <th className="py-0.5 pr-2 text-right font-medium">CO₂</th>
              <th className="py-0.5 pr-2 font-medium">
                {t("panels.carbon.colTime", { defaultValue: "Time" })}
              </th>
              <th className="py-0.5 font-medium">
                {t("panels.carbon.colSource", { defaultValue: "Source" })}
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, 8).map((r) => (
              <tr
                key={r.id ?? `${r.vehicle_id}-${r.created_at}`}
                className="border-t border-border/50"
              >
                <td className="py-0.5 pr-2 font-mono">{r.vehicle_id}</td>
                <td className="py-0.5 pr-2 text-right">
                  {r.distance_km != null ? `${r.distance_km.toFixed(1)} km` : "—"}
                </td>
                <td className="py-0.5 pr-2 text-right">
                  {r.co2_kg != null ? `${r.co2_kg.toFixed(1)} kg` : "—"}
                </td>
                <td className="py-0.5 pr-2 text-muted-foreground">{fmtTs(r.created_at)}</td>
                <td className="py-0.5">
                  <span className="rounded bg-muted px-1.5 py-0.5 text-[10px]">
                    {r.source ?? "—"}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function CarbonTile() {
  const { t } = useTranslation();
  const q = useQuery({ queryKey: ["carbon-rollup"], queryFn: () => getAdapter().carbonRollup() });
  const c = q.data;

  return (
    <CollapsibleCard
      id="carbon"
      title={t("panels.carbon.title")}
      subtitle={t("panels.carbon.subtitle")}
      headerRight={<CarbonMethodologyDialog />}
      bodyClassName="space-y-3"
    >
      {q.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> {t("common.loading")}
        </div>
      ) : !c ? (
        <EmptyState>{t("panels.carbon.empty")}</EmptyState>
      ) : (
        <>
          {/* Headline pair: the CO₂e figure and the fleet it was computed over,
              each with an explicit label so neither number is ambiguous. */}
          <div className="grid grid-cols-2 gap-3">
            <div className="rounded-md border border-border bg-muted/20 px-3 py-2">
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                {t("panels.carbon.total")}
              </div>
              <div className="text-2xl font-semibold tabular-nums">
                {(c.total_kg / 1000).toFixed(2)}
                <span className="ml-1 text-xs font-normal text-muted-foreground">t CO₂e</span>
              </div>
              <div className="text-[10px] text-muted-foreground tabular-nums">
                {c.total_kg.toLocaleString()} kg
              </div>
            </div>
            <div className="rounded-md border border-border bg-muted/20 px-3 py-2">
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                {t("panels.carbon.totalVehicles", { defaultValue: "Total vehicles" })}
              </div>
              <div className="text-2xl font-semibold tabular-nums">
                {c.vehicle_count.toLocaleString()}
                <span className="ml-1 text-xs font-normal text-muted-foreground">
                  {t("panels.carbon.vehiclesUnit", { defaultValue: "vehicles" })}
                </span>
              </div>
              <div className="text-[10px] text-muted-foreground">
                {t("panels.carbon.contributesTo", {
                  defaultValue: "Vehicles included in the CO₂e calculation above",
                })}
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

          {/* which vehicles the figure covers, category by category */}
          <VehicleBreakdown rollup={c} />

          {/* by class */}
          <div>
            <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
              {t("panels.carbon.byClass")}
            </div>
            <div className="space-y-1">
              {Object.entries(c.by_class).map(([cls, kg]) => {
                const max = Math.max(...Object.values(c.by_class), 1);
                const colour = classColour(cls, Object.keys(c.by_class));
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

          {/* Persisted per-vehicle emission ledger (R6 durable store). */}
          <CarbonLedger />
        </>
      )}
    </CollapsibleCard>
  );
}

export default CarbonTile;
