// SecureVision face-recognition panels.
//
// Population boundary, stated once and enforced by where these panels live:
//
//   /api/identity  (UNTOUCHED)  = DRIVERS — enrolment approval, gate verification
//   /api/sv/faces               = SITE PERSONNEL — people authorised inside
//                                 restricted/machinery zones, matched by I-07
//
// There is no dual-write and no sync between them. The Site Personnel tab sits
// on Driver Enrollment because that screen already carries the customs+admin
// role policy and the DPDP-audited enrolment idiom — not because the two
// galleries are the same thing.

import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { BadgeCheck, Brain, Camera, Trash2, UserPlus, Users } from "lucide-react";

import { getAdapter } from "@/data";
import CameraCapture from "@/components/CameraCapture";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DataTable, StatCard, StatGrid, StatusChip, type Column } from "@/components/ui/dtccc";
import { EmptyState, Spinner } from "@/components/ui/misc";
import { useAuthedImage } from "@/hooks/useAuthedImage";
import {
  svKeys,
  useSvFaceEvents,
  useSvFaceStatus,
  useSvFaces,
  useSvHealth,
} from "@/hooks/useSecureVision";
import { STATUS } from "@/lib/tokens";
import { fmtDateTimeIST } from "@/lib/utils";
import { fmtConfidence, svErrorMessage, type SvFaceEvent, type SvPerson } from "@/lib/securevision";
import {
  SvCameraCell,
  SvSectionHeader,
  SvSourceBadge,
  SvUnavailable,
  SvVerdictChip,
} from "./SvCommon";

/** Small authenticated avatar for an enrolled person. */
function PersonPhoto({ person }: { person: SvPerson }) {
  const img = useAuthedImage(person.photo_url);
  if (img.status === "loading") return <Spinner className="h-3 w-3" />;
  if (img.status !== "ready" || !img.src) {
    return (
      <span className="inline-flex h-8 w-8 items-center justify-center rounded-full bg-muted text-[10px] text-muted-foreground">
        —
      </span>
    );
  }
  return (
    <img
      src={img.src}
      alt={person.name ?? "enrolled person"}
      className="h-8 w-8 rounded-full object-cover"
    />
  );
}

// ------------------------------------------------------------ enrolment dialog
function EnrollDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const qc = useQueryClient();
  const [personId, setPersonId] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState("");
  const [department, setDepartment] = useState("");
  const [photos, setPhotos] = useState<Blob[]>([]);
  const [useCamera, setUseCamera] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const enroll = useMutation({
    mutationFn: () =>
      getAdapter().svEnrollFace({
        person_id: personId.trim(),
        name: name.trim(),
        role: role.trim() || undefined,
        department: department.trim() || undefined,
        photos,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: svKeys.faces });
      void qc.invalidateQueries({ queryKey: svKeys.faceStatus });
      reset();
      onOpenChange(false);
    },
    // 409 duplicate / 422 no usable face / 503 model not loaded each get their
    // own sentence from svErrorMessage — the operator is told what to fix.
    onError: (e) => setError(svErrorMessage(e)),
  });

  function reset() {
    setPersonId("");
    setName("");
    setRole("");
    setDepartment("");
    setPhotos([]);
    setUseCamera(false);
    setError(null);
  }

  async function onCapture(dataUrl: string) {
    const blob = await (await fetch(dataUrl)).blob();
    setPhotos((prev) => [...prev, blob]);
    setUseCamera(false);
  }

  const canSubmit = personId.trim() && name.trim() && photos.length > 0 && !enroll.isPending;

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) reset();
        onOpenChange(v);
      }}
    >
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            Enrol site personnel <SvSourceBadge />
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <p className="text-[11px] text-muted-foreground">
            Enrols into the SecureVision gallery used by restricted-zone recognition (I-07). This is
            separate from driver identity, which stays in the JNPA identity service.
          </p>
          <div className="grid gap-2 sm:grid-cols-2">
            <LabeledInput
              label="Person ID *"
              value={personId}
              onChange={setPersonId}
              placeholder="EMP-1042"
            />
            <LabeledInput label="Name *" value={name} onChange={setName} placeholder="Full name" />
            <LabeledInput label="Role" value={role} onChange={setRole} placeholder="Technician" />
            <LabeledInput
              label="Department"
              value={department}
              onChange={setDepartment}
              placeholder="Maintenance"
            />
          </div>

          <div>
            <div className="mb-1 flex items-center justify-between">
              <span className="text-[11px] font-medium text-muted-foreground">
                Photos * ({photos.length} added — more photos give a more robust match)
              </span>
              <div className="flex gap-1.5">
                <Button size="sm" variant="outline" onClick={() => setUseCamera((v) => !v)}>
                  <Camera className="mr-1 h-3.5 w-3.5" />
                  {useCamera ? "Close camera" : "Camera"}
                </Button>
                <label className="inline-flex cursor-pointer items-center rounded-md border border-border px-2 py-1 text-xs hover:bg-muted">
                  Upload
                  <input
                    type="file"
                    accept="image/jpeg,image/png,image/webp"
                    multiple
                    className="hidden"
                    onChange={(e) =>
                      setPhotos((prev) => [...prev, ...Array.from(e.target.files ?? [])])
                    }
                  />
                </label>
              </div>
            </div>
            {useCamera && (
              <CameraCapture onCapture={onCapture} captureLabel="Capture photo" facing="user" />
            )}
          </div>

          {error && (
            <div
              className="rounded border px-2 py-1.5 text-xs"
              style={{ borderColor: STATUS.critical, color: STATUS.critical }}
            >
              {error}
            </div>
          )}

          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button disabled={!canSubmit} onClick={() => enroll.mutate()}>
              {enroll.isPending ? (
                <Spinner className="mr-2 h-3 w-3" />
              ) : (
                <UserPlus className="mr-2 h-3.5 w-3.5" />
              )}
              Enrol
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function LabeledInput({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="block">
      <span className="text-[11px] font-medium text-muted-foreground">{label}</span>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="mt-0.5 h-9 w-full rounded-md border border-border bg-background px-2 text-[13px] outline-none focus:ring-2 focus:ring-primary/20"
      />
    </label>
  );
}

// -------------------------------------------------------------- personnel tab
export function SvSitePersonnelPanel() {
  const qc = useQueryClient();
  const facesQ = useSvFaces();
  const statusQ = useSvFaceStatus();
  const [enrollOpen, setEnrollOpen] = useState(false);

  const setActive = useMutation({
    mutationFn: ({ pk, active }: { pk: number; active: boolean }) =>
      getAdapter().svUpdateFace(pk, { is_active: active }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: svKeys.faces }),
  });
  const remove = useMutation({
    mutationFn: (pk: number) => getAdapter().svDeleteFace(pk),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: svKeys.faces });
      void qc.invalidateQueries({ queryKey: svKeys.faceStatus });
    },
  });

  const columns: Column<SvPerson>[] = useMemo(
    () => [
      { key: "photo", header: "", render: (p) => <PersonPhoto person={p} /> },
      {
        key: "person_id",
        header: "Person ID",
        className: "font-mono",
        render: (p) => p.person_id ?? "—",
      },
      { key: "name", header: "Name", render: (p) => p.name ?? "—" },
      { key: "role", header: "Role", render: (p) => p.role ?? "—" },
      { key: "department", header: "Department", render: (p) => p.department ?? "—" },
      {
        key: "status",
        header: "Status",
        render: (p) => (
          <StatusChip
            label={p.is_active ? "Active" : "Inactive"}
            tone={p.is_active ? "ok" : "neutral"}
          />
        ),
      },
      {
        key: "created",
        header: "Enrolled",
        render: (p) => (p.created_at ? fmtDateTimeIST(p.created_at) : "—"),
      },
      {
        key: "actions",
        header: "",
        render: (p) => (
          <div className="flex justify-end gap-1.5">
            <Button
              size="sm"
              variant="outline"
              disabled={p.id == null || setActive.isPending}
              onClick={() => p.id != null && setActive.mutate({ pk: p.id, active: !p.is_active })}
            >
              {p.is_active ? "Deactivate" : "Activate"}
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={p.id == null || remove.isPending}
              onClick={() => {
                if (
                  p.id != null &&
                  window.confirm(`Remove ${p.name ?? p.person_id} from the SecureVision gallery?`)
                ) {
                  remove.mutate(p.id);
                }
              }}
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>
        ),
      },
    ],
    [remove, setActive],
  );

  if (facesQ.error)
    return <SvUnavailable error={facesQ.error} onRetry={() => void facesQ.refetch()} />;

  const people = facesQ.data ?? [];
  const active = people.filter((p) => p.is_active).length;

  return (
    <div className="space-y-3">
      <StatGrid>
        <StatCard icon={Users} label="Site personnel enrolled" value={people.length} tone="info" />
        <StatCard icon={BadgeCheck} label="Active" value={active} tone="ok" />
        <StatCard
          icon={Brain}
          label="Face model"
          value={statusQ.data?.model_ready ? "Ready" : (statusQ.data?.status ?? "—")}
          tone={statusQ.data?.model_ready ? "ok" : "warn"}
          sub={statusQ.data?.model_name ?? undefined}
        />
        <StatCard
          label="Gallery loaded"
          value={statusQ.data?.gallery_loaded ?? "—"}
          tone="neutral"
          sub="as reported by SecureVision"
        />
      </StatGrid>

      <Card className="p-3">
        <SvSectionHeader
          icon={Users}
          title="Site Personnel"
          subtitle="The SecureVision gallery matched by restricted-zone recognition (I-07). Driver identity is unchanged and remains in the JNPA identity service."
          right={
            <Button size="sm" onClick={() => setEnrollOpen(true)}>
              <UserPlus className="mr-1 h-3.5 w-3.5" /> Enrol person
            </Button>
          }
        />
        <DataTable
          columns={columns}
          rows={people}
          rowKey={(p) => String(p.id ?? p.person_id)}
          status={{
            isLoading: facesQ.isLoading,
            isFetching: facesQ.isFetching,
            isError: facesQ.isError,
            error: facesQ.error,
          }}
          onRetry={() => void facesQ.refetch()}
          search={(p, q) =>
            [p.person_id, p.name, p.role, p.department]
              .filter(Boolean)
              .some((v) => String(v).toLowerCase().includes(q))
          }
          searchPlaceholder="Search personnel…"
          emptyLabel="No site personnel enrolled in SecureVision."
          pageSize={10}
        />
      </Card>

      <EnrollDialog open={enrollOpen} onOpenChange={setEnrollOpen} />
    </div>
  );
}

// ------------------------------------------------------------------ face events
export function SvFaceEventsPanel({ limit = 100 }: { limit?: number }) {
  const q = useSvFaceEvents(limit);

  const columns: Column<SvFaceEvent>[] = useMemo(
    () => [
      {
        key: "when",
        header: "Time",
        render: (e) => (e.created_at ? fmtDateTimeIST(e.created_at) : "—"),
      },
      {
        key: "verdict",
        header: "Verdict",
        render: (e) => <SvVerdictChip status={e.person_status} />,
      },
      {
        key: "person",
        header: "Person",
        render: (e) => (
          <span className="text-xs">
            {e.name ?? <span className="text-muted-foreground">Not identified</span>}
            {e.person_id && <span className="ml-1 font-mono text-[11px]">({e.person_id})</span>}
          </span>
        ),
      },
      { key: "confidence", header: "Confidence", render: (e) => fmtConfidence(e.confidence) },
      { key: "camera", header: "Camera", render: (e) => <SvCameraCell camera={e.camera} /> },
      {
        key: "location",
        header: "Location",
        render: (e) =>
          e.latitude != null && e.longitude != null ? (
            <span className="font-mono text-[11px]">
              {e.latitude.toFixed(4)}, {e.longitude.toFixed(4)}
            </span>
          ) : (
            "—"
          ),
      },
      {
        key: "snapshot",
        header: "Snapshot",
        render: () => (
          <span
            className="text-[11px] text-muted-foreground"
            title="SecureVision stores event snapshots on its own filesystem and publishes no endpoint to fetch them."
          >
            not available
          </span>
        ),
      },
    ],
    [],
  );

  if (q.error) return <SvUnavailable error={q.error} onRetry={() => void q.refetch()} />;

  return (
    <Card className="p-3">
      <SvSectionHeader
        icon={Camera}
        title="Face recognition events"
        subtitle="SecureVision's own recognition log. Independent of JNPA geo-fence events."
      />
      <DataTable
        columns={columns}
        rows={q.data ?? []}
        rowKey={(e) => String(e.id ?? `${e.person_id}-${e.created_at}`)}
        status={{
          isLoading: q.isLoading,
          isFetching: q.isFetching,
          isError: q.isError,
          error: q.error,
        }}
        onRetry={() => void q.refetch()}
        emptyLabel="No SecureVision face events."
        pageSize={10}
      />
    </Card>
  );
}

// ----------------------------------------------------------- health integration
export function SvFaceModelCard() {
  const healthQ = useSvHealth();
  const statusQ = useSvFaceStatus();

  const health = healthQ.data;
  const status = statusQ.data;
  const reachable = health?.status === "LIVE";

  return (
    <Card className="p-3">
      <SvSectionHeader
        icon={Brain}
        title="SecureVision AI platform"
        subtitle="Proxied vendor integration — credentials are backend-only"
        right={
          <StatusChip
            label={health?.status ?? "…"}
            tone={reachable ? "ok" : health?.status === "NOT_CONFIGURED" ? "neutral" : "critical"}
          />
        }
      />
      {healthQ.isLoading ? (
        <div className="flex items-center gap-2 p-2 text-xs text-muted-foreground">
          <Spinner className="h-3 w-3" /> Checking SecureVision…
        </div>
      ) : healthQ.error ? (
        <SvUnavailable error={healthQ.error} onRetry={() => void healthQ.refetch()} compact />
      ) : (
        <div className="grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
          <Kv
            label="Connection"
            value={reachable ? "Connected" : (health?.detail ?? health?.status ?? "—")}
          />
          <Kv label="Service account" value={health?.service_account ?? "—"} />
          <Kv
            label="Mode"
            value={
              health?.mode === "UPLOAD_CLIP_ANALYTICS"
                ? "Clip upload analytics"
                : (health?.mode ?? "—")
            }
          />
          <Kv
            label="Persistence"
            value={
              health?.persistence === "NONE"
                ? "Session only (not persisted)"
                : (health?.persistence ?? "—")
            }
          />
          <Kv
            label="Face model"
            value={
              status?.model_ready ? `Ready · ${status.model_name ?? "—"}` : (status?.status ?? "—")
            }
          />
          <Kv label="Provider" value={status?.provider ?? "—"} />
          <Kv
            label="Similarity threshold"
            value={status?.similarity_threshold != null ? String(status.similarity_threshold) : "—"}
          />
          <Kv
            label="Gallery"
            value={
              status?.gallery_loaded != null
                ? `${status.gallery_loaded} loaded / ${status.authorized_in_db ?? "—"} enrolled`
                : "—"
            }
          />
          <Kv
            label="Camera mapping"
            value={
              health?.camera_map_configured
                ? `${health.camera_map_entries} cameras mapped`
                : "Not configured"
            }
          />
          <Kv label="Analyses this session" value={String(health?.analyses_in_session ?? 0)} />
        </div>
      )}
      {health?.status === "NOT_CONFIGURED" && (
        <EmptyState>
          SecureVision credentials are not set on this deployment. Every other console surface is
          unaffected.
        </EmptyState>
      )}
    </Card>
  );
}

function Kv({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[10.5px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="truncate text-foreground" title={value}>
        {value}
      </div>
    </div>
  );
}
