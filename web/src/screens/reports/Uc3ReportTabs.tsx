// UC-3 Reports & Enforcement — five self-contained report components that reuse
// the DTCCC DataTable (search + pagination) and add CSV export + Print via its
// `toolbar` slot. Each report is a NAMED export (no default) so the host Reports
// screen can compose them into tabs. Every report fetches with react-query v5,
// derives loosely-typed rows, and renders a PageContainer-wrapped DataTable so it
// works embedded inside a host tab. All data comes from existing api.ts methods.

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import {
  PageContainer,
  StatGrid,
  StatCard,
  StatusChip,
  DataTable,
  type Column,
  type Tone,
} from "@/components/ui/dtccc";
import { fmtDateTimeIST } from "@/lib/utils";

// Live screens refresh on a ~10s cadence — reports poll a little slower.
const POLL_MS = 15_000;

// --- shared helpers ----------------------------------------------------------

/** Build a CSV string and trigger a client-side download via a Blob object URL. */
function exportCsv(filename: string, headers: string[], rows: string[][]) {
  const escape = (v: string) => {
    const s = String(v ?? "");
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [headers, ...rows].map((r) => r.map(escape).join(","));
  const csv = lines.join("\r\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

const BTN_CLASS =
  "inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-muted";

/** Export CSV + Print buttons, dropped into the DataTable `toolbar` slot. */
function ReportToolbar({ onExport }: { onExport: () => void }) {
  return (
    <div className="ml-auto flex items-center gap-2">
      <button type="button" onClick={onExport} className={BTN_CLASS}>
        Export CSV
      </button>
      <button type="button" onClick={() => window.print()} className={BTN_CLASS}>
        Print
      </button>
    </div>
  );
}

/** Map one-or-more react-query results into the DataTable `status` shape. */
function mergeStatus(...queries: { isLoading: boolean; isError: boolean; error?: unknown }[]) {
  return {
    isLoading: queries.some((q) => q.isLoading),
    isError: queries.some((q) => q.isError),
    error: queries.find((q) => q.isError)?.error,
  };
}

function pct(conf?: number | null): string {
  if (conf == null || Number.isNaN(Number(conf))) return "—";
  const n = Number(conf);
  const v = n <= 1 ? n * 100 : n;
  return `${v.toFixed(0)}%`;
}

function includesQ(hay: string, q: string): boolean {
  return hay.toLowerCase().includes(q);
}

// --- 1. Camera AI ------------------------------------------------------------

const CAMERA_HEADERS = ["Camera", "Gate", "Vehicle Count", "Queue", "Confidence", "Detection Time"];

export function CameraAIReport() {
  const countsQ = useQuery({
    queryKey: ["report", "camera-counts"],
    queryFn: () => api.cameraCounts({ limit: 200 }),
    refetchInterval: POLL_MS,
  });
  const dashQ = useQuery({
    queryKey: ["report", "camera-dashboard"],
    queryFn: () => api.cameraDashboard(),
    refetchInterval: POLL_MS,
  });

  const rows: any[] = countsQ.data?.counts ?? [];
  const dash: any = dashQ.data ?? {};
  const containerReads = dash.container_reads ?? {};
  const avgConf = dash.avg_confidence ?? {};

  const columns: Column<any>[] = useMemo(
    () => [
      {
        key: "camera",
        header: "Camera",
        className: "font-mono",
        render: (r) => r.camera_id ?? "—",
      },
      { key: "gate", header: "Gate", className: "font-mono", render: (r) => r.gate_id ?? "—" },
      {
        key: "vehicles",
        header: "Vehicle Count",
        align: "right",
        className: "tabular-nums",
        render: (r) => r.vehicle_count ?? 0,
      },
      {
        key: "queue",
        header: "Queue",
        align: "right",
        className: "tabular-nums",
        render: (r) => r.queue_count ?? 0,
      },
      {
        key: "conf",
        header: "Confidence",
        align: "right",
        className: "tabular-nums",
        render: (r) => pct(r.confidence),
      },
      {
        key: "ts",
        header: "Detection Time",
        className: "whitespace-nowrap text-muted-foreground",
        render: (r) => (r.ts ? fmtDateTimeIST(r.ts) : "—"),
      },
    ],
    [],
  );

  return (
    <PageContainer>
      <StatGrid>
        <StatCard
          label="Container Count"
          value={containerReads.total ?? 0}
          tone="info"
          loading={dashQ.isLoading}
        />
        <StatCard
          label="Trailer Count"
          value={dash.trailer_reads ?? 0}
          tone="info"
          loading={dashQ.isLoading}
        />
        <StatCard
          label="Avg Confidence"
          value={pct(avgConf.counts)}
          tone="neutral"
          loading={dashQ.isLoading}
        />
      </StatGrid>

      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => `${r.camera_id ?? ""}-${r.ts ?? Math.random()}`}
        status={mergeStatus(countsQ)}
        onRetry={() => countsQ.refetch()}
        emptyLabel="No camera counts in RDS yet."
        search={(r, q) => includesQ(`${r.camera_id ?? ""} ${r.gate_id ?? ""}`, q)}
        searchPlaceholder="Search camera / gate…"
        toolbar={
          <ReportToolbar
            onExport={() =>
              exportCsv(
                "camera-ai-report.csv",
                CAMERA_HEADERS,
                rows.map((r) => [
                  String(r.camera_id ?? ""),
                  String(r.gate_id ?? ""),
                  String(r.vehicle_count ?? 0),
                  String(r.queue_count ?? 0),
                  pct(r.confidence),
                  r.ts ? fmtDateTimeIST(r.ts) : "",
                ]),
              )
            }
          />
        }
      />
    </PageContainer>
  );
}

// --- 2. NVR ------------------------------------------------------------------

const NVR_HEADERS = ["Device", "Camera", "Status", "Recording", "Last Seen"];

function statusTone(status?: string | null): Tone {
  const s = String(status ?? "").toUpperCase();
  if (s === "ONLINE") return "ok";
  if (s === "OFFLINE") return "critical";
  return "neutral";
}

export function NvrReport() {
  const streamsQ = useQuery({
    queryKey: ["report", "nvr-streams"],
    queryFn: () => api.nvrStreams(),
    refetchInterval: POLL_MS,
  });
  const devicesQ = useQuery({
    queryKey: ["report", "nvr-devices"],
    queryFn: () => api.nvrDevices(),
    refetchInterval: POLL_MS,
  });

  const streams: any[] = streamsQ.data?.streams ?? [];
  const devices: any[] = devicesQ.data?.devices ?? [];

  const deviceById = useMemo(() => {
    const m = new Map<string, any>();
    for (const d of devices) m.set(String(d.nvr_id ?? d.id ?? ""), d);
    return m;
  }, [devices]);

  const cameraOf = (r: any) => r.camera_id ?? (r.channel != null ? `ch${r.channel}` : "—");
  const lastSeenOf = (r: any) => {
    const d = deviceById.get(String(r.nvr_id ?? ""));
    return d?.updated_at ? fmtDateTimeIST(d.updated_at) : "—";
  };

  const columns: Column<any>[] = useMemo(
    () => [
      { key: "device", header: "Device", className: "font-mono", render: (r) => r.nvr_name ?? "—" },
      { key: "camera", header: "Camera", className: "font-mono", render: (r) => cameraOf(r) },
      {
        key: "status",
        header: "Status",
        render: (r) => <StatusChip label={String(r.status ?? "—")} tone={statusTone(r.status)} />,
      },
      {
        key: "recording",
        header: "Recording",
        render: (r) => (String(r.status ?? "").toUpperCase() === "ONLINE" ? "Yes" : "No"),
      },
      {
        key: "lastseen",
        header: "Last Seen",
        className: "whitespace-nowrap text-muted-foreground",
        render: (r) => lastSeenOf(r),
      },
    ],
    [deviceById],
  );

  return (
    <PageContainer>
      <DataTable
        columns={columns}
        rows={streams}
        rowKey={(r) => `${r.nvr_id ?? ""}-${r.camera_id ?? r.channel ?? Math.random()}`}
        status={mergeStatus(streamsQ, devicesQ)}
        onRetry={() => {
          void streamsQ.refetch();
          void devicesQ.refetch();
        }}
        emptyLabel="No NVR streams registered yet."
        search={(r, q) => includesQ(`${r.nvr_name ?? ""} ${cameraOf(r)} ${r.status ?? ""}`, q)}
        searchPlaceholder="Search device / camera…"
        toolbar={
          <ReportToolbar
            onExport={() =>
              exportCsv(
                "nvr-report.csv",
                NVR_HEADERS,
                streams.map((r) => [
                  String(r.nvr_name ?? ""),
                  cameraOf(r),
                  String(r.status ?? ""),
                  String(r.status ?? "").toUpperCase() === "ONLINE" ? "Yes" : "No",
                  lastSeenOf(r),
                ]),
              )
            }
          />
        }
      />
    </PageContainer>
  );
}

// --- 3. Bottlenecks ----------------------------------------------------------

const BOTTLENECK_HEADERS = ["Road", "Severity", "Delay", "Average Speed", "Snapshot Time"];

export function BottlenecksReport() {
  const histQ = useQuery({
    queryKey: ["report", "bottleneck-history"],
    queryFn: () => api.bottleneckHistory(200),
    refetchInterval: POLL_MS,
  });

  const rows: any[] = histQ.data?.snapshots ?? [];

  const roadOf = (r: any) => r.name ?? r.segment_id ?? "—";
  const severityOf = (r: any) => (r.jam_factor != null ? Number(r.jam_factor).toFixed(1) : "—");
  const delayOf = (r: any) => (r.avg_delay_min != null ? `${r.avg_delay_min} min` : "—");
  const speedOf = (r: any) => (r.speed_kmh != null ? `${r.speed_kmh} km/h` : "—");

  const columns: Column<any>[] = useMemo(
    () => [
      { key: "road", header: "Road", className: "font-medium", render: (r) => roadOf(r) },
      {
        key: "severity",
        header: "Severity",
        align: "right",
        className: "tabular-nums",
        render: (r) => severityOf(r),
      },
      {
        key: "delay",
        header: "Delay",
        align: "right",
        className: "tabular-nums",
        render: (r) => delayOf(r),
      },
      {
        key: "speed",
        header: "Average Speed",
        align: "right",
        className: "tabular-nums",
        render: (r) => speedOf(r),
      },
      {
        key: "ts",
        header: "Snapshot Time",
        className: "whitespace-nowrap text-muted-foreground",
        render: (r) => (r.ts ? fmtDateTimeIST(r.ts) : "—"),
      },
    ],
    [],
  );

  return (
    <PageContainer>
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => `${r.segment_id ?? r.name ?? ""}-${r.ts ?? Math.random()}`}
        status={mergeStatus(histQ)}
        onRetry={() => histQ.refetch()}
        emptyLabel="No bottleneck snapshots in RDS yet."
        search={(r, q) => includesQ(`${r.name ?? ""} ${r.segment_id ?? ""}`, q)}
        searchPlaceholder="Search road / segment…"
        toolbar={
          <ReportToolbar
            onExport={() =>
              exportCsv(
                "bottlenecks-report.csv",
                BOTTLENECK_HEADERS,
                rows.map((r) => [
                  roadOf(r),
                  severityOf(r),
                  delayOf(r),
                  speedOf(r),
                  r.ts ? fmtDateTimeIST(r.ts) : "",
                ]),
              )
            }
          />
        }
      />
    </PageContainer>
  );
}

// --- 4. Reefer ---------------------------------------------------------------

const REEFER_HEADERS = ["Slot", "Container", "Temperature", "Availability", "Allocation"];

function reeferTone(status?: string | null): Tone {
  const s = String(status ?? "").toUpperCase();
  if (s === "AVAILABLE" || s === "FREE") return "ok";
  if (s === "OCCUPIED" || s === "ALLOCATED") return "warn";
  if (s === "FAULT" || s === "ALARM") return "critical";
  return "neutral";
}

export function ReeferReport() {
  const slotsQ = useQuery({
    queryKey: ["report", "reefer-slots"],
    queryFn: () => api.reeferSlots(),
    refetchInterval: POLL_MS,
  });
  const availQ = useQuery({
    queryKey: ["report", "reefer-availability"],
    queryFn: () => api.reeferAvailability(),
    refetchInterval: POLL_MS,
  });

  const rows: any[] = slotsQ.data?.slots ?? [];
  const totals: any = availQ.data?.totals ?? {};

  const tempOf = (r: any) => (r.set_temperature != null ? `${r.set_temperature}°C` : "—");
  const allocOf = (r: any) => (r.container_number ? "Allocated" : "Free");

  const columns: Column<any>[] = useMemo(
    () => [
      { key: "slot", header: "Slot", className: "font-mono", render: (r) => r.slot_code ?? "—" },
      {
        key: "container",
        header: "Container",
        className: "font-mono",
        render: (r) => r.container_number ?? "—",
      },
      {
        key: "temp",
        header: "Temperature",
        align: "right",
        className: "tabular-nums",
        render: (r) => tempOf(r),
      },
      {
        key: "avail",
        header: "Availability",
        render: (r) => <StatusChip label={String(r.status ?? "—")} tone={reeferTone(r.status)} />,
      },
      { key: "alloc", header: "Allocation", render: (r) => allocOf(r) },
    ],
    [],
  );

  return (
    <PageContainer>
      <StatGrid>
        <StatCard label="Total" value={totals.total ?? 0} tone="info" loading={availQ.isLoading} />
        <StatCard
          label="Available"
          value={totals.available ?? 0}
          tone="ok"
          loading={availQ.isLoading}
        />
        <StatCard
          label="Occupied"
          value={totals.occupied ?? 0}
          tone="warn"
          loading={availQ.isLoading}
        />
      </StatGrid>

      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => String(r.slot_code ?? r.id ?? Math.random())}
        status={mergeStatus(slotsQ)}
        onRetry={() => slotsQ.refetch()}
        emptyLabel="No reefer slots in RDS yet."
        search={(r, q) => includesQ(`${r.slot_code ?? ""} ${r.container_number ?? ""}`, q)}
        searchPlaceholder="Search slot / container…"
        toolbar={
          <ReportToolbar
            onExport={() =>
              exportCsv(
                "reefer-report.csv",
                REEFER_HEADERS,
                rows.map((r) => [
                  String(r.slot_code ?? ""),
                  String(r.container_number ?? "—"),
                  tempOf(r),
                  String(r.status ?? ""),
                  allocOf(r),
                ]),
              )
            }
          />
        }
      />
    </PageContainer>
  );
}

// --- 5. Integrations ---------------------------------------------------------

const INTEGRATION_HEADERS = ["System", "Status", "Configured", "Last Sync"];

function integrationTone(mode?: string | null): Tone {
  return String(mode ?? "").toUpperCase() === "LIVE" ? "ok" : "warn";
}

export function IntegrationReport() {
  const pdpQ = useQuery({
    queryKey: ["report", "pdp-health"],
    queryFn: () => api.pdpHealth(),
    refetchInterval: POLL_MS,
  });
  const ldbQ = useQuery({
    queryKey: ["report", "ldb-health"],
    queryFn: () => api.ldbHealth(),
    refetchInterval: POLL_MS,
  });
  const rmsQ = useQuery({
    queryKey: ["report", "rms-health"],
    queryFn: () => api.rmsHealth(),
    refetchInterval: POLL_MS,
  });
  const nvrQ = useQuery({
    queryKey: ["report", "nvr-health"],
    queryFn: () => api.nvrHealth(),
    refetchInterval: POLL_MS,
  });

  // Client-side health-check time — the last time these adapters were polled.
  const lastSync = new Date().toLocaleTimeString();

  const rows: any[] = useMemo(() => {
    const h = (q: any) => (q.data ?? {}) as any;
    return [
      { system: "PDP", mode: h(pdpQ).mode, configured: h(pdpQ).configured },
      { system: "LDB", mode: h(ldbQ).mode, configured: h(ldbQ).configured },
      { system: "RMS-TAS", mode: h(rmsQ).mode, configured: h(rmsQ).configured },
      { system: "NVR", mode: h(nvrQ).mode, configured: h(nvrQ).configured },
      { system: "WEATHER", mode: "MOCK", configured: false },
    ];
  }, [pdpQ.data, ldbQ.data, rmsQ.data, nvrQ.data]);

  const modeLabel = (r: any) => String(r.mode ?? "MOCK").toUpperCase();
  const configuredLabel = (r: any) => (r.configured ? "Yes" : "No");

  const columns: Column<any>[] = useMemo(
    () => [
      { key: "system", header: "System", className: "font-medium", render: (r) => r.system },
      {
        key: "status",
        header: "Status",
        render: (r) => <StatusChip label={modeLabel(r)} tone={integrationTone(r.mode)} />,
      },
      { key: "configured", header: "Configured", render: (r) => configuredLabel(r) },
      {
        key: "lastsync",
        header: "Last Sync",
        className: "whitespace-nowrap text-muted-foreground",
        render: () => lastSync,
      },
    ],
    [lastSync],
  );

  return (
    <PageContainer>
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => String(r.system)}
        status={mergeStatus(pdpQ, ldbQ, rmsQ, nvrQ)}
        onRetry={() => {
          void pdpQ.refetch();
          void ldbQ.refetch();
          void rmsQ.refetch();
          void nvrQ.refetch();
        }}
        emptyLabel="No integration adapters reported."
        search={(r, q) => includesQ(`${r.system} ${modeLabel(r)}`, q)}
        searchPlaceholder="Search system…"
        toolbar={
          <ReportToolbar
            onExport={() =>
              exportCsv(
                "integration-report.csv",
                INTEGRATION_HEADERS,
                rows.map((r) => [String(r.system), modeLabel(r), configuredLabel(r), lastSync]),
              )
            }
          />
        }
      />
      <p className="px-1 text-[11px] text-muted-foreground">
        Last Sync shows the last health-check time. External systems run on labeled MOCK adapters
        until *_BASE_URL is configured.
      </p>
    </PageContainer>
  );
}
