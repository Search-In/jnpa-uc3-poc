import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, X, RefreshCw, Eye, UserCheck, ShieldCheck } from "lucide-react";
import { getAdapter } from "@/data";
import type { DriverEnrollment } from "@/lib/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Spinner, EmptyState } from "@/components/ui/misc";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { STATUS } from "@/lib/tokens";
import { fmtDateTimeIST } from "@/lib/utils";

// Admin: Driver Enrolment Requests (Identity / C2). Reviews the PENDING queue
// submitted from the Driver PWA, lets an admin inspect the captured reference
// frames + profile, and approve / reject / request re-enrolment. On approval the
// gateway mints the face template (identity service) and stores the reference
// photo (MinIO), after which the driver is verifiable at the gate. Restricted to
// CUSTOMS / DTCCC_ADMIN (mirrors the /api/identity gateway policy).

const FILTERS = ["PENDING", "ACTIVE", "REJECTED", "ALL"] as const;
type Filter = (typeof FILTERS)[number];

function statusColour(status?: string): string {
  switch ((status ?? "").toUpperCase()) {
    case "ACTIVE":
      return STATUS.ok;
    case "PENDING":
      return STATUS.warning;
    case "REJECTED":
      return STATUS.critical;
    case "REENROLL":
      return STATUS.info;
    default:
      return STATUS.unknown;
  }
}

export default function DriverEnrollments() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [filter, setFilter] = useState<Filter>("PENDING");
  const [openId, setOpenId] = useState<string | null>(null);

  const listQ = useQuery({
    queryKey: ["enrollments", filter],
    queryFn: () => getAdapter().enrollments(filter === "ALL" ? undefined : filter),
    refetchInterval: 8000,
  });
  const rows = listQ.data ?? [];

  function invalidate() {
    void qc.invalidateQueries({ queryKey: ["enrollments"] });
  }

  return (
    <div className="h-full overflow-y-auto p-4">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle className="flex items-center gap-2">
              <ShieldCheck className="h-4 w-4" />
              {t("enrollments.title", "Driver Enrolment Requests")}
            </CardTitle>
            <p className="text-[11px] text-muted-foreground">
              {t(
                "enrollments.subtitle",
                "Review and approve driver face enrolments submitted from the mobile app (DPDP-audited).",
              )}
            </p>
          </div>
          <div className="flex gap-1">
            {FILTERS.map((f) => (
              <Button
                key={f}
                size="sm"
                variant={filter === f ? "default" : "outline"}
                onClick={() => setFilter(f)}
              >
                {f}
              </Button>
            ))}
          </div>
        </CardHeader>
        <CardContent>
          {listQ.isLoading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Spinner /> {t("common.loading", "Loading…")}
            </div>
          ) : rows.length === 0 ? (
            <EmptyState>
              {t("enrollments.empty", "No enrolment requests in this view.")}
            </EmptyState>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border text-left text-[10px] uppercase tracking-wide text-muted-foreground">
                    <th className="py-2 pr-2">{t("enrollments.photo", "Photo")}</th>
                    <th className="py-2 pr-2">{t("enrollments.driver", "Driver")}</th>
                    <th className="py-2 pr-2">{t("enrollments.license", "Licence")}</th>
                    <th className="py-2 pr-2">{t("enrollments.submitted", "Submitted")}</th>
                    <th className="py-2 pr-2">{t("enrollments.status", "Status")}</th>
                    <th className="py-2 pr-2 text-right">{t("enrollments.actions", "Actions")}</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((e) => (
                    <EnrollmentRow
                      key={e.driver_id}
                      row={e}
                      onView={() => setOpenId(e.driver_id)}
                      onChanged={invalidate}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

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
    </div>
  );
}

function EnrollmentRow({
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

  return (
    <tr className="border-b border-border/60 align-middle">
      <td className="py-2 pr-2">
        {row.photo ? (
          <img
            src={row.photo}
            alt={row.name}
            className="h-9 w-9 rounded-full object-cover"
          />
        ) : (
          <div className="flex h-9 w-9 items-center justify-center rounded-full bg-muted">
            <UserCheck className="h-4 w-4 text-muted-foreground" />
          </div>
        )}
      </td>
      <td className="py-2 pr-2">
        <div className="font-medium text-foreground">{row.name}</div>
        <div className="font-mono text-[10px] text-muted-foreground">{row.driver_id}</div>
      </td>
      <td className="py-2 pr-2 font-mono">{row.license_no || "—"}</td>
      <td className="py-2 pr-2 text-muted-foreground">
        {row.submitted_at ? fmtDateTimeIST(row.submitted_at) : "—"}
      </td>
      <td className="py-2 pr-2">
        <Badge colour={statusColour(row.status)}>{row.status}</Badge>
      </td>
      <td className="py-2 pr-2">
        <div className="flex justify-end gap-1">
          <Button size="sm" variant="outline" onClick={onView}>
            <Eye className="h-3.5 w-3.5" /> {t("enrollments.view", "View")}
          </Button>
          {pending && (
            <>
              <Button
                size="sm"
                onClick={() => actions.approve.mutate()}
                disabled={actions.busy}
              >
                {actions.approve.isPending ? <Spinner /> : <Check className="h-3.5 w-3.5" />}
                {t("enrollments.approve", "Approve")}
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  const reason = window.prompt(t("enrollments.rejectReason", "Reason for rejection?") ?? "");
                  if (reason !== null) actions.reject.mutate(reason);
                }}
                disabled={actions.busy}
              >
                <X className="h-3.5 w-3.5" /> {t("enrollments.reject", "Reject")}
              </Button>
            </>
          )}
        </div>
      </td>
    </tr>
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

  if (detailQ.isLoading || !rec) {
    return (
      <div className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
        <Spinner /> {t("common.loading", "Loading…")}
      </div>
    );
  }

  const pending = (rec.status ?? "").toUpperCase() === "PENDING";

  return (
    <div className="space-y-4 p-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-sm font-semibold">{rec.name}</div>
          <div className="font-mono text-[11px] text-muted-foreground">{rec.driver_id}</div>
        </div>
        <Badge colour={statusColour(rec.status)}>{rec.status}</Badge>
      </div>

      {/* Captured reference frames */}
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
                alt={`frame ${i + 1}`}
                className="h-24 w-24 rounded-lg border border-border object-cover"
              />
            ))}
          </div>
        ) : rec.photo ? (
          <img
            src={rec.photo}
            alt="reference"
            className="h-24 w-24 rounded-lg border border-border object-cover"
          />
        ) : (
          <p className="text-[11px] text-muted-foreground">
            {t("enrollments.noFrames", "No frames retained (already approved).")}
          </p>
        )}
      </div>

      {/* Profile */}
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

      {/* DPDP note */}
      <div
        className="rounded-md border px-3 py-2 text-[11px]"
        style={{ borderColor: `${STATUS.info}80`, backgroundColor: `${STATUS.info}1a` }}
      >
        {t(
          "enrollments.dpdpNote",
          "Approving mints a face template and stores the reference photo. Every action is DPDP-audited (actor + timestamp).",
        )}
      </div>

      <div className="flex flex-wrap justify-end gap-2">
        {pending ? (
          <>
            <Button onClick={() => actions.approve.mutate()} disabled={actions.busy}>
              {actions.approve.isPending ? <Spinner /> : <Check className="h-4 w-4" />}
              {t("enrollments.approve", "Approve")}
            </Button>
            <Button
              variant="outline"
              onClick={() => {
                const reason = window.prompt(t("enrollments.rejectReason", "Reason for rejection?") ?? "");
                if (reason !== null) actions.reject.mutate(reason);
              }}
              disabled={actions.busy}
            >
              <X className="h-4 w-4" /> {t("enrollments.reject", "Reject")}
            </Button>
            <Button
              variant="outline"
              onClick={() => actions.reenroll.mutate()}
              disabled={actions.busy}
            >
              <RefreshCw className="h-4 w-4" /> {t("enrollments.reenroll", "Request re-enrolment")}
            </Button>
          </>
        ) : (
          <Button variant="outline" onClick={onClose}>
            {t("common.close", "Close")}
          </Button>
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
  return {
    approve,
    reject,
    reenroll,
    busy: approve.isPending || reject.isPending || reenroll.isPending,
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
