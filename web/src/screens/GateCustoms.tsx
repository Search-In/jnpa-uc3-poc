// Customs & Gate console — e-Seal / Form-13 / Weighbridge / ICEGATE captures,
// Auto-LEO reconciliation, and the Customs-flag feed. Every row is RDS-backed
// (jnpa.gate_captures / leo_reconciliation / alerts) via /api/gate-data/* — no
// synthetic runtime data. Redesigned onto the DTCCC kit (provider strip, summary
// cards, tabbed searchable tables). Per-source provider mode (SIM|LIVE) is shown
// as a badge so the operator sees which sources are wired to a real endpoint.

import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ShieldCheck,
  PackageCheck,
  Scale,
  FileText,
  Flag,
  ClipboardCheck,
  Camera,
} from "lucide-react";
import { api } from "@/lib/api";
import { useIncomingSearch } from "@/lib/searchStore";
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
import CustomsDetailsDrawer from "@/components/panels/CustomsDetailsDrawer";
import AutoLeoJoinPanel from "@/components/panels/AutoLeoJoinPanel";
import { fmtDateTimeIST } from "@/lib/utils";
import type { CustomsAlert, GateCapture } from "@/lib/types";

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
  // Container selected for the ICEGATE customs details drawer (null = closed).
  const [selectedContainer, setSelectedContainer] = useState<string | null>(null);

  // Header Global Search hand-off. The omnibox routes a CONTAINER query here;
  // before this the query was dropped and the operator landed unfiltered.
  const incomingSearch = useIncomingSearch(["container", "vehicle"]);
  useEffect(() => {
    if (incomingSearch) setTab("captures");
  }, [incomingSearch]);

  const providersQ = useQuery({ queryKey: ["gate-providers"], queryFn: () => api.gateProviders() });
  const capturesQ = useQuery({
    queryKey: ["gate-captures", captureType],
    queryFn: () => api.gateCaptures(captureType, undefined, 200),
  });
  const customsQ = useQuery({
    queryKey: ["customs-history"],
    queryFn: () => api.customsHistory(200),
  });

  const sources = providersQ.data?.sources ?? {};
  const captures = capturesQ.data?.captures ?? [];
  const customs = customsQ.data?.alerts ?? [];

  const updatedAt = Math.max(capturesQ.dataUpdatedAt || 0, customsQ.dataUpdatedAt || 0);
  const anyFetching = capturesQ.isFetching || customsQ.isFetching;

  function refreshAll() {
    void qc.invalidateQueries({ queryKey: ["gate-captures"] });
    void qc.invalidateQueries({ queryKey: ["auto-leo-board"] });
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
            { key: "leo", label: "Auto-LEO", icon: ShieldCheck },
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
              onRowClick={(c) => c.container_no && setSelectedContainer(c.container_no)}
              initialSearch={incomingSearch}
            />
          </Card>
        )}
        {tab === "leo" && (
          // UC3-040. The four-way join replaces the flags-only reconciliation
          // list this tab used to show: that list reported WHICH flags fired but
          // not WHICH of the four evidence streams caused them, so a missing
          // weighbridge and a disagreeing one looked identical. The panel keeps
          // the same tab, the same audience and the same route.
          <AutoLeoJoinPanel />
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

      {/* ICEGATE row details — customs document view + workflow timeline. */}
      <CustomsDetailsDrawer
        containerNo={selectedContainer}
        onClose={() => setSelectedContainer(null)}
      />
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
  onRowClick,
  initialSearch,
}: {
  rows: GateCapture[];
  status: any;
  onRetry: () => void;
  type: string;
  onRowClick?: (c: GateCapture) => void;
  initialSearch?: string;
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
      onRowClick={type === "ICEGATE" ? onRowClick : undefined}
      emptyLabel={`No ${type} captures in RDS yet.`}
      search={(c, q) =>
        `${c.container_no ?? ""} ${c.vehicle_plate ?? ""} ${c.status ?? ""}`
          .toLowerCase()
          .includes(q)
      }
      searchPlaceholder="Search container / vehicle…"
      initialSearch={initialSearch}
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
