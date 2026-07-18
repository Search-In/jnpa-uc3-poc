// Camera AI console (UC-III Features 3/4/5) — edge-camera vehicle/queue counting,
// trailer-number reads, and container-number reads with ISO-6346 validation.
// Every panel is backed by /api/camera-ai/* (RDS-persisted edge inferences) — no
// synthetic runtime data. Built on the DTCCC kit (KPI strip, tabbed tables) and
// mirrors the polling cadence of the other live screens.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Camera,
  Truck,
  Container,
  Gauge,
  Users,
  CheckCircle2,
  XCircle,
  ScanLine,
} from "lucide-react";
import { api } from "@/lib/api";
import { Card } from "@/components/ui/card";
import {
  PageContainer,
  PageHeader,
  StatGrid,
  StatCard,
  SegmentedTabs,
  DataTable,
  StatusChip,
  type Column,
  type Tone,
} from "@/components/ui/dtccc";
import { STATUS } from "@/lib/tokens";
import { fmtDateTimeIST } from "@/lib/utils";

type TabKey = "counting" | "trailers" | "containers";

// Live screens refresh on a ~10s cadence.
const POLL_MS = 10_000;

function congestionTone(level?: string | null): Tone {
  const l = String(level ?? "").toUpperCase();
  if (l === "HIGH") return "critical";
  if (l === "MEDIUM") return "warn";
  if (l === "LOW") return "ok";
  return "neutral";
}

function pct(conf?: number | null): string {
  if (conf == null || Number.isNaN(Number(conf))) return "—";
  const n = Number(conf);
  // Accept either 0..1 or 0..100 confidences.
  const v = n <= 1 ? n * 100 : n;
  return `${v.toFixed(0)}%`;
}

export default function CameraAI() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<TabKey>("counting");

  const dashQ = useQuery({
    queryKey: ["camera-dashboard"],
    queryFn: () => api.cameraDashboard(),
    refetchInterval: POLL_MS,
  });
  const countsQ = useQuery({
    queryKey: ["camera-counts"],
    queryFn: () => api.cameraCounts({ limit: 200 }),
    refetchInterval: POLL_MS,
  });
  const trailersQ = useQuery({
    queryKey: ["camera-trailers"],
    queryFn: () => api.cameraTrailers(200),
    refetchInterval: POLL_MS,
  });
  const containersQ = useQuery({
    queryKey: ["camera-containers"],
    queryFn: () => api.cameraContainers(200),
    refetchInterval: POLL_MS,
  });

  const dash: any = dashQ.data ?? {};
  const counts: any[] = countsQ.data?.counts ?? [];
  const trailers: any[] = trailersQ.data?.trailers ?? [];
  const containers: any[] = containersQ.data?.containers ?? [];

  const containerReads = dash.container_reads ?? {};
  const congestionByGate: Record<string, string> = dash.congestion_by_gate ?? {};
  const avgConf = dash.avg_confidence ?? {};

  const updatedAt = Math.max(
    dashQ.dataUpdatedAt || 0,
    countsQ.dataUpdatedAt || 0,
    trailersQ.dataUpdatedAt || 0,
    containersQ.dataUpdatedAt || 0,
  );
  const anyFetching =
    dashQ.isFetching || countsQ.isFetching || trailersQ.isFetching || containersQ.isFetching;

  function refreshAll() {
    void qc.invalidateQueries({ queryKey: ["camera-dashboard"] });
    void qc.invalidateQueries({ queryKey: ["camera-counts"] });
    void qc.invalidateQueries({ queryKey: ["camera-trailers"] });
    void qc.invalidateQueries({ queryKey: ["camera-containers"] });
  }

  return (
    <PageContainer>
      <PageHeader
        icon={Camera}
        title="Camera AI"
        subtitle="Edge vehicle/queue counting · Trailer-ID · Container-ID (ISO-6346) · RDS-backed"
        updatedAt={updatedAt}
        isFetching={anyFetching}
        onRefresh={refreshAll}
      />

      {/* KPI strip */}
      <div className="px-4 pt-3">
        <StatGrid>
          <StatCard
            icon={Truck}
            label="Trailer Reads"
            value={dash.trailer_reads ?? 0}
            tone="info"
            loading={dashQ.isLoading}
          />
          <StatCard
            icon={CheckCircle2}
            label="Containers Valid"
            value={containerReads.valid ?? 0}
            tone="ok"
            loading={dashQ.isLoading}
          />
          <StatCard
            icon={XCircle}
            label="Containers Invalid"
            value={containerReads.invalid ?? 0}
            tone={(containerReads.invalid ?? 0) > 0 ? "critical" : "ok"}
            loading={dashQ.isLoading}
          />
          <StatCard
            icon={Gauge}
            label="Avg Confidence"
            value={pct(avgConf.counts)}
            tone="neutral"
            loading={dashQ.isLoading}
          />
        </StatGrid>
      </div>

      {/* Congestion-by-gate chips */}
      <div className="px-4 pt-3">
        <Card className="p-3">
          <div className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            <Users className="h-3.5 w-3.5" aria-hidden />
            Congestion by gate
          </div>
          {Object.keys(congestionByGate).length === 0 ? (
            <span className="text-xs text-muted-foreground">No gate readings yet.</span>
          ) : (
            <div className="flex flex-wrap gap-2">
              {Object.entries(congestionByGate).map(([gate, level]) => (
                <div
                  key={gate}
                  className="flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1.5"
                >
                  <span className="font-mono text-xs text-foreground">{gate}</span>
                  <StatusChip label={String(level ?? "—")} tone={congestionTone(level)} />
                </div>
              ))}
            </div>
          )}
          <div className="mt-2 flex flex-wrap gap-3 text-[11px] text-muted-foreground">
            <span>
              Trailer conf: <span className="font-mono">{pct(avgConf.trailer)}</span>
            </span>
            <span>
              Container conf: <span className="font-mono">{pct(avgConf.container)}</span>
            </span>
          </div>
        </Card>
      </div>

      {/* Container validator (ISO-6346 demonstrator) */}
      <div className="px-4 pt-3">
        <ContainerValidator
          onIngested={() => {
            void qc.invalidateQueries({ queryKey: ["camera-containers"] });
            void qc.invalidateQueries({ queryKey: ["camera-dashboard"] });
          }}
        />
      </div>

      {/* Tabs + tables */}
      <div className="px-4 py-3">
        <SegmentedTabs
          value={tab}
          onChange={setTab}
          className="mb-3"
          tabs={[
            { key: "counting", label: "Counting", icon: Users, count: counts.length },
            { key: "trailers", label: "Trailers", icon: Truck, count: trailers.length },
            { key: "containers", label: "Containers", icon: Container, count: containers.length },
          ]}
        />

        {tab === "counting" && (
          <Card className="overflow-hidden">
            <CountingTable rows={counts} status={countsQ} onRetry={() => countsQ.refetch()} />
          </Card>
        )}
        {tab === "trailers" && (
          <Card className="overflow-hidden">
            <TrailersTable rows={trailers} status={trailersQ} onRetry={() => trailersQ.refetch()} />
          </Card>
        )}
        {tab === "containers" && (
          <Card className="overflow-hidden">
            <ContainersTable
              rows={containers}
              status={containersQ}
              onRetry={() => containersQ.refetch()}
            />
          </Card>
        )}
      </div>
    </PageContainer>
  );
}

function ContainerValidator({ onIngested }: { onIngested: () => void }) {
  const [cameraId, setCameraId] = useState("");
  const [containerNumber, setContainerNumber] = useState("");
  const [plate, setPlate] = useState("");

  const ingest = useMutation({
    mutationFn: () =>
      api.cameraContainerIngest({
        camera_id: cameraId || "manual",
        container_number: containerNumber.trim().toUpperCase(),
        plate: plate.trim().toUpperCase() || undefined,
      }),
    onSuccess: onIngested,
  });

  const res: any = ingest.data;
  const canSubmit = containerNumber.trim().length >= 4 && !ingest.isPending;

  return (
    <Card className="p-4">
      <div className="mb-3 flex items-center gap-2">
        <ScanLine size={15} />
        <h3 className="text-sm font-semibold">Validate container number</h3>
        <span className="text-[11px] text-muted-foreground">ISO-6346 check-digit</span>
      </div>
      <div className="flex flex-wrap items-end gap-2 text-sm">
        <label className="flex flex-col gap-0.5">
          <span className="text-[10px] text-muted-foreground">Container no.</span>
          <input
            value={containerNumber}
            onChange={(e) => setContainerNumber(e.target.value)}
            placeholder="MSCU1234565"
            className="w-40 rounded-md border border-border bg-card px-2 py-1.5 font-mono uppercase outline-none"
          />
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="text-[10px] text-muted-foreground">Camera ID</span>
          <input
            value={cameraId}
            onChange={(e) => setCameraId(e.target.value)}
            placeholder="CAM-G1"
            className="w-28 rounded-md border border-border bg-card px-2 py-1.5 outline-none"
          />
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="text-[10px] text-muted-foreground">Plate (opt.)</span>
          <input
            value={plate}
            onChange={(e) => setPlate(e.target.value)}
            placeholder="MH04AB1234"
            className="w-32 rounded-md border border-border bg-card px-2 py-1.5 font-mono uppercase outline-none"
          />
        </label>
        <button
          disabled={!canSubmit}
          onClick={() => ingest.mutate()}
          className="rounded-md bg-primary px-3 py-1.5 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        >
          {ingest.isPending ? "Validating…" : "Validate"}
        </button>
      </div>

      {ingest.isError && (
        <div className="mt-2 text-[11px]" style={{ color: STATUS.critical }}>
          {(ingest.error as Error)?.message ?? "Validation failed."}
        </div>
      )}
      {res && (
        <div className="mt-3 flex flex-wrap items-center gap-2 text-[12px]">
          <span className="font-mono">{res.row?.container_number ?? containerNumber}</span>
          <StatusChip
            label={res.valid ? "VALID" : "INVALID"}
            tone={res.valid ? "ok" : "critical"}
          />
          <StatusChip
            label={res.check_digit_ok ? "check-digit ✓" : "check-digit ✗"}
            tone={res.check_digit_ok ? "ok" : "critical"}
          />
          {res.row?.iso_type && (
            <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px]">
              {res.row.iso_type}
            </span>
          )}
        </div>
      )}
    </Card>
  );
}

function CountingTable({
  rows,
  status,
  onRetry,
}: {
  rows: any[];
  status: any;
  onRetry: () => void;
}) {
  const columns: Column<any>[] = useMemo(
    () => [
      {
        key: "ts",
        header: "Time",
        className: "whitespace-nowrap text-muted-foreground",
        render: (r) => (r.ts ? fmtDateTimeIST(r.ts) : "—"),
      },
      { key: "camera", header: "Camera", className: "font-mono", render: (r) => r.camera_id ?? "—" },
      { key: "gate", header: "Gate", className: "font-mono", render: (r) => r.gate_id ?? "—" },
      {
        key: "vehicles",
        header: "Vehicles",
        className: "tabular-nums",
        render: (r) => r.vehicle_count ?? 0,
      },
      {
        key: "queue",
        header: "Queue",
        className: "tabular-nums",
        render: (r) => r.queue_count ?? 0,
      },
      {
        key: "congestion",
        header: "Congestion",
        render: (r) => (
          <StatusChip
            label={String(r.congestion_level ?? "—")}
            tone={congestionTone(r.congestion_level)}
          />
        ),
      },
      { key: "conf", header: "Conf.", className: "tabular-nums", render: (r) => pct(r.confidence) },
    ],
    [],
  );
  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(r) => `${r.camera_id ?? ""}-${r.ts ?? Math.random()}`}
      status={status}
      onRetry={onRetry}
      emptyLabel="No counting reads in RDS yet."
      search={(r, q) =>
        `${r.camera_id ?? ""} ${r.gate_id ?? ""} ${r.congestion_level ?? ""}`
          .toLowerCase()
          .includes(q)
      }
      searchPlaceholder="Search camera / gate…"
      pageSize={10}
    />
  );
}

function TrailersTable({
  rows,
  status,
  onRetry,
}: {
  rows: any[];
  status: any;
  onRetry: () => void;
}) {
  const columns: Column<any>[] = useMemo(
    () => [
      {
        key: "ts",
        header: "Time",
        className: "whitespace-nowrap text-muted-foreground",
        render: (r) => (r.ts ? fmtDateTimeIST(r.ts) : "—"),
      },
      { key: "camera", header: "Camera", className: "font-mono", render: (r) => r.camera_id ?? "—" },
      {
        key: "trailer",
        header: "Trailer No.",
        className: "font-mono",
        render: (r) => r.trailer_number ?? "—",
      },
      { key: "plate", header: "Plate", className: "font-mono", render: (r) => r.plate ?? "—" },
      { key: "conf", header: "Conf.", className: "tabular-nums", render: (r) => pct(r.confidence) },
    ],
    [],
  );
  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(r) => `${r.trailer_number ?? ""}-${r.ts ?? Math.random()}`}
      status={status}
      onRetry={onRetry}
      emptyLabel="No trailer reads in RDS yet."
      search={(r, q) =>
        `${r.trailer_number ?? ""} ${r.plate ?? ""} ${r.camera_id ?? ""}`.toLowerCase().includes(q)
      }
      searchPlaceholder="Search trailer / plate…"
      pageSize={10}
    />
  );
}

function ContainersTable({
  rows,
  status,
  onRetry,
}: {
  rows: any[];
  status: any;
  onRetry: () => void;
}) {
  const columns: Column<any>[] = useMemo(
    () => [
      {
        key: "ts",
        header: "Time",
        className: "whitespace-nowrap text-muted-foreground",
        render: (r) => (r.ts ? fmtDateTimeIST(r.ts) : "—"),
      },
      {
        key: "container",
        header: "Container No.",
        className: "font-mono",
        render: (r) => r.container_number ?? "—",
      },
      { key: "iso", header: "ISO Type", className: "font-mono", render: (r) => r.iso_type ?? "—" },
      {
        key: "checkdigit",
        header: "Check-digit",
        render: (r) => (
          <StatusChip
            label={r.check_digit_ok ? "✓" : "✗"}
            tone={r.check_digit_ok ? "ok" : "critical"}
          />
        ),
      },
      {
        key: "valid",
        header: "Validity",
        render: (r) => (
          <StatusChip label={r.valid ? "VALID" : "INVALID"} tone={r.valid ? "ok" : "critical"} />
        ),
      },
      { key: "plate", header: "Plate", className: "font-mono", render: (r) => r.plate ?? "—" },
      { key: "conf", header: "Conf.", className: "tabular-nums", render: (r) => pct(r.confidence) },
    ],
    [],
  );
  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(r) => `${r.container_number ?? ""}-${r.ts ?? Math.random()}`}
      status={status}
      onRetry={onRetry}
      emptyLabel="No container reads in RDS yet."
      search={(r, q) =>
        `${r.container_number ?? ""} ${r.iso_type ?? ""} ${r.plate ?? ""}`.toLowerCase().includes(q)
      }
      searchPlaceholder="Search container / plate…"
      pageSize={10}
    />
  );
}
