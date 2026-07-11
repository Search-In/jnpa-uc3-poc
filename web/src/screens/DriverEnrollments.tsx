// Driver Enrolment — admin approval console (Identity / C2), redesigned onto the
// DTCCC kit for consistency. Reviews the PENDING queue submitted from the Driver
// PWA, inspects captured reference frames + profile, and approves / rejects /
// requests re-enrolment. On approval the gateway mints the face template and
// stores the reference photo; every action is DPDP-audited. RDS-backed via
// /api/identity (getAdapter().enrollments / enrollmentDetail / approve / reject /
// reenroll) — query keys and endpoints UNCHANGED. Restricted to CUSTOMS / ADMIN.

import { useMemo, useState } from "react";
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
} from "lucide-react";
import { getAdapter } from "@/data";
import type { DriverEnrollment } from "@/lib/types";
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
        title={t("enrollments.title", "Driver Enrolment Requests")}
        subtitle={t(
          "enrollments.subtitle",
          "Review and approve driver face enrolments submitted from the mobile app (DPDP-audited).",
        )}
        updatedAt={listQ.dataUpdatedAt}
        isFetching={listQ.isFetching && !listQ.isLoading}
        onRefresh={invalidate}
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
            emptyLabel={t("enrollments.empty", "No enrolment requests in this view.")}
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
            <DialogTitle>{t("enrollments.review", "Review enrolment")}</DialogTitle>
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
    </PageContainer>
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
              <RefreshCw className="h-4 w-4" /> {t("enrollments.reenroll", "Request re-enrolment")}
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
