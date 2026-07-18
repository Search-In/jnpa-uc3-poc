// ECY TRT (Turn-Round Time) — Feature 8. Tracks the empty-container-yard
// vehicle lifecycle Gate-In → Parking → Loading → Gate-Out and rolls the
// elapsed phase minutes up into a Turn-Round Time (TRT) KPI. All figures are
// RDS-backed via /api/trt/* (summary + records); the "Advance a vehicle" demo
// control POSTs /api/trt/phase to step a vehicle through the lifecycle so a
// TRT record can be watched completing live. Built on the DTCCC kit to match
// ParkingManagement conventions (StatCard grid, DataTable, StatusChip, tokens).

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Timer,
  LogIn,
  SquareParking,
  PackageCheck,
  LogOut,
  CheckCircle2,
  Hourglass,
  ChevronRight,
  Truck,
} from "lucide-react";
import { api } from "@/lib/api";
import { Card } from "@/components/ui/card";
import {
  PageContainer,
  PageHeader,
  StatGrid,
  StatCard,
  DataTable,
  StatusChip,
  type Column,
  type Tone,
} from "@/components/ui/dtccc";
import { STATUS } from "@/lib/tokens";
import { fmtDateTimeIST } from "@/lib/utils";

// The four lifecycle phases, in order. Each button POSTs /api/trt/phase to
// advance a vehicle to that stage.
const PHASES = [
  { key: "GATE_IN", label: "Gate In", icon: LogIn },
  { key: "PARKING", label: "Parking", icon: SquareParking },
  { key: "LOADING", label: "Loading", icon: PackageCheck },
  { key: "GATE_OUT", label: "Gate Out", icon: LogOut },
] as const;

// Rounds a possibly-null minute value for display.
function mins(v: any): string {
  return v == null || Number.isNaN(Number(v)) ? "—" : `${Math.round(Number(v))}m`;
}

function statusTone(status?: string | null): Tone {
  const s = String(status ?? "").toUpperCase();
  if (s === "COMPLETED" || s === "GATE_OUT") return "ok";
  if (s === "OPEN" || s === "GATE_IN") return "info";
  return "warn";
}

export default function EcyTrt() {
  const qc = useQueryClient();
  const [vehicleId, setVehicleId] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ tone: Tone; text: string } | null>(null);

  const summaryQ = useQuery({
    queryKey: ["trt-summary"],
    queryFn: () => api.trtSummary(),
    refetchInterval: 8000,
  });
  const recordsQ = useQuery({
    queryKey: ["trt-records"],
    queryFn: () => api.trtRecords({ limit: 200 }),
  });

  const summary: any = summaryQ.data ?? {};
  const phases: any = summary.phases ?? {};
  const live = summary.source === "live";
  const records: any[] = recordsQ.data?.records ?? [];

  function refreshAll() {
    void qc.invalidateQueries({ queryKey: ["trt-summary"] });
    void qc.invalidateQueries({ queryKey: ["trt-records"] });
  }

  async function advance(phase: string) {
    const vid = vehicleId.trim();
    if (!vid) {
      setMsg({ tone: "warn", text: "Enter a vehicle ID first." });
      return;
    }
    setBusy(phase);
    setMsg(null);
    try {
      await api.trtPhase({ vehicle_id: vid, phase });
      setMsg({ tone: "ok", text: `${vid} advanced to ${phase.replace("_", " ")}.` });
      refreshAll();
    } catch (e: any) {
      setMsg({ tone: "critical", text: e?.message ? String(e.message) : "Failed to advance phase." });
    } finally {
      setBusy(null);
    }
  }

  const columns: Column<any>[] = [
    {
      key: "vehicle",
      header: "Vehicle",
      className: "font-mono",
      render: (r) => r.plate || r.vehicle_id || "—",
    },
    { key: "trip", header: "Trip", className: "font-mono text-muted-foreground", render: (r) => r.trip_id ?? "—" },
    {
      key: "gate_in",
      header: "Gate In",
      className: "text-muted-foreground",
      render: (r) => (r.gate_in_at ? fmtDateTimeIST(r.gate_in_at) : "—"),
    },
    { key: "g2p", header: "Gate→Park", align: "right", className: "tabular-nums", render: (r) => mins(r.gate_to_park_min) },
    { key: "p2l", header: "Park→Load", align: "right", className: "tabular-nums", render: (r) => mins(r.park_to_load_min) },
    { key: "l2o", header: "Load→Out", align: "right", className: "tabular-nums", render: (r) => mins(r.load_to_out_min) },
    {
      key: "trt",
      header: "TRT",
      align: "right",
      className: "tabular-nums font-semibold",
      render: (r) => mins(r.trt_min),
    },
    {
      key: "status",
      header: "Status",
      render: (r) => <StatusChip label={r.status ?? "—"} tone={statusTone(r.status)} />,
    },
  ];

  return (
    <PageContainer>
      <PageHeader
        icon={Timer}
        title="ECY Turn-Round Time"
        subtitle="Empty-container-yard vehicle lifecycle · Gate-In → Parking → Loading → Gate-Out · RDS-backed"
        updatedAt={summaryQ.dataUpdatedAt}
        isFetching={summaryQ.isFetching && !summaryQ.isLoading}
        onRefresh={refreshAll}
      />

      {/* KPI hero: big avg TRT + provenance chip, then the phase stepper. */}
      <div className="px-4 pt-3">
        <Card className="p-4">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                Average Turn-Round Time
                <span
                  className="rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide"
                  style={{
                    color: live ? STATUS.ok : STATUS.warning,
                    backgroundColor: `${live ? STATUS.ok : STATUS.warning}1a`,
                  }}
                  title={live ? "Aggregated from live TRT records" : "No completed records yet — showing baseline"}
                >
                  {live ? "Live" : "Baseline"}
                </span>
              </div>
              <div className="mt-1 flex items-baseline gap-1.5">
                <span className="text-4xl font-bold tabular-nums text-foreground">
                  {summaryQ.isLoading ? "…" : summary.avg_trt_min != null ? Math.round(Number(summary.avg_trt_min)) : "—"}
                </span>
                <span className="text-sm text-muted-foreground">minutes</span>
              </div>
            </div>
            <div className="flex gap-2">
              <MiniStat label="Completed" value={summary.completed ?? "—"} tone="ok" icon={CheckCircle2} />
              <MiniStat label="Open" value={summary.open ?? "—"} tone="warn" icon={Hourglass} />
            </div>
          </div>

          {/* Horizontal stepper with phase averages under each arrow. */}
          <div className="mt-4 flex flex-wrap items-center gap-1 overflow-x-auto">
            <StepNode label="Gate In" icon={LogIn} />
            <StepArrow label="Gate→Park" value={mins(phases.gate_to_park_min)} />
            <StepNode label="Parking" icon={SquareParking} />
            <StepArrow label="Park→Load" value={mins(phases.park_to_load_min)} />
            <StepNode label="Loading" icon={PackageCheck} />
            <StepArrow label="Load→Out" value={mins(phases.load_to_out_min)} />
            <StepNode label="Gate Out" icon={LogOut} />
            <StepArrow label="TRT" value={mins(summary.avg_trt_min)} accent />
            <StepNode label="TRT" icon={Timer} accent />
          </div>
        </Card>
      </div>

      {/* Phase-average summary cards. */}
      <div className="px-4 pt-3">
        <StatGrid className="lg:grid-cols-3">
          <StatCard
            icon={LogIn}
            label="Gate → Parking (avg)"
            value={mins(phases.gate_to_park_min)}
            tone="info"
            loading={summaryQ.isLoading}
          />
          <StatCard
            icon={SquareParking}
            label="Parking → Loading (avg)"
            value={mins(phases.park_to_load_min)}
            tone="warn"
            loading={summaryQ.isLoading}
          />
          <StatCard
            icon={PackageCheck}
            label="Loading → Gate-Out (avg)"
            value={mins(phases.load_to_out_min)}
            tone="ok"
            loading={summaryQ.isLoading}
          />
        </StatGrid>
      </div>

      {/* Demo control: advance a vehicle through the lifecycle. */}
      <div className="px-4 pt-3">
        <Card className="p-4">
          <div className="mb-2 flex items-center gap-2">
            <Truck className="h-4 w-4 text-muted-foreground" />
            <h2 className="text-sm font-semibold text-foreground">Advance a vehicle</h2>
          </div>
          <p className="mb-3 text-[11px] text-muted-foreground">
            Step a vehicle through the ECY lifecycle to watch its TRT record build and complete.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={vehicleId}
              onChange={(e) => setVehicleId(e.target.value)}
              placeholder="Vehicle ID / plate…"
              className="h-9 w-56 rounded-md border border-border bg-background px-3 text-[13px] font-medium text-foreground outline-none transition-colors focus:ring-2 focus:ring-primary/20"
            />
            {PHASES.map((p) => {
              const Icon = p.icon;
              return (
                <button
                  key={p.key}
                  type="button"
                  disabled={busy !== null}
                  onClick={() => advance(p.key)}
                  className="inline-flex h-9 items-center gap-1.5 rounded-md border border-border bg-background px-3 text-[13px] font-medium text-foreground transition-colors hover:bg-muted disabled:opacity-50"
                >
                  <Icon className="h-3.5 w-3.5" />
                  {busy === p.key ? "…" : p.label}
                </button>
              );
            })}
          </div>
          {msg && (
            <div className="mt-3">
              <StatusChip label={msg.text} tone={msg.tone} />
            </div>
          )}
        </Card>
      </div>

      {/* Records table — newest first. */}
      <div className="px-4 py-3">
        <Card className="overflow-hidden">
          <DataTable
            columns={columns}
            rows={records}
            rowKey={(r) => String(r.trip_id ?? `${r.vehicle_id}-${r.gate_in_at}`)}
            status={recordsQ}
            onRetry={() => recordsQ.refetch()}
            emptyLabel="No TRT records in RDS yet — advance a vehicle above."
            search={(r, q) =>
              `${r.plate ?? ""} ${r.vehicle_id ?? ""} ${r.trip_id ?? ""} ${r.status ?? ""}`
                .toLowerCase()
                .includes(q)
            }
            searchPlaceholder="Search vehicle / plate / trip…"
            pageSize={12}
          />
        </Card>
      </div>
    </PageContainer>
  );
}

function MiniStat({
  label,
  value,
  tone,
  icon: Icon,
}: {
  label: string;
  value: any;
  tone: Tone;
  icon: typeof CheckCircle2;
}) {
  const colour = tone === "ok" ? STATUS.ok : tone === "warn" ? STATUS.warning : STATUS.critical;
  return (
    <div className="flex items-center gap-2 rounded-lg border border-border px-3 py-2">
      <span
        className="flex h-8 w-8 items-center justify-center rounded-md"
        style={{ backgroundColor: `${colour}1a`, color: colour }}
      >
        <Icon className="h-4 w-4" />
      </span>
      <div>
        <div className="text-lg font-bold tabular-nums leading-none text-foreground">{value}</div>
        <div className="text-[10px] text-muted-foreground">{label}</div>
      </div>
    </div>
  );
}

function StepNode({
  label,
  icon: Icon,
  accent,
}: {
  label: string;
  icon: typeof LogIn;
  accent?: boolean;
}) {
  const colour = accent ? STATUS.ok : STATUS.info ?? "#2563eb";
  return (
    <div className="flex shrink-0 flex-col items-center gap-1">
      <span
        className="flex h-10 w-10 items-center justify-center rounded-full border"
        style={{ backgroundColor: `${colour}1a`, color: colour, borderColor: `${colour}40` }}
      >
        <Icon className="h-5 w-5" />
      </span>
      <span className="text-[10px] font-medium text-foreground">{label}</span>
    </div>
  );
}

function StepArrow({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className="flex shrink-0 flex-col items-center px-1">
      <div className="flex items-center gap-0.5 text-muted-foreground">
        <ChevronRight className="h-4 w-4" />
      </div>
      <span
        className="mt-0.5 text-[11px] font-semibold tabular-nums"
        style={{ color: accent ? STATUS.ok : undefined }}
      >
        {value}
      </span>
      <span className="text-[9px] text-muted-foreground">{label}</span>
    </div>
  );
}
