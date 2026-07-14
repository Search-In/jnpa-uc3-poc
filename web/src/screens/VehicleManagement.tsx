// Vehicle Management — the Vehicle Master (fleet registry) admin console. The
// authoritative list of vehicles a driver may be assigned to: register, search,
// filter and change status (ACTIVE / INACTIVE / MAINTENANCE). The Driver
// Enrollment "assign vehicle" dropdown draws ONLY from the ACTIVE, unassigned
// vehicles here (GET /api/vehicles/available). Restricted to CUSTOMS / ADMIN.

import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Truck, Plus, Pencil, Eye, Power, Wrench, CheckCircle2, CircleSlash } from "lucide-react";
import { getAdapter } from "@/data";
import type { FleetVehicle, VehicleStatus } from "@/lib/types";
import { Card } from "@/components/ui/card";
import { Spinner } from "@/components/ui/misc";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
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

const FILTERS = ["ALL", "ACTIVE", "INACTIVE", "MAINTENANCE"] as const;
type Filter = (typeof FILTERS)[number];

function statusTone(status?: string): Tone {
  switch ((status ?? "").toUpperCase()) {
    case "ACTIVE":
      return "ok";
    case "MAINTENANCE":
      return "warn";
    case "INACTIVE":
      return "critical";
    default:
      return "neutral";
  }
}

export default function VehicleManagement() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [filter, setFilter] = useState<Filter>("ALL");
  const [createOpen, setCreateOpen] = useState(false);
  const [editVehicle, setEditVehicle] = useState<FleetVehicle | null>(null);
  const [viewVehicle, setViewVehicle] = useState<FleetVehicle | null>(null);

  const listQ = useQuery({
    queryKey: ["fleet-vehicles"],
    queryFn: () => getAdapter().vehicles(),
  });
  const statsQ = useQuery({
    queryKey: ["fleet-stats"],
    queryFn: () => getAdapter().vehicleStats(),
  });
  const all = listQ.data ?? [];

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["fleet-vehicles"] });
    void qc.invalidateQueries({ queryKey: ["fleet-stats"] });
    // Keep the enrollment dropdown in sync (available set changed).
    void qc.invalidateQueries({ queryKey: ["available-vehicles"] });
  };

  const counts = useMemo(() => {
    const c = { ALL: all.length, ACTIVE: 0, INACTIVE: 0, MAINTENANCE: 0 } as Record<Filter, number>;
    for (const v of all) {
      const s = (v.status ?? "").toUpperCase();
      if (s === "ACTIVE") c.ACTIVE++;
      else if (s === "INACTIVE") c.INACTIVE++;
      else if (s === "MAINTENANCE") c.MAINTENANCE++;
    }
    return c;
  }, [all]);

  const rows = useMemo(
    () => (filter === "ALL" ? all : all.filter((v) => (v.status ?? "").toUpperCase() === filter)),
    [all, filter],
  );

  const stats = statsQ.data;

  const columns: Column<FleetVehicle>[] = [
    {
      key: "vehicle_id",
      header: t("vehicles.vehicleId", "Vehicle ID"),
      className: "font-mono font-medium",
      render: (v) => v.vehicle_id,
    },
    {
      key: "vehicle_number",
      header: t("vehicles.number", "Vehicle Number"),
      className: "font-mono",
      render: (v) => v.vehicle_number || "—",
    },
    {
      key: "vehicle_type",
      header: t("vehicles.type", "Type"),
      render: (v) => v.vehicle_type || "—",
    },
    {
      key: "status",
      header: t("vehicles.status", "Status"),
      render: (v) => <StatusChip label={v.status} tone={statusTone(v.status)} />,
    },
    {
      key: "assigned_driver",
      header: t("vehicles.assignedDriver", "Assigned Driver"),
      render: (v) =>
        v.assigned_driver ? (
          <div>
            <div className="font-medium text-foreground">{v.assigned_driver.name}</div>
            <div className="font-mono text-[10px] text-muted-foreground">
              {v.assigned_driver.driver_id}
            </div>
          </div>
        ) : (
          <span className="text-[11px] text-muted-foreground">
            {t("vehicles.unassigned", "Unassigned")}
          </span>
        ),
    },
    {
      key: "updated_at",
      header: t("vehicles.updated", "Last Updated"),
      className: "text-muted-foreground",
      render: (v) => (v.updated_at ? fmtDateTimeIST(v.updated_at) : "—"),
    },
    {
      key: "actions",
      header: t("vehicles.actions", "Actions"),
      align: "right",
      render: (v) => (
        <RowActions
          row={v}
          onView={() => setViewVehicle(v)}
          onEdit={() => setEditVehicle(v)}
          onChanged={invalidate}
        />
      ),
    },
  ];

  return (
    <PageContainer>
      <PageHeader
        icon={Truck}
        title={t("vehicles.title", "Vehicle Management")}
        subtitle={t(
          "vehicles.subtitle",
          "Register and manage fleet vehicles. Only ACTIVE, unassigned vehicles are offered for driver assignment.",
        )}
        updatedAt={listQ.dataUpdatedAt}
        isFetching={listQ.isFetching && !listQ.isLoading}
        onRefresh={invalidate}
        actions={
          <button
            type="button"
            onClick={() => setCreateOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-semibold text-primary-foreground transition-colors hover:bg-primary/90"
          >
            <Plus className="h-3.5 w-3.5" />
            {t("vehicles.add", "Add Vehicle")}
          </button>
        }
      />

      <div className="px-4 pt-3">
        <StatGrid className="lg:grid-cols-4">
          <StatCard
            icon={Truck}
            label={t("vehicles.total", "Total Vehicles")}
            value={stats?.total ?? 0}
            tone="info"
            loading={statsQ.isLoading}
          />
          <StatCard
            icon={CheckCircle2}
            label={t("vehicles.active", "Active Vehicles")}
            value={stats?.active ?? 0}
            tone="ok"
            loading={statsQ.isLoading}
          />
          <StatCard
            icon={CircleSlash}
            label={t("vehicles.assigned", "Assigned Vehicles")}
            value={stats?.assigned ?? 0}
            tone="neutral"
            loading={statsQ.isLoading}
          />
          <StatCard
            icon={Power}
            label={t("vehicles.available", "Available Vehicles")}
            value={stats?.available ?? 0}
            tone={stats && stats.available > 0 ? "ok" : "warn"}
            loading={statsQ.isLoading}
          />
        </StatGrid>
      </div>

      <div className="px-4 py-3">
        <SegmentedTabs
          value={filter}
          onChange={setFilter}
          className="mb-3"
          tabs={FILTERS.map((f) => ({
            key: f,
            label: f.charAt(0) + f.slice(1).toLowerCase(),
            count: counts[f],
          }))}
        />
        <Card className="overflow-hidden">
          <DataTable
            columns={columns}
            rows={rows}
            rowKey={(v) => v.vehicle_id}
            status={listQ}
            onRetry={() => listQ.refetch()}
            emptyLabel={t("vehicles.empty", "No vehicles in this view.")}
            search={(v, q) =>
              `${v.vehicle_id} ${v.vehicle_number ?? ""} ${v.vehicle_type ?? ""} ${
                v.assigned_driver?.name ?? ""
              }`
                .toLowerCase()
                .includes(q)
            }
            searchPlaceholder={t("vehicles.searchPlaceholder", "Search vehicle / plate / driver…")}
            pageSize={12}
          />
        </Card>
      </div>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="p-0">
          <DialogHeader>
            <DialogTitle>{t("vehicles.add", "Add Vehicle")}</DialogTitle>
          </DialogHeader>
          {createOpen && (
            <VehicleForm
              onClose={() => setCreateOpen(false)}
              onSaved={() => {
                invalidate();
                setCreateOpen(false);
              }}
            />
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={!!editVehicle} onOpenChange={(o) => !o && setEditVehicle(null)}>
        <DialogContent className="p-0">
          <DialogHeader>
            <DialogTitle>{t("vehicles.edit", "Edit Vehicle")}</DialogTitle>
          </DialogHeader>
          {editVehicle && (
            <VehicleForm
              existing={editVehicle}
              onClose={() => setEditVehicle(null)}
              onSaved={() => {
                invalidate();
                setEditVehicle(null);
              }}
            />
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={!!viewVehicle} onOpenChange={(o) => !o && setViewVehicle(null)}>
        <DialogContent className="p-0">
          <DialogHeader>
            <DialogTitle>{t("vehicles.details", "Vehicle Details")}</DialogTitle>
          </DialogHeader>
          {viewVehicle && <VehicleDetail vehicle={viewVehicle} />}
        </DialogContent>
      </Dialog>
    </PageContainer>
  );
}

const STATUS_OPTIONS: VehicleStatus[] = ["ACTIVE", "INACTIVE", "MAINTENANCE"];

function VehicleForm({
  existing,
  onClose,
  onSaved,
}: {
  existing?: FleetVehicle;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const editing = !!existing;
  const vehicleId = existing?.vehicle_id ?? "";
  const [number, setNumber] = useState(existing?.vehicle_number ?? "");
  const [type, setType] = useState(existing?.vehicle_type ?? "Container Truck");
  const [chassis, setChassis] = useState(existing?.chassis_number ?? "");
  const [rfid, setRfid] = useState(existing?.rfid_fastag_id ?? "");
  const [status, setStatus] = useState<VehicleStatus>(existing?.status ?? "ACTIVE");

  const save = useMutation({
    mutationFn: async (): Promise<FleetVehicle> => {
      const res = editing
        ? await getAdapter().updateVehicle(existing!.vehicle_id, {
            vehicle_number: number.trim(),
            vehicle_type: type.trim(),
            chassis_number: chassis.trim(),
            rfid_fastag_id: rfid.trim(),
            status,
          })
        : await getAdapter().createVehicle({
            vehicle_number: number.trim(),
            vehicle_type: type.trim() || undefined,
            chassis_number: chassis.trim() || undefined,
            rfid_fastag_id: rfid.trim() || undefined,
            status,
          });
      return res.vehicle;
    },
    onSuccess: onSaved,
  });

  const canSubmit = number.trim().length > 0 && !save.isPending;
  const inputCls =
    "w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm outline-none focus:border-primary disabled:opacity-60";

  return (
    <div className="space-y-3 p-4">
      <div className="grid grid-cols-2 gap-3">
        <Labeled label={t("vehicles.vehicleId", "Vehicle ID")}>
          <input
            className={`${inputCls} font-mono`}
            value={editing ? vehicleId : ""}
            readOnly
            disabled
            placeholder={t("vehicles.idAuto", "Auto-generated (next TRK-…)")}
          />
        </Labeled>
        <Labeled label={t("vehicles.number", "Vehicle Number")} required>
          <input
            className={inputCls}
            value={number ?? ""}
            onChange={(e) => setNumber(e.target.value)}
            placeholder="MH04AB1234"
            autoFocus
          />
        </Labeled>
        <Labeled label={t("vehicles.type", "Vehicle Type")}>
          <input
            className={inputCls}
            value={type ?? ""}
            onChange={(e) => setType(e.target.value)}
          />
        </Labeled>
        <Labeled label={t("vehicles.chassis", "Chassis Number")}>
          <input
            className={inputCls}
            value={chassis ?? ""}
            onChange={(e) => setChassis(e.target.value)}
          />
        </Labeled>
        <Labeled label={t("vehicles.rfid", "RFID / FASTag ID")}>
          <input
            className={inputCls}
            value={rfid ?? ""}
            onChange={(e) => setRfid(e.target.value)}
          />
        </Labeled>
        <Labeled label={t("vehicles.status", "Status")}>
          <select
            className={inputCls}
            value={status}
            onChange={(e) => setStatus(e.target.value as VehicleStatus)}
          >
            {STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {s.charAt(0) + s.slice(1).toLowerCase()}
              </option>
            ))}
          </select>
        </Labeled>
      </div>

      {!editing && (
        <div className="text-[11px] text-muted-foreground">
          {t(
            "vehicles.idAutoNote",
            "The Vehicle ID is assigned automatically from the TRK sequence on save.",
          )}
        </div>
      )}

      {save.error && <div className="text-xs text-red-500">{(save.error as Error).message}</div>}

      <div className="flex justify-end gap-2 pt-1">
        <button
          type="button"
          onClick={onClose}
          className="rounded-md border border-border px-3 py-1.5 text-[13px] font-medium hover:bg-muted"
        >
          {t("common.cancel", "Cancel")}
        </button>
        <button
          type="button"
          disabled={!canSubmit}
          onClick={() => save.mutate()}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        >
          {save.isPending ? (
            <Spinner className="text-primary-foreground" />
          ) : editing ? (
            <Pencil className="h-4 w-4" />
          ) : (
            <Plus className="h-4 w-4" />
          )}
          {editing ? t("vehicles.saveChanges", "Save Changes") : t("vehicles.add", "Add Vehicle")}
        </button>
      </div>
    </div>
  );
}

function RowActions({
  row,
  onView,
  onEdit,
  onChanged,
}: {
  row: FleetVehicle;
  onView: () => void;
  onEdit: () => void;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const isActive = (row.status ?? "").toUpperCase() === "ACTIVE";
  const toggle = useMutation({
    mutationFn: () =>
      getAdapter().updateVehicle(row.vehicle_id, {
        status: isActive ? "INACTIVE" : "ACTIVE",
      }),
    onSuccess: onChanged,
  });
  const stop = (fn: () => void) => (e: React.MouseEvent) => {
    e.stopPropagation();
    fn();
  };
  return (
    <div className="flex items-center justify-end gap-1">
      <button
        onClick={stop(onView)}
        className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] font-medium hover:bg-muted"
      >
        <Eye className="h-3.5 w-3.5" /> {t("vehicles.view", "View")}
      </button>
      <button
        onClick={stop(onEdit)}
        className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] font-medium hover:bg-muted"
      >
        <Pencil className="h-3.5 w-3.5" /> {t("vehicles.editAction", "Edit")}
      </button>
      <button
        onClick={stop(() => toggle.mutate())}
        disabled={toggle.isPending}
        title={toggle.error ? (toggle.error as Error).message : undefined}
        className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] font-medium hover:bg-muted disabled:opacity-50"
      >
        {toggle.isPending ? (
          <Spinner />
        ) : isActive ? (
          <Power className="h-3.5 w-3.5" />
        ) : (
          <CheckCircle2 className="h-3.5 w-3.5" />
        )}{" "}
        {isActive ? t("vehicles.deactivate", "Deactivate") : t("vehicles.activate", "Activate")}
      </button>
    </div>
  );
}

function VehicleDetail({ vehicle }: { vehicle: FleetVehicle }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-4 p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Truck className="h-5 w-5 text-primary" />
          <div>
            <div className="font-mono text-sm font-semibold">{vehicle.vehicle_id}</div>
            <div className="text-[11px] text-muted-foreground">{vehicle.vehicle_number || "—"}</div>
          </div>
        </div>
        <StatusChip label={vehicle.status} tone={statusTone(vehicle.status)} />
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
        <Field k={t("vehicles.type", "Type")} v={vehicle.vehicle_type} />
        <Field k={t("vehicles.chassis", "Chassis Number")} v={vehicle.chassis_number} mono />
        <Field k={t("vehicles.rfid", "RFID / FASTag ID")} v={vehicle.rfid_fastag_id} mono />
        <Field
          k={t("vehicles.assignedDriver", "Assigned Driver")}
          v={
            vehicle.assigned_driver
              ? `${vehicle.assigned_driver.name ?? ""} (${vehicle.assigned_driver.driver_id})`
              : t("vehicles.unassigned", "Unassigned")
          }
        />
        <Field k={t("vehicles.createdBy", "Created By")} v={vehicle.created_by} />
        <Field
          k={t("vehicles.created", "Created")}
          v={vehicle.created_at ? fmtDateTimeIST(vehicle.created_at) : "—"}
        />
        <Field
          k={t("vehicles.updated", "Last Updated")}
          v={vehicle.updated_at ? fmtDateTimeIST(vehicle.updated_at) : "—"}
        />
      </dl>
      <div
        className="flex items-start gap-2 rounded-md border px-3 py-2 text-[11px]"
        style={{ borderColor: `${STATUS.info}80`, backgroundColor: `${STATUS.info}1a` }}
      >
        <Wrench className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        {t(
          "vehicles.detailNote",
          "Only ACTIVE vehicles with no active driver appear in the Driver Enrollment assignment dropdown.",
        )}
      </div>
    </div>
  );
}

function Labeled({
  label,
  required,
  className,
  children,
}: {
  label: string;
  required?: boolean;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <label className={`block ${className ?? ""}`}>
      <span className="mb-1 block text-[10px] uppercase tracking-wide text-muted-foreground">
        {label}
        {required && <span className="ml-0.5 text-red-500">*</span>}
      </span>
      {children}
    </label>
  );
}

function Field({ k, v, mono }: { k: string; v?: React.ReactNode; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-[10px] uppercase tracking-wide text-muted-foreground">{k}</dt>
      <dd className={`truncate text-foreground ${mono ? "font-mono" : ""}`}>{v || "—"}</dd>
    </div>
  );
}
