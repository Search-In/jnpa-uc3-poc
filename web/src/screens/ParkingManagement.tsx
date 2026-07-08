// Parking Management — capacity / availability / occupancy, live vehicle list,
// entry-exit history and parking violations. Every figure is RDS-backed
// (jnpa.parking_facilities / parking_slots / parking_transactions / parking_events)
// via /api/parking/* — no synthetic occupancy. Redesigned onto the DTCCC kit
// (summary cards, occupancy chart, facilities map, tabbed searchable tables).

import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { SquareParking, Car, History, TriangleAlert, ParkingCircle, Ban, CheckCircle2 } from "lucide-react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { ArcgisMap } from "@/components/map/ArcgisMap";
import { useMapSettings } from "@/lib/mapSettings";
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
import type { ParkingFacility, ParkingFacilityRow, ParkingTransaction, ParkingViolation } from "@/lib/types";

type TabKey = "facilities" | "vehicles" | "history" | "violations";

function statusTone(status?: string | null, freePct?: number | null): Tone {
  if (status === "FULL") return "critical";
  if ((freePct ?? 100) < 15) return "warn";
  return "ok";
}

export default function ParkingManagement() {
  const qc = useQueryClient();
  const { basemap } = useMapSettings();
  const [tab, setTab] = useState<TabKey>("facilities");

  // Distinct key from the adapter-shaped ["parking-summary"] used by the Live-Ops
  // ParkingBoard — this one returns the {available,capacity,...} api shape, so a
  // shared key would let one screen read the other's differently-named fields.
  const sumQ = useQuery({ queryKey: ["parking-summary-mgmt"], queryFn: () => api.parkingSummary(), refetchInterval: 10000 });
  const availQ = useQuery({ queryKey: ["parking-avail"], queryFn: () => api.parkingAvailability(), refetchInterval: 10000 });
  const histQ = useQuery({ queryKey: ["parking-hist"], queryFn: () => api.parkingHistory(200) });
  const violQ = useQuery({ queryKey: ["parking-viol"], queryFn: () => api.parkingViolations(200) });

  const s = sumQ.data;
  const facilities = availQ.data?.facilities ?? [];
  const activeVehicles = (histQ.data?.transactions ?? []).filter((t) => t.status === "ACTIVE");

  const utilPct = s && s.capacity ? Math.round((s.occupied / s.capacity) * 100) : 0;

  const chartData = useMemo(
    () =>
      facilities.map((f) => ({
        name: (f.name ?? f.facility_id).replace(/^Parking\s*/i, "").slice(0, 12),
        occupied: f.occupied,
        available: f.available,
      })),
    [facilities],
  );

  // Map facilities (with coords) onto the ArcgisMap parking layer.
  const mapFacilities: ParkingFacility[] = useMemo(
    () =>
      facilities
        .filter((f) => f.lat != null && f.lon != null)
        .map((f) => ({
          facility_id: f.facility_id,
          name: f.name ?? f.facility_id,
          gate_id: f.gate_id,
          lat: f.lat as number,
          lon: f.lon as number,
          capacity: f.capacity,
          occupied: f.occupied,
          available: f.available,
          utilisation_pct: f.free_pct != null ? 100 - f.free_pct : 0,
          status: f.status,
        })),
    [facilities],
  );

  function refreshAll() {
    void qc.invalidateQueries({ queryKey: ["parking-summary-mgmt"] });
    void qc.invalidateQueries({ queryKey: ["parking-avail"] });
    void qc.invalidateQueries({ queryKey: ["parking-hist"] });
    void qc.invalidateQueries({ queryKey: ["parking-viol"] });
  }

  return (
    <PageContainer>
      <PageHeader
        icon={SquareParking}
        title="Parking Management"
        subtitle="Geo-fenced port holding yards · RDS-backed"
        updatedAt={sumQ.dataUpdatedAt}
        isFetching={sumQ.isFetching && !sumQ.isLoading}
        onRefresh={refreshAll}
      />

      {/* Summary cards */}
      <div className="px-4 pt-3">
        <StatGrid className="lg:grid-cols-5">
          <StatCard icon={ParkingCircle} label="Total Capacity" value={s?.capacity ?? "—"} tone="info" loading={sumQ.isLoading} />
          <StatCard icon={Car} label="Occupied" value={s?.occupied ?? "—"} tone="warn" loading={sumQ.isLoading} sub={`${utilPct}% utilised`} />
          <StatCard icon={CheckCircle2} label="Available" value={s?.available ?? "—"} tone="ok" loading={sumQ.isLoading} />
          <StatCard icon={Ban} label="Full Facilities" value={s?.full ?? "—"} tone={(s?.full ?? 0) > 0 ? "critical" : "ok"} loading={sumQ.isLoading} />
          <StatCard icon={TriangleAlert} label="Violations" value={violQ.data?.violations?.length ?? "—"} tone={(violQ.data?.violations?.length ?? 0) > 0 ? "warn" : "ok"} loading={violQ.isLoading} />
        </StatGrid>
      </div>

      {/* Occupancy chart + facilities map */}
      <div className="grid grid-cols-1 gap-3 px-4 pt-3 lg:grid-cols-2">
        <Card className="p-3">
          <h2 className="mb-2 text-sm font-semibold text-foreground">Occupancy by Facility</h2>
          {chartData.length === 0 ? (
            <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">No facilities in RDS.</div>
          ) : (
            <div className="h-48">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={chartData} margin={{ top: 4, right: 8, left: -18, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="hsl(215 20% 90%)" vertical={false} />
                  <XAxis dataKey="name" tick={{ fontSize: 10 }} interval={0} angle={-15} textAnchor="end" height={44} />
                  <YAxis tick={{ fontSize: 10 }} />
                  <Tooltip contentStyle={{ fontSize: 12, borderRadius: 8 }} />
                  <Bar dataKey="occupied" stackId="a" fill={STATUS.warning} radius={[0, 0, 0, 0]} />
                  <Bar dataKey="available" stackId="a" fill={STATUS.ok} radius={[3, 3, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </Card>
        <Card className="relative h-56 overflow-hidden p-0 lg:h-auto lg:min-h-[16rem]">
          <ArcgisMap basemap={basemap} parkingFacilities={mapFacilities} />
        </Card>
      </div>

      {/* Tabbed tables */}
      <div className="px-4 py-3">
        <SegmentedTabs
          value={tab}
          onChange={setTab}
          className="mb-3"
          tabs={[
            { key: "facilities", label: "Facilities", icon: SquareParking, count: facilities.length },
            { key: "vehicles", label: "Vehicles", icon: Car, count: activeVehicles.length },
            { key: "history", label: "Entry / Exit History", icon: History },
            { key: "violations", label: "Violations", icon: TriangleAlert, count: violQ.data?.violations?.length },
          ]}
        />
        <Card className="overflow-hidden">
          {tab === "facilities" && <FacilitiesTable rows={facilities} status={availQ} onRetry={() => availQ.refetch()} />}
          {tab === "vehicles" && <VehiclesTable rows={activeVehicles} status={histQ} onRetry={() => histQ.refetch()} />}
          {tab === "history" && <HistoryTable rows={histQ.data?.transactions ?? []} status={histQ} onRetry={() => histQ.refetch()} />}
          {tab === "violations" && <ViolationsTable rows={violQ.data?.violations ?? []} status={violQ} onRetry={() => violQ.refetch()} />}
        </Card>
      </div>
    </PageContainer>
  );
}

function FacilitiesTable({ rows, status, onRetry }: { rows: ParkingFacilityRow[]; status: any; onRetry: () => void }) {
  const columns: Column<ParkingFacilityRow>[] = [
    { key: "name", header: "Facility", render: (f) => <span className="font-medium">{f.name ?? f.facility_id}</span> },
    { key: "capacity", header: "Capacity", align: "right", className: "tabular-nums", render: (f) => f.capacity },
    { key: "occupied", header: "Occupied", align: "right", className: "tabular-nums", render: (f) => f.occupied },
    { key: "available", header: "Available", align: "right", className: "tabular-nums", render: (f) => f.available },
    { key: "free", header: "Free %", align: "right", className: "tabular-nums", render: (f) => `${f.free_pct ?? "—"}%` },
    { key: "status", header: "Status", render: (f) => <StatusChip label={f.status} tone={statusTone(f.status, f.free_pct)} /> },
  ];
  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(f) => f.facility_id}
      status={status}
      onRetry={onRetry}
      emptyLabel="No facilities in RDS."
      search={(f, q) => `${f.name ?? ""} ${f.facility_id} ${f.status}`.toLowerCase().includes(q)}
      searchPlaceholder="Search facilities…"
      pageSize={10}
    />
  );
}

function VehiclesTable({ rows, status, onRetry }: { rows: ParkingTransaction[]; status: any; onRetry: () => void }) {
  const columns: Column<ParkingTransaction>[] = [
    { key: "vehicle", header: "Vehicle", className: "font-mono", render: (t) => t.vehicle_id },
    { key: "driver", header: "Driver", render: (t) => t.driver_id ?? "—" },
    { key: "facility", header: "Facility", render: (t) => t.facility_id },
    { key: "entry", header: "Entry", className: "text-muted-foreground", render: (t) => (t.entry_time ? fmtDateTimeIST(t.entry_time) : "—") },
  ];
  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(t) => String(t.id)}
      status={status}
      onRetry={onRetry}
      emptyLabel="No vehicles currently parked."
      search={(t, q) => `${t.vehicle_id ?? ""} ${t.driver_id ?? ""} ${t.facility_id ?? ""}`.toLowerCase().includes(q)}
      searchPlaceholder="Search vehicle / driver…"
      pageSize={10}
    />
  );
}

function HistoryTable({ rows, status, onRetry }: { rows: ParkingTransaction[]; status: any; onRetry: () => void }) {
  const columns: Column<ParkingTransaction>[] = [
    { key: "vehicle", header: "Vehicle", className: "font-mono", render: (t) => t.vehicle_id },
    { key: "facility", header: "Facility", render: (t) => t.facility_id },
    { key: "entry", header: "Entry", className: "text-muted-foreground", render: (t) => (t.entry_time ? fmtDateTimeIST(t.entry_time) : "—") },
    { key: "exit", header: "Exit", className: "text-muted-foreground", render: (t) => (t.exit_time ? fmtDateTimeIST(t.exit_time) : "—") },
    { key: "dur", header: "Duration", align: "right", className: "tabular-nums", render: (t) => (t.duration_s != null ? `${Math.round(t.duration_s / 60)}m` : "—") },
    { key: "status", header: "Status", render: (t) => <StatusChip label={t.status} tone={t.status === "ACTIVE" ? "warn" : "ok"} /> },
  ];
  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(t) => String(t.id)}
      status={status}
      onRetry={onRetry}
      emptyLabel="No parking history in RDS yet."
      search={(t, q) => `${t.vehicle_id ?? ""} ${t.facility_id ?? ""} ${t.status}`.toLowerCase().includes(q)}
      searchPlaceholder="Search history…"
      pageSize={10}
    />
  );
}

function ViolationsTable({ rows, status, onRetry }: { rows: ParkingViolation[]; status: any; onRetry: () => void }) {
  const columns: Column<ParkingViolation>[] = [
    { key: "type", header: "Type", render: (v) => <StatusChip label={v.event_type} tone="critical" /> },
    { key: "vehicle", header: "Vehicle", className: "font-mono", render: (v) => v.vehicle_id ?? "—" },
    { key: "facility", header: "Facility", render: (v) => v.facility_id ?? "—" },
    { key: "when", header: "When", className: "text-muted-foreground", render: (v) => fmtDateTimeIST(v.created_at) },
  ];
  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(v) => String(v.id)}
      status={status}
      onRetry={onRetry}
      emptyLabel="No parking violations in RDS."
      search={(v, q) => `${v.event_type} ${v.vehicle_id ?? ""} ${v.facility_id ?? ""}`.toLowerCase().includes(q)}
      searchPlaceholder="Search violations…"
      pageSize={10}
    />
  );
}
