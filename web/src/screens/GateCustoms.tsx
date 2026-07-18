// Customs & Gate console — e-Seal / Form-13 / Weighbridge / ICEGATE captures,
// Auto-LEO reconciliation, and the Customs-flag feed. Every row is RDS-backed
// (jnpa.gate_captures / leo_reconciliation / alerts) via /api/gate-data/* — no
// synthetic runtime data. Redesigned onto the DTCCC kit (provider strip, summary
// cards, tabbed searchable tables). Per-source provider mode (SIM|LIVE) is shown
// as a badge so the operator sees which sources are wired to a real endpoint.

import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ShieldCheck,
  PackageCheck,
  Scale,
  FileText,
  Container,
  Flag,
  ClipboardCheck,
  Camera,
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
  Embedded,
  type Column,
  type Tone,
} from "@/components/ui/dtccc";
import CameraAI from "@/screens/CameraAI";
import { fmtDateTimeIST } from "@/lib/utils";
import type { CustomsAlert, GateCapture, LeoReconciliation } from "@/lib/types";

type TabKey = "captures" | "leo" | "customs" | "camera";

const CAPTURE_TYPES = [
  { key: "ESEAL", label: "e-Seal", icon: ShieldCheck },
  { key: "FORM13", label: "Form-13", icon: FileText },
  { key: "WEIGHBRIDGE", label: "Weighbridge", icon: Scale },
  { key: "ICEGATE", label: "ICEGATE", icon: PackageCheck },
] as const;

function ModeChip({ mode }: { mode: string }) {
  const live = mode === "live";
  return <StatusChip label={live ? "LIVE" : "SIM"} tone={live ? "ok" : "neutral"} />;
}

export default function GateCustoms() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<TabKey>("captures");
  const [captureType, setCaptureType] = useState<string>("ESEAL");

  const providersQ = useQuery({ queryKey: ["gate-providers"], queryFn: () => api.gateProviders() });
  const capturesQ = useQuery({
    queryKey: ["gate-captures", captureType],
    queryFn: () => api.gateCaptures(captureType, undefined, 200),
  });
  const leoQ = useQuery({
    queryKey: ["leo-recon"],
    queryFn: () => api.gateReconciliations(undefined, 200),
  });
  const customsQ = useQuery({
    queryKey: ["customs-history"],
    queryFn: () => api.customsHistory(200),
  });

  const sources = providersQ.data?.sources ?? {};
  const captures = capturesQ.data?.captures ?? [];
  const recon = leoQ.data?.reconciliations ?? [];
  const customs = customsQ.data?.alerts ?? [];

  const leoReady = recon.filter((r) => r.leo_ready).length;
  const leoBlocked = recon.length - leoReady;

  const updatedAt = Math.max(
    capturesQ.dataUpdatedAt || 0,
    leoQ.dataUpdatedAt || 0,
    customsQ.dataUpdatedAt || 0,
  );
  const anyFetching = capturesQ.isFetching || leoQ.isFetching || customsQ.isFetching;

  function refreshAll() {
    void qc.invalidateQueries({ queryKey: ["gate-captures"] });
    void qc.invalidateQueries({ queryKey: ["leo-recon"] });
    void qc.invalidateQueries({ queryKey: ["customs-history"] });
    void qc.invalidateQueries({ queryKey: ["gate-providers"] });
  }

  return (
    <PageContainer>
      <PageHeader
        icon={ShieldCheck}
        title="Customs & Gate"
        subtitle="e-Seal · Form-13 · Weighbridge · ICEGATE · Auto-LEO · RDS-backed"
        updatedAt={updatedAt}
        isFetching={anyFetching}
        onRefresh={refreshAll}
      />

      {/* Provider mode strip */}
      <div className="flex flex-wrap gap-2 px-4 pt-3">
        {CAPTURE_TYPES.map(({ key, label, icon: Icon }) => (
          <div
            key={key}
            className="flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1.5"
          >
            <Icon className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
            <span className="text-xs font-medium text-foreground">{label}</span>
            <ModeChip mode={sources[key]?.mode ?? "sim"} />
          </div>
        ))}
      </div>

      {/* Summary cards */}
      <div className="px-4 pt-3">
        <StatGrid>
          <StatCard
            icon={ClipboardCheck}
            label={`${CAPTURE_TYPES.find((c) => c.key === captureType)?.label} Captures`}
            value={captures.length}
            tone="info"
            loading={capturesQ.isLoading}
          />
          <StatCard
            icon={ShieldCheck}
            label="LEO Ready"
            value={leoReady}
            tone="ok"
            loading={leoQ.isLoading}
          />
          <StatCard
            icon={Container}
            label="LEO Blocked"
            value={leoBlocked}
            tone={leoBlocked > 0 ? "warn" : "ok"}
            loading={leoQ.isLoading}
          />
          <StatCard
            icon={Flag}
            label="Customs Flags"
            value={customs.length}
            tone={customs.length > 0 ? "critical" : "ok"}
            loading={customsQ.isLoading}
          />
        </StatGrid>
      </div>

      {/* Tabs + tables */}
      <div className="px-4 py-3">
        <SegmentedTabs
          value={tab}
          onChange={setTab}
          className="mb-3"
          tabs={[
            { key: "captures", label: "Gate Captures", icon: ClipboardCheck },
            { key: "leo", label: "Auto-LEO", icon: ShieldCheck, count: recon.length },
            { key: "customs", label: "Customs Flags", icon: Flag, count: customs.length },
            { key: "camera", label: "Camera AI", icon: Camera },
          ]}
        />

        {tab === "captures" && (
          <Card className="overflow-hidden">
            <div className="border-b border-border px-3 py-2">
              <SegmentedTabs
                value={captureType}
                onChange={setCaptureType}
                tabs={CAPTURE_TYPES.map((c) => ({ key: c.key, label: c.label, icon: c.icon }))}
              />
            </div>
            <CapturesTable
              rows={captures}
              status={capturesQ}
              onRetry={() => capturesQ.refetch()}
              type={captureType}
            />
          </Card>
        )}
        {tab === "leo" && (
          <Card className="overflow-hidden">
            <LeoTable rows={recon} status={leoQ} onRetry={() => leoQ.refetch()} />
          </Card>
        )}
        {tab === "customs" && (
          <Card className="overflow-hidden">
            <CustomsTable rows={customs} status={customsQ} onRetry={() => customsQ.refetch()} />
          </Card>
        )}
        {tab === "camera" && (
          <Embedded>
            <CameraAI />
          </Embedded>
        )}
      </div>
    </PageContainer>
  );
}

function captureStatusTone(status?: string | null): Tone {
  return status === "TAMPERED" || status === "PENDING" ? "critical" : "ok";
}

function CapturesTable({
  rows,
  status,
  onRetry,
  type,
}: {
  rows: GateCapture[];
  status: any;
  onRetry: () => void;
  type: string;
}) {
  const columns: Column<GateCapture>[] = useMemo(
    () => [
      {
        key: "container",
        header: "Container",
        className: "font-mono",
        render: (c) => c.container_no ?? "—",
      },
      {
        key: "vehicle",
        header: "Vehicle",
        className: "font-mono",
        render: (c) => c.vehicle_plate ?? "—",
      },
      {
        key: "status",
        header: "Status",
        render: (c) => <StatusChip label={c.status ?? "—"} tone={captureStatusTone(c.status)} />,
      },
      { key: "source", header: "Source", render: (c) => <ModeChip mode={c.source_mode} /> },
      {
        key: "captured",
        header: "Captured",
        className: "text-muted-foreground",
        render: (c) => (c.captured_at ? fmtDateTimeIST(c.captured_at) : "—"),
      },
    ],
    [],
  );
  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(c) => String(c.id)}
      status={status}
      onRetry={onRetry}
      emptyLabel={`No ${type} captures in RDS yet.`}
      search={(c, q) =>
        `${c.container_no ?? ""} ${c.vehicle_plate ?? ""} ${c.status ?? ""}`
          .toLowerCase()
          .includes(q)
      }
      searchPlaceholder="Search container / vehicle…"
      pageSize={10}
    />
  );
}

function LeoTable({
  rows,
  status,
  onRetry,
}: {
  rows: LeoReconciliation[];
  status: any;
  onRetry: () => void;
}) {
  const columns: Column<LeoReconciliation>[] = [
    {
      key: "container",
      header: "Container",
      className: "font-mono",
      render: (r) => r.container_no ?? "—",
    },
    {
      key: "vehicle",
      header: "Vehicle",
      className: "font-mono",
      render: (r) => r.vehicle_plate ?? "—",
    },
    {
      key: "leo",
      header: "LEO",
      render: (r) => (
        <StatusChip
          label={r.leo_ready ? "READY" : "BLOCKED"}
          tone={r.leo_ready ? "ok" : "critical"}
        />
      ),
    },
    {
      key: "flags",
      header: "Customs Flags",
      render: (r) =>
        r.customs_flags.length ? (
          <div className="flex flex-wrap gap-1">
            {r.customs_flags.map((f) => (
              <StatusChip key={f} label={f} tone="warn" />
            ))}
          </div>
        ) : (
          <span className="text-muted-foreground">—</span>
        ),
    },
    {
      key: "reconciled",
      header: "Reconciled",
      className: "text-muted-foreground",
      render: (r) => fmtDateTimeIST(r.reconciled_at),
    },
  ];
  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(r) => String(r.id)}
      status={status}
      onRetry={onRetry}
      emptyLabel="No reconciliations in RDS yet."
      search={(r, q) =>
        `${r.container_no ?? ""} ${r.vehicle_plate ?? ""} ${r.customs_flags.join(" ")}`
          .toLowerCase()
          .includes(q)
      }
      searchPlaceholder="Search container / vehicle…"
      pageSize={10}
    />
  );
}

function CustomsTable({
  rows,
  status,
  onRetry,
}: {
  rows: CustomsAlert[];
  status: any;
  onRetry: () => void;
}) {
  const columns: Column<CustomsAlert>[] = [
    {
      key: "flag",
      header: "Flag",
      className: "font-medium",
      render: (a) => String(a.payload?.flag ?? "—"),
    },
    {
      key: "severity",
      header: "Severity",
      render: (a) => (
        <StatusChip label={a.severity} tone={a.severity === "critical" ? "critical" : "warn"} />
      ),
    },
    {
      key: "container",
      header: "Container",
      className: "font-mono",
      render: (a) => String(a.payload?.container_no ?? "—"),
    },
    { key: "vehicle", header: "Vehicle", className: "font-mono", render: (a) => a.plate ?? "—" },
    {
      key: "raised",
      header: "Raised",
      className: "text-muted-foreground",
      render: (a) => fmtDateTimeIST(a.ts),
    },
  ];
  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(a) => a.id}
      status={status}
      onRetry={onRetry}
      emptyLabel="No customs flags in RDS yet."
      search={(a, q) =>
        `${String(a.payload?.flag ?? "")} ${String(a.payload?.container_no ?? "")} ${a.plate ?? ""} ${a.severity}`
          .toLowerCase()
          .includes(q)
      }
      searchPlaceholder="Search flags…"
      pageSize={10}
    />
  );
}
