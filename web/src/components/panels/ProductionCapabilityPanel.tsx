import { CollapsibleCard } from "@/components/ui/CollapsibleCard";
import { STATUS } from "@/lib/tokens";
import { DATA_MODE } from "@/data";

// Production Capability panel (UC-3 audit P2 Task 8). Shows the Simulation →
// Pilot → Production maturity ladder across the four migration dimensions, so an
// evaluator sees the honest path from the PoC to a live deployment. Static
// reference content (no live data); the current PoC stage is highlighted.

type Stage = "Simulation" | "Pilot" | "Production";
const STAGES: Stage[] = ["Simulation", "Pilot", "Production"];
const CURRENT: Stage = "Simulation"; // the PoC runs at the Simulation stage

interface DimensionRow {
  dimension: string;
  simulation: string;
  pilot: string;
  production: string;
}

const ROWS: DimensionRow[] = [
  {
    dimension: "Data migration",
    simulation: "Deterministic seed corpus (Postgres/Timescale); MinIO evidence.",
    pilot: "Backfill from JNPA TOS / ICEGATE exports into the same schema.",
    production: "Live CDC from TOS/ICEGATE; RDS + object store at scale.",
  },
  {
    dimension: "Live API integration",
    simulation: "Schema-faithful simulators (Vahan/Sarathi/FastTag/ULIP/ICEGATE).",
    pilot: "One live connector at a time behind the fallback orchestrator.",
    production: "All JNPA-facilitated APIs live; sim demoted to outage fallback.",
  },
  {
    dimension: "ML retraining",
    simulation: "Offline train on synthetic + fixtures; committed artifacts.",
    pilot: "Fine-tune on captured corridor data; shadow-eval vs baseline.",
    production: "Scheduled retrain pipeline; drift monitoring; A/B rollout.",
  },
  {
    dimension: "Scaling approach",
    simulation: "Single-host docker-compose; 20k-device truck simulator.",
    pilot: "Managed containers; 30k+ devices; read-replicas.",
    production: "Autoscaled services; partitioned Kafka; HA Postgres/Redis.",
  },
];

function stageColor(s: Stage): string {
  return s === "Simulation" ? STATUS.warning : s === "Pilot" ? STATUS.info : STATUS.ok;
}

export function ProductionCapabilityPanel() {
  return (
    <CollapsibleCard
      id="production-capability"
      title="Production Capability"
      subtitle="Simulation → Pilot → Production — the path from PoC to live"
      bodyClassName="space-y-3"
    >
      {/* Stage ladder */}
      <div className="flex items-center gap-2">
        {STAGES.map((s, i) => (
          <div key={s} className="flex items-center gap-2">
            <span
              className="rounded-full px-2.5 py-1 text-[11px] font-semibold"
              style={{
                color: stageColor(s),
                backgroundColor: stageColor(s) + (s === CURRENT ? "33" : "18"),
                outline: s === CURRENT ? `1px solid ${stageColor(s)}` : "none",
              }}
            >
              {s}
              {s === CURRENT ? " · you are here" : ""}
            </span>
            {i < STAGES.length - 1 && <span className="text-muted-foreground">→</span>}
          </div>
        ))}
        <span className="ml-auto text-[11px] text-muted-foreground">
          data mode: <strong className="text-foreground">{DATA_MODE}</strong>
        </span>
      </div>

      {/* Dimension matrix */}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[640px] border-collapse text-[12px]">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="py-1 pr-3 font-medium">Dimension</th>
              {STAGES.map((s) => (
                <th key={s} className="py-1 pr-3 font-medium" style={{ color: stageColor(s) }}>
                  {s}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ROWS.map((r) => (
              <tr key={r.dimension} className="border-t border-border align-top">
                <td className="py-1.5 pr-3 font-medium">{r.dimension}</td>
                <td className="py-1.5 pr-3 text-muted-foreground">{r.simulation}</td>
                <td className="py-1.5 pr-3 text-muted-foreground">{r.pilot}</td>
                <td className="py-1.5 pr-3 text-muted-foreground">{r.production}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </CollapsibleCard>
  );
}

export default ProductionCapabilityPanel;
