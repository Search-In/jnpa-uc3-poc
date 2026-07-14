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

// Emission factors mirror the backend (carbon/factors.py). The "effective" per-km
// factor = nominal laden payload (t) × tonne-km factor (g CO₂e/t·km) ÷ 1000, i.e.
// the kg CO₂e a laden truck of that class emits per km moved. Kept in sync so the
// methodology the operator reads matches what the service actually computes.
const EMISSION_FACTORS: {
  cls: string;
  payloadT: number;
  gPerTonneKm: number;
  idleGPerMin: number;
}[] = [
  { cls: "HGV", payloadT: 20, gPerTonneKm: 62, idleGPerMin: 134 },
  { cls: "REEFER", payloadT: 18, gPerTonneKm: 78, idleGPerMin: 224 },
  { cls: "RIGID", payloadT: 10, gPerTonneKm: 85, idleGPerMin: 134 },
  { cls: "LGV", payloadT: 1, gPerTonneKm: 110, idleGPerMin: 60 },
];

function effectiveKgPerKm(payloadT: number, gPerTonneKm: number): number {
  return (payloadT * gPerTonneKm) / 1000;
}

// "How CO₂ is calculated" — a plain-language methodology dialog so a new user can
// see exactly how the numbers are derived (no black box). Opened from the ⓘ button
// in the Carbon Footprint card header.
function CarbonMethodologyDialog() {
  const { t } = useTranslation();
  const hgv = EMISSION_FACTORS[0];
  const hgvKgPerKm = effectiveKgPerKm(hgv.payloadT, hgv.gPerTonneKm); // 1.24
  const exampleKm = 25;
  const exampleTotal = hgvKgPerKm * exampleKm; // 31.0 kg (moving only)

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

          <div>
            <div className="mb-1 text-[11px] uppercase tracking-wide text-muted-foreground">
              Emission factor by vehicle class
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-[12px] tabular-nums">
                <thead>
                  <tr className="text-left text-muted-foreground">
                    <th className="py-1 pr-3 font-medium">Class</th>
                    <th className="py-1 pr-3 text-right font-medium">Laden payload</th>
                    <th className="py-1 pr-3 text-right font-medium">Factor</th>
                    <th className="py-1 text-right font-medium">Idle</th>
                  </tr>
                </thead>
                <tbody>
                  {EMISSION_FACTORS.map((f) => (
                    <tr key={f.cls} className="border-t border-border/50">
                      <td className="py-1 pr-3 font-mono">{f.cls}</td>
                      <td className="py-1 pr-3 text-right">{f.payloadT} t</td>
                      <td className="py-1 pr-3 text-right">
                        {effectiveKgPerKm(f.payloadT, f.gPerTonneKm).toFixed(2)} kg/km
                      </td>
                      <td className="py-1 text-right text-muted-foreground">
                        {f.idleGPerMin} g/min
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="mt-1 text-[10px] text-muted-foreground">
              Factor = laden payload (t) × tonne-km factor (g CO₂e/t·km) ÷ 1000.
            </p>
          </div>

          <div>
            <div className="mb-1 text-[11px] uppercase tracking-wide text-muted-foreground">
              Worked example
            </div>
            <div className="rounded-md border border-border p-3">
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[13px]">
                <dt className="text-muted-foreground">Vehicle</dt>
                <dd className="font-mono">TRK-000001</dd>
                <dt className="text-muted-foreground">Vehicle class</dt>
                <dd>HGV</dd>
                <dt className="text-muted-foreground">Distance</dt>
                <dd>{exampleKm} km</dd>
                <dt className="text-muted-foreground">Emission factor</dt>
                <dd>{hgvKgPerKm.toFixed(2)} kg CO₂/km</dd>
                <dt className="font-medium">Total (moving)</dt>
                <dd className="font-semibold">
                  {exampleKm} × {hgvKgPerKm.toFixed(2)} = {exampleTotal.toFixed(1)} kg CO₂e
                </dd>
              </dl>
              <p className="mt-2 text-[10px] text-muted-foreground">
                Idle emissions (engine running while stationary) are added on top: idle minutes ×
                the class idle factor above.
              </p>
            </div>
          </div>

          <p className="text-[10px] text-muted-foreground">
            Factors are IPCC / GHG-Protocol / GLEC constants (diesel 2680 g CO₂e per litre). Unknown
            classes default to HGV.
          </p>
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
              <div className="text-[10px] text-muted-foreground">{t("panels.carbon.vehicles")}</div>
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

          {/* Persisted per-vehicle emission ledger (R6 durable store). */}
          <CarbonLedger />
        </>
      )}
    </CollapsibleCard>
  );
}

export default CarbonTile;
