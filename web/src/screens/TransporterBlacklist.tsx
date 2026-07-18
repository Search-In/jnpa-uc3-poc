// Transporter Blacklist — admin console for the transporter registry and the
// gate-enforcement blacklist. Search transporters, drill into a transporter to
// see its mapped vehicles and blacklist history, add vehicles, blacklist / lift,
// and probe the live vehicle-validation gate (ALLOW / DENY) that the lane
// enforcement uses. Backed by /api/transporters/*.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, Plus, ShieldCheck, ShieldAlert, Search, Truck, ScanLine } from "lucide-react";
import { api } from "@/lib/api";
import { PageContainer, PageHeader, StatusChip, type Tone } from "@/components/ui/dtccc";
import { Card } from "@/components/ui/card";
import { EmptyState, LoadingState } from "@/components/ui/misc";
import { STATUS } from "@/lib/tokens";
import { fmtDateTimeIST } from "@/lib/utils";

const SEVERITIES = ["LOW", "MEDIUM", "HIGH", "CRITICAL"] as const;

function severityTone(sev?: string): Tone {
  switch ((sev ?? "").toUpperCase()) {
    case "CRITICAL":
    case "HIGH":
      return "critical";
    case "MEDIUM":
      return "warn";
    case "LOW":
      return "info";
    default:
      return "neutral";
  }
}

const inputCls =
  "w-full rounded-md border border-border bg-card px-2 py-1.5 text-sm outline-none focus:border-primary";

export default function TransporterBlacklist() {
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const listQ = useQuery({
    queryKey: ["transporters", q],
    queryFn: () => api.transporters({ q: q.trim() || undefined }),
  });
  const blacklistQ = useQuery({
    queryKey: ["transporter-blacklist"],
    queryFn: () => api.transporterBlacklist(),
  });
  const detailQ = useQuery({
    queryKey: ["transporter", selectedId],
    queryFn: () => api.transporter(selectedId as number),
    enabled: selectedId != null,
  });

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["transporters"] });
    void qc.invalidateQueries({ queryKey: ["transporter-blacklist"] });
    if (selectedId != null) void qc.invalidateQueries({ queryKey: ["transporter", selectedId] });
  };

  const transporters: any[] = listQ.data?.transporters ?? [];
  const blacklist: any[] = blacklistQ.data?.blacklist ?? [];

  return (
    <PageContainer>
      <PageHeader
        title="Transporter Blacklist"
        subtitle="Registry, gate-enforcement blacklist and live vehicle validation"
        icon={Ban}
      />

      <div className="grid grid-cols-1 gap-3 px-4 py-3 lg:grid-cols-2">
        {/* ---------------- Transporters table ---------------- */}
        <Card className="p-4">
          <div className="mb-3 flex items-center gap-2">
            <ShieldCheck size={15} />
            <h3 className="text-sm font-semibold">Transporters</h3>
            <span className="text-[11px] text-muted-foreground">
              ({listQ.data?.count ?? 0})
            </span>
          </div>

          <div className="mb-3 flex items-center gap-2 rounded-md border border-border bg-card px-2 py-1.5">
            <Search size={14} className="text-muted-foreground" />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search name / code / GSTIN…"
              className="w-full bg-transparent text-sm outline-none"
            />
          </div>

          {listQ.isLoading ? (
            <LoadingState />
          ) : !transporters.length ? (
            <EmptyState>No transporters match.</EmptyState>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[420px] border-collapse text-[12px]">
                <thead>
                  <tr className="text-left text-muted-foreground">
                    <th className="py-1 pr-3 font-medium">Name</th>
                    <th className="py-1 pr-3 font-medium">Code</th>
                    <th className="py-1 pr-3 font-medium">Status</th>
                    <th className="py-1 pr-3 font-medium">Vehicles</th>
                  </tr>
                </thead>
                <tbody>
                  {transporters.map((t) => {
                    const isBl = t.blacklisted || t.status === "BLACKLISTED";
                    return (
                      <tr
                        key={t.id}
                        onClick={() => setSelectedId(t.id)}
                        className={`cursor-pointer border-t border-border align-top hover:bg-muted/50 ${
                          selectedId === t.id ? "bg-muted/60" : ""
                        }`}
                      >
                        <td className="py-1.5 pr-3">
                          <div className="font-medium text-foreground">{t.name}</div>
                          <div className="font-mono text-[10px] text-muted-foreground">
                            {t.gstin || "—"}
                          </div>
                        </td>
                        <td className="py-1.5 pr-3 font-mono text-[11px]">{t.code || "—"}</td>
                        <td className="py-1.5 pr-3">
                          <StatusChip
                            label={isBl ? "BLACKLISTED" : t.status || "ACTIVE"}
                            tone={isBl ? "critical" : "ok"}
                          />
                        </td>
                        <td className="py-1.5 pr-3 tabular-nums">{t.vehicle_count ?? 0}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        {/* ---------------- Detail panel ---------------- */}
        <Card className="p-4">
          {selectedId == null ? (
            <div className="flex h-full min-h-[160px] items-center justify-center">
              <EmptyState>Select a transporter to view details.</EmptyState>
            </div>
          ) : detailQ.isLoading ? (
            <LoadingState />
          ) : !detailQ.data ? (
            <EmptyState>Transporter not found.</EmptyState>
          ) : (
            <TransporterDetail
              detail={detailQ.data}
              onChanged={invalidate}
            />
          )}
        </Card>

        {/* ---------------- Vehicle validation widget ---------------- */}
        <Card className="p-4">
          <div className="mb-3 flex items-center gap-2">
            <ScanLine size={15} />
            <h3 className="text-sm font-semibold">Vehicle validation</h3>
            <span className="text-[11px] text-muted-foreground">gate-enforcement check</span>
          </div>
          <VehicleValidation />
        </Card>

        {/* ---------------- Create transporter ---------------- */}
        <Card className="p-4">
          <div className="mb-3 flex items-center gap-2">
            <Plus size={15} />
            <h3 className="text-sm font-semibold">Create transporter</h3>
          </div>
          <CreateTransporter onCreated={invalidate} />
        </Card>

        {/* ---------------- Active blacklist ---------------- */}
        <Card className="p-4 lg:col-span-2">
          <div className="mb-3 flex items-center gap-2">
            <ShieldAlert size={15} />
            <h3 className="text-sm font-semibold">Active blacklist</h3>
            <span className="text-[11px] text-muted-foreground">
              ({blacklistQ.data?.count ?? 0})
            </span>
          </div>
          {blacklistQ.isLoading ? (
            <LoadingState />
          ) : !blacklist.length ? (
            <EmptyState>No transporters are currently blacklisted.</EmptyState>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[560px] border-collapse text-[12px]">
                <thead>
                  <tr className="text-left text-muted-foreground">
                    <th className="py-1 pr-3 font-medium">Transporter</th>
                    <th className="py-1 pr-3 font-medium">Code</th>
                    <th className="py-1 pr-3 font-medium">Severity</th>
                    <th className="py-1 pr-3 font-medium">Reason</th>
                    <th className="py-1 pr-3 font-medium">Since</th>
                  </tr>
                </thead>
                <tbody>
                  {blacklist.map((b, i) => (
                    <tr
                      key={`${b.transporter_id}-${i}`}
                      onClick={() => setSelectedId(b.transporter_id)}
                      className="cursor-pointer border-t border-border align-top hover:bg-muted/50"
                    >
                      <td className="py-1.5 pr-3 font-medium">{b.transporter_name}</td>
                      <td className="py-1.5 pr-3 font-mono text-[11px]">
                        {b.transporter_code || "—"}
                      </td>
                      <td className="py-1.5 pr-3">
                        <StatusChip label={b.severity} tone={severityTone(b.severity)} />
                      </td>
                      <td className="py-1.5 pr-3 text-muted-foreground">{b.reason || "—"}</td>
                      <td className="py-1.5 pr-3 whitespace-nowrap text-muted-foreground">
                        {b.blacklisted_at ? fmtDateTimeIST(b.blacklisted_at) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </PageContainer>
  );
}

/* ==================== Detail panel ==================== */

function TransporterDetail({
  detail,
  onChanged,
}: {
  detail: any;
  onChanged: () => void;
}) {
  const t = detail.transporter ?? {};
  const vehicles: any[] = detail.vehicles ?? [];
  const history: any[] = detail.blacklist_history ?? [];
  const isBlacklisted = t.blacklisted || t.status === "BLACKLISTED";

  const [vehNo, setVehNo] = useState("");
  const [driverId, setDriverId] = useState("");
  const [reason, setReason] = useState("");
  const [severity, setSeverity] = useState<(typeof SEVERITIES)[number]>("HIGH");

  const addVehicle = useMutation({
    mutationFn: () =>
      api.transporterAddVehicle(t.id, {
        vehicle_no: vehNo.trim(),
        driver_id: driverId.trim() || undefined,
      }),
    onSuccess: () => {
      setVehNo("");
      setDriverId("");
      onChanged();
    },
  });
  const blacklistAdd = useMutation({
    mutationFn: () =>
      api.transporterBlacklistAdd(t.id, { reason: reason.trim(), severity }),
    onSuccess: () => {
      setReason("");
      onChanged();
    },
  });
  const lift = useMutation({
    mutationFn: () => api.transporterLift(t.id, { actor: "admin" }),
    onSuccess: onChanged,
  });

  return (
    <div className="space-y-4 text-sm">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-base font-semibold">{t.name}</div>
          <div className="font-mono text-[11px] text-muted-foreground">
            {t.code || "—"} · {t.gstin || "no GSTIN"}
          </div>
        </div>
        <StatusChip
          label={isBlacklisted ? "BLACKLISTED" : t.status || "ACTIVE"}
          tone={isBlacklisted ? "critical" : "ok"}
        />
      </div>

      {/* Mapped vehicles */}
      <div>
        <div className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          <Truck size={13} /> Mapped vehicles ({vehicles.length})
        </div>
        {!vehicles.length ? (
          <div className="text-[12px] text-muted-foreground">No vehicles mapped.</div>
        ) : (
          <div className="space-y-1">
            {vehicles.map((v, i) => (
              <div
                key={`${v.vehicle_no_norm || v.vehicle_no}-${i}`}
                className="flex items-center justify-between rounded-md border border-border px-2 py-1 text-[12px]"
              >
                <span className="font-mono font-medium">{v.vehicle_no}</span>
                <span className="text-muted-foreground">
                  {v.driver_id ? `driver ${v.driver_id}` : "no driver"}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Add vehicle */}
      <div className="rounded-md border border-border p-2">
        <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          Add vehicle
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <input
            value={vehNo}
            onChange={(e) => setVehNo(e.target.value)}
            placeholder="Vehicle no (MH04AB1234)"
            className={`${inputCls} w-44 font-mono`}
          />
          <input
            value={driverId}
            onChange={(e) => setDriverId(e.target.value)}
            placeholder="Driver ID (optional)"
            className={`${inputCls} w-40`}
          />
          <button
            disabled={!vehNo.trim() || addVehicle.isPending}
            onClick={() => addVehicle.mutate()}
            className="rounded-md border border-border px-3 py-1.5 text-[12px] font-semibold hover:bg-muted disabled:opacity-50"
          >
            {addVehicle.isPending ? "Adding…" : "Add"}
          </button>
        </div>
        {addVehicle.isError && (
          <div className="mt-1 text-[11px]" style={{ color: STATUS.critical }}>
            {(addVehicle.error as Error)?.message}
          </div>
        )}
      </div>

      {/* Blacklist / Lift */}
      {isBlacklisted ? (
        <div
          className="rounded-md border p-2"
          style={{ borderColor: `${STATUS.critical}66`, backgroundColor: `${STATUS.critical}14` }}
        >
          <div className="mb-2 text-[12px]">
            This transporter is <strong>blacklisted</strong> — vehicles will be denied at the gate.
          </div>
          <button
            disabled={lift.isPending}
            onClick={() => lift.mutate()}
            className="rounded-md bg-primary px-3 py-1.5 text-[12px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {lift.isPending ? "Lifting…" : "Lift blacklist"}
          </button>
          {lift.isError && (
            <div className="mt-1 text-[11px]" style={{ color: STATUS.critical }}>
              {(lift.error as Error)?.message}
            </div>
          )}
        </div>
      ) : (
        <div className="rounded-md border border-border p-2">
          <div className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            <Ban size={13} /> Blacklist transporter
          </div>
          <div className="space-y-2">
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Reason (e.g. repeated e-Challan violations)"
              className={inputCls}
            />
            <div className="flex items-center gap-2">
              <select
                value={severity}
                onChange={(e) => setSeverity(e.target.value as (typeof SEVERITIES)[number])}
                className={`${inputCls} w-36`}
              >
                {SEVERITIES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
              <button
                disabled={!reason.trim() || blacklistAdd.isPending}
                onClick={() => blacklistAdd.mutate()}
                className="rounded-md px-3 py-1.5 text-[12px] font-semibold text-white disabled:opacity-50"
                style={{ backgroundColor: STATUS.critical }}
              >
                {blacklistAdd.isPending ? "Blacklisting…" : "Blacklist"}
              </button>
            </div>
          </div>
          {blacklistAdd.isError && (
            <div className="mt-1 text-[11px]" style={{ color: STATUS.critical }}>
              {(blacklistAdd.error as Error)?.message}
            </div>
          )}
        </div>
      )}

      {/* Blacklist history */}
      <div>
        <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          Blacklist history ({history.length})
        </div>
        {!history.length ? (
          <div className="text-[12px] text-muted-foreground">No blacklist events.</div>
        ) : (
          <div className="space-y-1">
            {history.map((h, i) => (
              <div
                key={i}
                className="flex flex-wrap items-center gap-2 rounded-md border border-border px-2 py-1 text-[11px]"
              >
                <StatusChip label={h.severity || h.action || "—"} tone={severityTone(h.severity)} />
                <span>{h.reason || h.action || "—"}</span>
                <span className="ml-auto text-muted-foreground">
                  {h.blacklisted_at || h.created_at || h.lifted_at
                    ? fmtDateTimeIST(h.blacklisted_at || h.created_at || h.lifted_at)
                    : ""}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/* ==================== Vehicle validation widget ==================== */

function VehicleValidation() {
  const [plate, setPlate] = useState("");
  const check = useMutation({
    mutationFn: (p: string) => api.validateVehicle(p.trim()),
  });
  const res: any = check.data;
  const deny = res?.decision === "DENY" || res?.blacklisted;

  return (
    <div className="space-y-3 text-sm">
      <div className="flex items-center gap-2">
        <input
          value={plate}
          onChange={(e) => setPlate(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && plate.trim() && check.mutate(plate)}
          placeholder="Plate (MH04AB1234)"
          className={`${inputCls} font-mono`}
        />
        <button
          disabled={!plate.trim() || check.isPending}
          onClick={() => check.mutate(plate)}
          className="rounded-md bg-primary px-3 py-1.5 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        >
          {check.isPending ? "Checking…" : "Validate"}
        </button>
      </div>

      {check.isError && (
        <div className="text-[11px]" style={{ color: STATUS.critical }}>
          {(check.error as Error)?.message}
        </div>
      )}

      {res && (
        <div
          className="rounded-md border p-3"
          style={{
            borderColor: `${deny ? STATUS.critical : STATUS.ok}66`,
            backgroundColor: `${deny ? STATUS.critical : STATUS.ok}14`,
          }}
        >
          <div className="flex items-center gap-2">
            <span
              className="rounded px-2 py-0.5 text-[13px] font-bold text-white"
              style={{ backgroundColor: deny ? STATUS.critical : STATUS.ok }}
            >
              {res.decision || (deny ? "DENY" : "ALLOW")}
            </span>
            <span className="font-mono text-[12px]">{res.plate}</span>
          </div>
          <div className="mt-2 space-y-0.5 text-[12px]">
            {res.transporter_name && (
              <div>
                <span className="text-muted-foreground">Transporter: </span>
                {res.transporter_name}
              </div>
            )}
            {res.severity && (
              <div>
                <span className="text-muted-foreground">Severity: </span>
                {res.severity}
              </div>
            )}
            {res.reason && (
              <div>
                <span className="text-muted-foreground">Reason: </span>
                {res.reason}
              </div>
            )}
            {!deny && !res.reason && (
              <div className="text-muted-foreground">No active blacklist — vehicle may enter.</div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/* ==================== Create transporter ==================== */

function CreateTransporter({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [code, setCode] = useState("");
  const [gstin, setGstin] = useState("");

  const create = useMutation({
    mutationFn: () =>
      api.transporterCreate({
        name: name.trim(),
        code: code.trim() || undefined,
        gstin: gstin.trim() || undefined,
      }),
    onSuccess: () => {
      setName("");
      setCode("");
      setGstin("");
      onCreated();
    },
  });

  return (
    <div className="space-y-2 text-sm">
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="Transporter name"
        className={inputCls}
      />
      <div className="flex items-center gap-2">
        <input
          value={code}
          onChange={(e) => setCode(e.target.value)}
          placeholder="Code"
          className={`${inputCls} font-mono`}
        />
        <input
          value={gstin}
          onChange={(e) => setGstin(e.target.value)}
          placeholder="GSTIN"
          className={`${inputCls} font-mono`}
        />
      </div>
      <button
        disabled={!name.trim() || create.isPending}
        onClick={() => create.mutate()}
        className="rounded-md bg-primary px-3 py-1.5 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
      >
        {create.isPending ? "Creating…" : "Create transporter"}
      </button>
      {create.isError && (
        <div className="text-[11px]" style={{ color: STATUS.critical }}>
          {(create.error as Error)?.message}
        </div>
      )}
    </div>
  );
}
