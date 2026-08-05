// Driver Enrollment — admin approval console (Identity / C2), redesigned onto the
// DTCCC kit for consistency. Reviews the PENDING queue submitted from the Driver
// PWA, inspects captured reference frames + profile, and approves / rejects /
// requests re-enrollment. On approval the gateway mints the face template and
// stores the reference photo; every action is DPDP-audited. RDS-backed via
// /api/identity (getAdapter().enrollments / enrollmentDetail / approve / reject /
// reenroll) — query keys and endpoints UNCHANGED. Restricted to CUSTOMS / ADMIN.

import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  X,
  RefreshCw,
  Eye,
  UserCheck,
  UserPlus,
  Clock,
  ShieldCheck,
  Ban,
  Plus,
  Truck,
  Search,
} from "lucide-react";
import { getAdapter } from "@/data";
import type { AvailableVehicle, DriverEnrollment } from "@/lib/types";
import { Card } from "@/components/ui/card";
import { Spinner, ErrorState } from "@/components/ui/misc";
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

const FILTERS = ["PENDING", "ACTIVE", "REJECTED", "ALL"] as const;
type Filter = (typeof FILTERS)[number];

function statusTone(status?: string): Tone {
  switch ((status ?? "").toUpperCase()) {
    case "ACTIVE":
      return "ok";
    case "PENDING":
      return "warn";
    case "REJECTED":
      return "critical";
    case "REENROLL":
      return "info";
    default:
      return "neutral";
  }
}

export default function DriverEnrollments() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [filter, setFilter] = useState<Filter>("PENDING");
  const [openId, setOpenId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  // Bridge from Driver Master: /enrollments?create=1&name=&license= opens the
  // existing admin create-profile form pre-filled (reuses POST /api/identity/drivers;
  // no new API/flow). The admin then assigns a Vehicle ID and submits as usual.
  const [searchParams, setSearchParams] = useSearchParams();
  const [prefill, setPrefill] = useState<{ name: string; license: string } | null>(null);
  useEffect(() => {
    if (searchParams.get("create") === "1") {
      setPrefill({
        name: searchParams.get("name") || "",
        license: searchParams.get("license") || "",
      });
      setCreateOpen(true);
      const next = new URLSearchParams(searchParams);
      ["create", "name", "license"].forEach((k) => next.delete(k));
      setSearchParams(next, { replace: true });
    }
  }, [searchParams, setSearchParams]);

  // Fetch the full set once (small table); filter client-side for the tabs so
  // per-status counts stay live. Key stays under the ["enrollments"] prefix that
  // the mutations invalidate.
  const listQ = useQuery({
    queryKey: ["enrollments"],
    queryFn: () => getAdapter().enrollments(),
  });
  const all = listQ.data ?? [];

  const counts = useMemo(() => {
    const c = { PENDING: 0, ACTIVE: 0, REJECTED: 0, ALL: all.length } as Record<Filter, number>;
    for (const e of all) {
      const s = (e.status ?? "").toUpperCase();
      if (s === "PENDING") c.PENDING++;
      else if (s === "ACTIVE") c.ACTIVE++;
      else if (s === "REJECTED") c.REJECTED++;
    }
    return c;
  }, [all]);

  const rows = useMemo(
    () => (filter === "ALL" ? all : all.filter((e) => (e.status ?? "").toUpperCase() === filter)),
    [all, filter],
  );

  const invalidate = () => void qc.invalidateQueries({ queryKey: ["enrollments"] });

  const columns: Column<DriverEnrollment>[] = [
    {
      key: "photo",
      header: "",
      render: (e) =>
        e.photo ? (
          <img src={e.photo} alt={e.name} className="h-9 w-9 rounded-full object-cover" />
        ) : (
          <div className="flex h-9 w-9 items-center justify-center rounded-full bg-muted">
            <UserCheck className="h-4 w-4 text-muted-foreground" />
          </div>
        ),
    },
    {
      key: "driver",
      header: t("enrollments.driver", "Driver"),
      render: (e) => (
        <div>
          <div className="font-medium text-foreground">{e.name}</div>
          <div className="font-mono text-[10px] text-muted-foreground">{e.driver_id}</div>
        </div>
      ),
    },
    {
      key: "license",
      header: t("enrollments.license", "Licence"),
      className: "font-mono",
      render: (e) => e.license_no || "—",
    },
    {
      key: "submitted",
      header: t("enrollments.submitted", "Submitted"),
      className: "text-muted-foreground",
      render: (e) => (e.submitted_at ? fmtDateTimeIST(e.submitted_at) : "—"),
    },
    {
      key: "status",
      header: t("enrollments.status", "Status"),
      render: (e) => <StatusChip label={e.status} tone={statusTone(e.status)} />,
    },
    {
      key: "actions",
      header: t("enrollments.actions", "Actions"),
      align: "right",
      render: (e) => (
        <RowActions row={e} onView={() => setOpenId(e.driver_id)} onChanged={invalidate} />
      ),
    },
  ];

  return (
    <PageContainer>
      <PageHeader
        icon={UserPlus}
        title={t("enrollments.title", "Driver Enrollment Requests")}
        subtitle={t(
          "enrollments.subtitle",
          "Review and approve driver face enrollments submitted from the mobile app (DPDP-audited).",
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
            {t("enrollments.create", "Create Driver Profile")}
          </button>
        }
      />

      <div className="px-4 pt-3">
        <StatGrid className="lg:grid-cols-4">
          <StatCard
            icon={Clock}
            label="Pending Review"
            value={counts.PENDING}
            tone={counts.PENDING > 0 ? "warn" : "ok"}
            loading={listQ.isLoading}
          />
          <StatCard
            icon={ShieldCheck}
            label="Active (Enrolled)"
            value={counts.ACTIVE}
            tone="ok"
            loading={listQ.isLoading}
          />
          <StatCard
            icon={Ban}
            label="Rejected"
            value={counts.REJECTED}
            tone={counts.REJECTED > 0 ? "critical" : "ok"}
            loading={listQ.isLoading}
          />
          <StatCard
            icon={UserPlus}
            label="Total Requests"
            value={counts.ALL}
            tone="info"
            loading={listQ.isLoading}
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
            rowKey={(e) => e.driver_id}
            status={listQ}
            onRetry={() => listQ.refetch()}
            emptyLabel={t("enrollments.empty", "No enrollment requests in this view.")}
            search={(e, q) =>
              `${e.name} ${e.driver_id} ${e.license_no ?? ""} ${e.vehicle_no ?? ""}`
                .toLowerCase()
                .includes(q)
            }
            searchPlaceholder="Search driver / licence / vehicle…"
            pageSize={10}
          />
        </Card>
      </div>

      <Dialog open={!!openId} onOpenChange={(o) => !o && setOpenId(null)}>
        <DialogContent className="p-0">
          <DialogHeader>
            <DialogTitle>{t("enrollments.review", "Review enrollment")}</DialogTitle>
          </DialogHeader>
          {openId && (
            <EnrollmentDetail
              driverId={openId}
              onClose={() => setOpenId(null)}
              onChanged={invalidate}
            />
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="p-0">
          <DialogHeader>
            <DialogTitle>{t("enrollments.create", "Create Driver Profile")}</DialogTitle>
          </DialogHeader>
          {createOpen && (
            <CreateDriverForm
              initialName={prefill?.name}
              initialLicense={prefill?.license}
              onClose={() => setCreateOpen(false)}
              onCreated={() => {
                invalidate();
                setFilter("PENDING");
                setCreateOpen(false);
              }}
            />
          )}
        </DialogContent>
      </Dialog>
    </PageContainer>
  );
}

// Admin-originated driver-profile creation: capture the profile + assign an
// available Vehicle ID (searchable dropdown — no free typing), then POST to
// /api/identity/drivers which creates a PENDING enrollment (source=ADMIN). The
// driver flows through the SAME approve action; on approval the Vehicle ID
// becomes eligible for PWA login.
function CreateDriverForm({
  onClose,
  onCreated,
  initialName,
  initialLicense,
}: {
  onClose: () => void;
  onCreated: () => void;
  initialName?: string;
  initialLicense?: string;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(initialName || "");
  const [license, setLicense] = useState(initialLicense || "");
  const [mobile, setMobile] = useState("");
  const [emergency, setEmergency] = useState("");
  const [vehicle, setVehicle] = useState<string>("");
  const [vehicleQuery, setVehicleQuery] = useState("");
  const [dropdownOpen, setDropdownOpen] = useState(false);

  const vehiclesQ = useQuery({
    queryKey: ["available-vehicles", vehicleQuery],
    queryFn: () => getAdapter().availableVehicles(vehicleQuery || undefined, 50),
  });

  const create = useMutation({
    mutationFn: () =>
      getAdapter().createDriverProfile({
        name: name.trim(),
        vehicle_no: vehicle,
        license_no: license.trim() || undefined,
        mobile: mobile.trim() || undefined,
        emergency_contact: emergency.trim() || undefined,
      }),
    onSuccess: onCreated,
  });

  const canSubmit = name.trim().length > 0 && /^TRK-\d{6}$/.test(vehicle) && !create.isPending;
  const inputCls =
    "w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm outline-none focus:border-primary";

  return (
    <div className="space-y-3 p-4">
      <div className="grid grid-cols-2 gap-3">
        <Labeled label={t("enrollments.name", "Driver Name")} required className="col-span-2">
          <input
            className={inputCls}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Ramesh Kumar"
            autoFocus
          />
        </Labeled>
        <Labeled label={t("enrollments.license", "License Number")}>
          <input
            className={inputCls}
            value={license}
            onChange={(e) => setLicense(e.target.value)}
          />
        </Labeled>
        <Labeled label={t("enrollments.mobile", "Mobile Number")}>
          <input
            className={inputCls}
            value={mobile}
            onChange={(e) => setMobile(e.target.value)}
            placeholder="+91 …"
          />
        </Labeled>
        <Labeled label={t("enrollments.emergency", "Emergency Contact")} className="col-span-2">
          <input
            className={inputCls}
            value={emergency}
            onChange={(e) => setEmergency(e.target.value)}
          />
        </Labeled>

        {/* Vehicle assignment — searchable dropdown of AVAILABLE vehicles only. */}
        <Labeled
          label={t("enrollments.assignVehicle", "Assign Vehicle")}
          required
          className="col-span-2"
        >
          <div className="relative">
            <div className="flex items-center gap-2 rounded-md border border-border bg-background px-2.5 py-1.5">
              {vehicle ? (
                <Truck className="h-4 w-4 text-primary" />
              ) : (
                <Search className="h-4 w-4 text-muted-foreground" />
              )}
              <input
                className="flex-1 bg-transparent text-sm outline-none"
                value={dropdownOpen ? vehicleQuery : vehicle || vehicleQuery}
                onFocus={() => setDropdownOpen(true)}
                onChange={(e) => {
                  setVehicleQuery(e.target.value);
                  setDropdownOpen(true);
                  if (vehicle) setVehicle("");
                }}
                placeholder={t("enrollments.searchVehicle", "Search available Vehicle ID…")}
              />
              {vehicle && (
                <button
                  type="button"
                  onClick={() => {
                    setVehicle("");
                    setVehicleQuery("");
                  }}
                  className="text-muted-foreground hover:text-foreground"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
            {dropdownOpen && (
              <div className="absolute z-10 mt-1 max-h-52 w-full overflow-auto rounded-md border border-border bg-card shadow-lg">
                {vehiclesQ.isLoading ? (
                  <div className="flex items-center gap-2 p-3 text-xs text-muted-foreground">
                    <Spinner /> {t("common.loading", "Loading…")}
                  </div>
                ) : (vehiclesQ.data ?? []).length === 0 ? (
                  <div className="p-3 text-xs text-muted-foreground">
                    {t("enrollments.noVehicles", "No available vehicles match.")}
                  </div>
                ) : (
                  (vehiclesQ.data ?? []).map((v: AvailableVehicle) => (
                    <button
                      key={v.vehicle_id}
                      type="button"
                      onClick={() => {
                        setVehicle(v.vehicle_id);
                        setVehicleQuery("");
                        setDropdownOpen(false);
                      }}
                      className="flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left text-sm hover:bg-muted"
                    >
                      <span className="font-mono">{v.vehicle_id}</span>
                      {v.plate && (
                        <span className="text-[10px] text-muted-foreground">{v.plate}</span>
                      )}
                    </button>
                  ))
                )}
              </div>
            )}
          </div>
        </Labeled>
      </div>

      <div
        className="rounded-md border px-3 py-2 text-[11px]"
        style={{ borderColor: `${STATUS.info}80`, backgroundColor: `${STATUS.info}1a` }}
      >
        {t(
          "enrollments.createNote",
          "Creates a PENDING profile. After you approve it, the assigned Vehicle ID becomes eligible for Driver PWA login.",
        )}
      </div>

      {create.error && (
        <div className="text-xs text-red-500">{(create.error as Error).message}</div>
      )}

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
          onClick={() => create.mutate()}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        >
          {create.isPending ? (
            <Spinner className="text-primary-foreground" />
          ) : (
            <Plus className="h-4 w-4" />
          )}
          {t("enrollments.createSubmit", "Create Profile")}
        </button>
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

function RowActions({
  row,
  onView,
  onChanged,
}: {
  row: DriverEnrollment;
  onView: () => void;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const actions = useEnrollmentActions(row.driver_id, onChanged);
  const pending = (row.status ?? "").toUpperCase() === "PENDING";
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
        <Eye className="h-3.5 w-3.5" /> {t("enrollments.view", "View")}
      </button>
      {pending && (
        <>
          <button
            onClick={stop(() => actions.approve.mutate())}
            disabled={actions.busy}
            className="inline-flex items-center gap-1 rounded-md bg-primary px-2 py-1 text-[11px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {actions.approve.isPending ? (
              <Spinner className="text-primary-foreground" />
            ) : (
              <Check className="h-3.5 w-3.5" />
            )}{" "}
            {t("enrollments.approve", "Approve")}
          </button>
          <button
            onClick={stop(() => {
              const reason = window.prompt(
                t("enrollments.rejectReason", "Reason for rejection?") ?? "",
              );
              if (reason !== null) actions.reject.mutate(reason);
            })}
            disabled={actions.busy}
            className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] font-medium hover:bg-muted disabled:opacity-50"
          >
            <X className="h-3.5 w-3.5" /> {t("enrollments.reject", "Reject")}
          </button>
        </>
      )}
    </div>
  );
}

function EnrollmentDetail({
  driverId,
  onClose,
  onChanged,
}: {
  driverId: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const detailQ = useQuery({
    queryKey: ["enrollment-detail", driverId],
    queryFn: () => getAdapter().enrollmentDetail(driverId),
  });
  const rec = detailQ.data;
  const actions = useEnrollmentActions(driverId, () => {
    onChanged();
    onClose();
  });

  if (detailQ.isError)
    return (
      <div className="p-4">
        <ErrorState onRetry={() => detailQ.refetch()} detail={(detailQ.error as Error)?.message} />
      </div>
    );
  if (detailQ.isLoading || !rec)
    return (
      <div className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
        <Spinner /> {t("common.loading", "Loading…")}
      </div>
    );

  const pending = (rec.status ?? "").toUpperCase() === "PENDING";
  return (
    <div className="space-y-4 p-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-sm font-semibold">{rec.name}</div>
          <div className="font-mono text-[11px] text-muted-foreground">{rec.driver_id}</div>
        </div>
        <StatusChip label={rec.status} tone={statusTone(rec.status)} />
      </div>

      <div>
        <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
          {t("enrollments.faces", "Reference frames")}
        </div>
        {rec.face_images && rec.face_images.length > 0 ? (
          <div className="flex flex-wrap gap-2">
            {rec.face_images.map((src, i) => (
              <img
                key={i}
                src={src}
                alt={`${t("enrollments.frame", "Frame")} ${i + 1}`}
                className="h-24 w-24 rounded-lg border border-border object-cover"
              />
            ))}
          </div>
        ) : rec.photo ? (
          <img
            src={rec.photo}
            alt={t("enrollments.referenceAlt", "Reference")}
            className="h-24 w-24 rounded-lg border border-border object-cover"
          />
        ) : (
          <p className="text-[11px] text-muted-foreground">
            {t("enrollments.noFrames", "No frames retained (already approved).")}
          </p>
        )}
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
        <Field k={t("enrollments.license", "Licence")} v={rec.license_no} mono />
        <Field k={t("enrollments.mobile", "Mobile")} v={rec.mobile} />
        <Field k={t("enrollments.vehicle", "Vehicle")} v={rec.vehicle_no} mono />
        <Field k={t("enrollments.aadhaar", "Aadhaar / ID")} v={rec.aadhaar_masked} mono />
        <Field k={t("enrollments.emergency", "Emergency contact")} v={rec.emergency_contact} />
        <Field
          k={t("enrollments.consent", "Consent")}
          v={rec.consent ? `✓ ${rec.consent_at ? fmtDateTimeIST(rec.consent_at) : ""}` : "✗"}
        />
        <Field
          k={t("enrollments.submitted", "Submitted")}
          v={rec.submitted_at ? fmtDateTimeIST(rec.submitted_at) : "—"}
        />
        <Field
          k={t("enrollments.source", "Source")}
          v={
            (rec.source ?? "").toUpperCase() === "ADMIN"
              ? t("enrollments.sourceAdmin", "Admin-created")
              : t("enrollments.sourcePwa", "Driver app")
          }
        />
        {rec.created_by && (
          <Field k={t("enrollments.createdBy", "Created by")} v={rec.created_by} />
        )}
        {rec.reviewed_by && (
          <Field k={t("enrollments.reviewedBy", "Reviewed by")} v={rec.reviewed_by} />
        )}
      </dl>

      {rec.rejection_reason && (
        <div
          className="rounded-md border px-3 py-2 text-[11px]"
          style={{ borderColor: `${STATUS.critical}80`, backgroundColor: `${STATUS.critical}1a` }}
        >
          {t("enrollments.reason", "Reason")}: {rec.rejection_reason}
        </div>
      )}

      <div
        className="rounded-md border px-3 py-2 text-[11px]"
        style={{ borderColor: `${STATUS.info}80`, backgroundColor: `${STATUS.info}1a` }}
      >
        {t(
          "enrollments.dpdpNote",
          "Approving mints a face template and stores the reference photo. Every action is DPDP-audited (actor + timestamp).",
        )}
      </div>

      {actions.error && (
        <div className="text-right text-xs text-red-500" title={actions.error}>
          {actions.error}
        </div>
      )}

      <div className="flex flex-wrap justify-end gap-2">
        {pending ? (
          <>
            <button
              onClick={() => actions.approve.mutate()}
              disabled={actions.busy}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {actions.approve.isPending ? (
                <Spinner className="text-primary-foreground" />
              ) : (
                <Check className="h-4 w-4" />
              )}{" "}
              {t("enrollments.approve", "Approve")}
            </button>
            <button
              onClick={() => {
                const reason = window.prompt(
                  t("enrollments.rejectReason", "Reason for rejection?") ?? "",
                );
                if (reason !== null) actions.reject.mutate(reason);
              }}
              disabled={actions.busy}
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-[13px] font-medium hover:bg-muted disabled:opacity-50"
            >
              <X className="h-4 w-4" /> {t("enrollments.reject", "Reject")}
            </button>
            <button
              onClick={() => actions.reenroll.mutate()}
              disabled={actions.busy}
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-[13px] font-medium hover:bg-muted disabled:opacity-50"
            >
              <RefreshCw className="h-4 w-4" /> {t("enrollments.reenroll", "Request re-enrollment")}
            </button>
          </>
        ) : (
          <button
            onClick={onClose}
            className="rounded-md border border-border px-3 py-1.5 text-[13px] font-medium hover:bg-muted"
          >
            {t("common.close", "Close")}
          </button>
        )}
      </div>
    </div>
  );
}

function useEnrollmentActions(driverId: string, onDone: () => void) {
  const approve = useMutation({
    mutationFn: () => getAdapter().approveEnrollment(driverId),
    onSuccess: onDone,
  });
  const reject = useMutation({
    mutationFn: (reason: string) => getAdapter().rejectEnrollment(driverId, reason),
    onSuccess: onDone,
  });
  const reenroll = useMutation({
    mutationFn: () => getAdapter().reenrollEnrollment(driverId),
    onSuccess: onDone,
  });
  const error =
    (approve.error as Error)?.message ??
    (reject.error as Error)?.message ??
    (reenroll.error as Error)?.message ??
    null;
  return {
    approve,
    reject,
    reenroll,
    busy: approve.isPending || reject.isPending || reenroll.isPending,
    error,
  };
}

function Field({ k, v, mono }: { k: string; v?: React.ReactNode; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-[10px] uppercase tracking-wide text-muted-foreground">{k}</dt>
      <dd className={`truncate text-foreground ${mono ? "font-mono" : ""}`}>{v || "—"}</dd>
    </div>
  );
}
