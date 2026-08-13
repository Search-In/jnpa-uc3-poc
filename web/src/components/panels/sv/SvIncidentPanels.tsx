// SecureVision incident panels for the EXISTING host screens.
//
// Each panel is an additive block on a screen that already owns the subject:
//
//   SvPlateContainerPanel  -> Camera AI (Trailers / Containers already live here)
//   SvVehicleCountPanel    -> Camera AI (queue/vehicle counting already lives here)
//   SvPersonZonePanel      -> Geo Analytics "AI Events" (zone intrusions live here)
//   SvTamperPanel          -> Gate & Lane Board "Camera Degraded" (camera health)
//   SvCombinedReportPanel  -> Reports & Enforcement (reporting + PDF live here)
//
// None of them replaces the JNPA data already on those screens; every one is
// badged SecureVision so vendor claims never blend into JNPA facts.

import { useMemo } from "react";
import {
  AlertTriangle,
  Camera,
  Container as ContainerIcon,
  ScanLine,
  ShieldAlert,
  Truck,
  VideoOff,
} from "lucide-react";

import { Card } from "@/components/ui/card";
import {
  DataTable,
  StatCard,
  StatGrid,
  StatusChip,
  TONE_COLOUR,
  type Column,
} from "@/components/ui/dtccc";
import { EmptyState, Spinner } from "@/components/ui/misc";
import { STATUS } from "@/lib/tokens";
import { fmtDateTimeIST } from "@/lib/utils";
import {
  fmtClipOffset,
  fmtConfidence,
  fmtDwell,
  type SvIncident,
  type SvPersonDetection,
  validationTone,
} from "@/lib/securevision";
import {
  buildCombinedReport,
  type SvReportFact,
  type SvReportSection,
  type SvReportSectionKey,
  type SvRiskAndAction,
} from "@/lib/svCombinedReport";
import { useSvCombined, useSvIncident, useSvPersons } from "@/hooks/useSecureVision";
import {
  SvAiBadge,
  SvCameraCell,
  SvContainerAgreementChip,
  SvEvidenceThumb,
  SvSectionHeader,
  SvUnavailable,
  SvVerdictChip,
} from "./SvCommon";

function NotFired({ what }: { what: string }) {
  return <EmptyState>SecureVision found no {what} in this clip.</EmptyState>;
}

function Loading() {
  return (
    <div className="flex items-center gap-2 p-4 text-xs text-muted-foreground">
      <Spinner className="h-3 w-3" /> Running SecureVision analyzer…
    </div>
  );
}

/** When the detection time is known, show it; otherwise show the clip offset and
 *  say so, rather than dressing a clip offset up as a wall-clock instant. */
function DetectedAt({ incident }: { incident: SvIncident }) {
  if (incident.detected_at) {
    return <span className="text-xs">{fmtDateTimeIST(incident.detected_at)}</span>;
  }
  return (
    <span
      className="text-xs text-muted-foreground"
      title="Clip-relative offset (upload time unknown)"
    >
      {fmtClipOffset(incident.clip_offset_s)}
    </span>
  );
}

// ------------------------------------------------------- I-01 + I-09 (vehicle)
export function SvPlateContainerPanel({ analysisId }: { analysisId: string | null }) {
  const plateQ = useSvIncident(analysisId, "i01");
  const containerQ = useSvIncident(analysisId, "i09");

  if (!analysisId) return null;
  if (plateQ.error) {
    return <SvUnavailable error={plateQ.error} onRetry={() => void plateQ.refetch()} />;
  }

  const plate = plateQ.data;
  const container = containerQ.data;

  return (
    <div className="space-y-3">
      <Card className="p-3">
        <SvSectionHeader
          icon={Truck}
          title="I-01 · Trailer Plate Capture"
          subtitle="ANPR read from the analysed clip"
        />
        {plateQ.isLoading ? (
          <Loading />
        ) : !plate?.fired ? (
          <NotFired what="vehicle plate" />
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Plate">
              <span className="font-mono text-sm font-semibold">{plate.plate?.plate ?? "—"}</span>
            </Field>
            <Field label="Plate valid">
              <StatusChip
                label={plate.plate?.plate_valid ? "Valid" : "Invalid"}
                tone={plate.plate?.plate_valid ? "ok" : "critical"}
              />
            </Field>
            <Field label="OCR confidence">{fmtConfidence(plate.ocr_confidence)}</Field>
            <Field label="Validation">
              <StatusChip
                label={plate.validation_status ?? "—"}
                tone={validationTone(plate.validation_status)}
              />
            </Field>
            <Field label="Vehicle type">{plate.plate?.vehicle_type ?? "—"}</Field>
            <Field label="Vehicle colour">{plate.plate?.vehicle_color ?? "—"}</Field>
            <Field label="Camera">
              <SvCameraCell camera={plate.camera} />
            </Field>
            <Field label="Track ID">{plate.track_id ?? "—"}</Field>
            <Field label="Detected">
              <DetectedAt incident={plate} />
            </Field>
            <Field label="Evidence">
              <div className="flex items-center gap-1.5">
                <SvEvidenceThumb url={plate.image_url} alt="Best frame" />
                {plate.evidence.map((e, i) => (
                  <SvEvidenceThumb
                    key={`${e.url}-${i}`}
                    url={e.url}
                    alt={`${e.region_type ?? "evidence"} crop`}
                    caption={`crop score ${fmtConfidence(e.crop_score)}`}
                  />
                ))}
              </div>
            </Field>
          </div>
        )}
        {plate?.description && (
          <p className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            {plate.description}
            {plate.ai_generated && <SvAiBadge provider={plate.vision_provider} />}
          </p>
        )}
      </Card>

      <Card className="p-3">
        <SvSectionHeader
          icon={ContainerIcon}
          title="I-09 · Container ISO 6346"
          subtitle="Vendor read, cross-checked against the JNPA ISO-6346 validator"
        />
        {containerQ.isLoading ? (
          <Loading />
        ) : containerQ.error ? (
          <SvUnavailable
            error={containerQ.error}
            onRetry={() => void containerQ.refetch()}
            compact
          />
        ) : !container?.fired ? (
          <NotFired what="container number" />
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Container">
              <span className="font-mono text-sm font-semibold">
                {container.container?.number ?? "—"}
              </span>
            </Field>
            <Field label="SecureVision validity">
              <StatusChip
                label={container.container?.vendor_valid ? "Valid" : "Invalid"}
                tone={container.container?.vendor_valid ? "ok" : "critical"}
              />
            </Field>
            <Field label="JNPA ISO-6346 check">
              <StatusChip
                label={container.container?.jnpa_valid ? "Valid" : "Invalid"}
                tone={container.container?.jnpa_valid ? "ok" : "critical"}
              />
            </Field>
            <Field label="Cross-check">
              <SvContainerAgreementChip
                agreement={container.container?.agreement}
                vendorValid={container.container?.vendor_valid}
                jnpaValid={container.container?.jnpa_valid}
              />
            </Field>
            <Field label="Towing vehicle">
              <span className="font-mono text-xs">{container.container?.plate ?? "—"}</span>
            </Field>
            <Field label="Camera">
              <SvCameraCell camera={container.camera} />
            </Field>
            <Field label="Detected">
              <DetectedAt incident={container} />
            </Field>
            <Field label="Evidence">
              <SvEvidenceThumb url={container.image_url} alt="Container best frame" />
            </Field>
          </div>
        )}
      </Card>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-[10.5px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="mt-0.5 truncate text-sm text-foreground">{children}</div>
    </div>
  );
}

// ------------------------------------------------------------------ I-02 count
export function SvVehicleCountPanel({ analysisId }: { analysisId: string | null }) {
  const q = useSvIncident(analysisId, "i02");
  const counts = q.data?.counts ?? [];

  if (!analysisId) return null;
  return (
    <Card className="p-3">
      <SvSectionHeader
        icon={Camera}
        title="I-02 · Vehicle Classification & Count"
        subtitle="Counts per class from the analysed clip. SecureVision reports classification only — it does not provide a congestion score."
      />
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <SvUnavailable error={q.error} onRetry={() => void q.refetch()} compact />
      ) : !q.data?.fired || !counts.length ? (
        <NotFired what="vehicle" />
      ) : (
        <>
          <StatGrid>
            <StatCard
              icon={Truck}
              label="Vehicles counted"
              value={q.data.total_count ?? 0}
              tone="info"
            />
            <StatCard icon={Camera} label="Classes seen" value={counts.length} tone="neutral" />
          </StatGrid>
          <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            {counts.map((c) => (
              <div
                key={c.vehicle_class ?? "unknown"}
                className="flex items-center justify-between rounded border border-border px-2 py-1.5"
              >
                <span className="text-xs capitalize text-muted-foreground">
                  {c.vehicle_class ?? "unknown"}
                </span>
                <span className="text-sm font-semibold tabular-nums">{c.count ?? 0}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </Card>
  );
}

// ----------------------------------------------------------------- I-07 person
export function SvPersonZonePanel({ analysisId }: { analysisId: string | null }) {
  const q = useSvPersons(analysisId);
  const persons = q.data?.persons ?? [];

  const columns: Column<SvPersonDetection>[] = useMemo(
    () => [
      {
        key: "verdict",
        header: "Verdict",
        render: (p) => <SvVerdictChip status={p.person_status} />,
      },
      {
        key: "person",
        header: "Person",
        render: (p) => (
          <span className="text-xs">
            {p.person_name ?? <span className="text-muted-foreground">Not identified</span>}
            {p.person_id && <span className="ml-1 font-mono text-[11px]">({p.person_id})</span>}
          </span>
        ),
      },
      {
        key: "zone",
        header: "Zone (SecureVision)",
        render: (p) => (
          <span
            className="text-xs"
            title="SecureVision's own zone. Not joined to JNPA geo-fence zones — the vendor exposes no zone API."
          >
            {p.zone ?? "—"}
          </span>
        ),
      },
      { key: "dwell", header: "Dwell", render: (p) => fmtDwell(p.dwell_seconds) },
      {
        key: "similarity",
        header: "Face match",
        render: (p) => (p.face_similarity == null ? "—" : fmtConfidence(p.face_similarity)),
      },
      { key: "camera", header: "Camera", render: (p) => <SvCameraCell camera={p.camera} /> },
      {
        key: "evidence",
        header: "Evidence",
        render: (p) => <SvEvidenceThumb url={p.image_url} alt="Person detection frame" />,
      },
    ],
    [],
  );

  if (!analysisId) return null;
  if (q.error) return <SvUnavailable error={q.error} onRetry={() => void q.refetch()} />;

  const unauthorized = persons.filter((p) => p.person_status === "UNAUTHORIZED").length;
  const unverified = persons.filter((p) => p.person_status === "UNVERIFIED").length;

  return (
    <Card className="p-3">
      <SvSectionHeader
        icon={ShieldAlert}
        title="I-07 · Person in Restricted/Machinery Zone"
        subtitle="One verdict per detected person. UNVERIFIED means identity could not be determined — it is not an accusation."
      />
      <StatGrid>
        <StatCard label="People detected" value={persons.length} tone="info" />
        <StatCard
          label="Unauthorized"
          value={unauthorized}
          tone={unauthorized > 0 ? "critical" : "ok"}
        />
        <StatCard label="Unverified" value={unverified} tone="neutral" />
      </StatGrid>
      <div className="mt-3">
        <DataTable
          columns={columns}
          rows={persons}
          rowKey={(p) => `${p.analysis_id}-${p.track_id}-${p.person_id ?? "anon"}`}
          status={{
            isLoading: q.isLoading,
            isFetching: q.isFetching,
            isError: q.isError,
            error: q.error,
          }}
          onRetry={() => void q.refetch()}
          emptyLabel="SecureVision detected no person in a restricted zone in this clip."
          pageSize={8}
        />
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------ I-12 camera
export function SvTamperPanel({ analysisId }: { analysisId: string | null }) {
  const q = useSvIncident(analysisId, "i12");
  const tamper = q.data?.tamper;

  if (!analysisId) return null;
  return (
    <Card className="p-3">
      <SvSectionHeader
        icon={VideoOff}
        title="I-12 · AI Tamper Check"
        subtitle="An INDEPENDENT signal. It does not replace the ANPR camera-health rung shown above — a camera can be LIVE and still fail this check."
      />
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <SvUnavailable error={q.error} onRetry={() => void q.refetch()} compact />
      ) : !q.data?.fired ? (
        <EmptyState>SecureVision found no tamper condition in this clip.</EmptyState>
      ) : (
        <div className="flex flex-wrap items-center gap-4">
          <div>
            <div className="text-[10.5px] uppercase tracking-wide text-muted-foreground">
              Camera
            </div>
            <SvCameraCell camera={q.data.camera} />
          </div>
          <div>
            <div className="text-[10.5px] uppercase tracking-wide text-muted-foreground">
              AI tamper check
            </div>
            <StatusChip label={tamper?.tamper_state ?? "—"} tone="critical" />
          </div>
          <div>
            <div className="text-[10.5px] uppercase tracking-wide text-muted-foreground">
              Analytic confidence
            </div>
            <span className="text-sm tabular-nums">
              {tamper?.analytic_confidence_pct == null ? "—" : `${tamper.analytic_confidence_pct}%`}
            </span>
          </div>
          <div>
            <div className="text-[10.5px] uppercase tracking-wide text-muted-foreground">
              Evidence
            </div>
            <SvEvidenceThumb url={q.data.image_url} alt="Tamper frame" />
          </div>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------- combined ALL
// The combined report is the operator's FIRST screen after an analysis, so it
// is laid out to be scanned, not read: a verbatim summary line, a risk/action
// strip, then one compact card per subject. The AI narrative is still carried
// in full behind a disclosure — the redesign re-arranges the same content, it
// never edits, re-words or supplements what SecureVision said (lib/
// svCombinedReport.ts owns that split and is unit-tested).

const SECTION_ICON: Record<SvReportSectionKey, React.ComponentType<{ className?: string }>> = {
  vehicle: Truck,
  person: ShieldAlert,
  camera: VideoOff,
};

/** One label/value pair — a chip when the model gave the value a tone. */
function ReportFact({ fact }: { fact: SvReportFact }) {
  return (
    <div className="flex items-baseline justify-between gap-2" title={fact.hint}>
      <span className="shrink-0 text-[10.5px] uppercase tracking-wide text-muted-foreground">
        {fact.label}
      </span>
      {fact.tone ? (
        <StatusChip label={fact.value} tone={fact.tone} />
      ) : (
        <span
          className={`min-w-0 truncate text-right text-xs text-foreground${
            fact.mono ? " font-mono font-semibold" : ""
          }`}
        >
          {fact.value}
        </span>
      )}
    </div>
  );
}

/** A findings card. Rendered only for sections that actually have data. */
function ReportSectionCard({ section }: { section: SvReportSection }) {
  const Icon = SECTION_ICON[section.key];
  return (
    <div className="rounded-md border border-border bg-card/60 p-2.5">
      <div className="mb-1.5 flex items-center gap-1.5">
        <Icon className="h-3.5 w-3.5 text-muted-foreground" />
        <h4 className="truncate text-[11px] font-semibold uppercase tracking-wide text-foreground">
          {section.title}
        </h4>
      </div>
      {section.facts.length > 0 && (
        <div className="space-y-1">
          {section.facts.map((f) => (
            <ReportFact key={`${section.key}-${f.label}`} fact={f} />
          ))}
        </div>
      )}
      {section.notes.length > 0 && (
        <ul className="mt-1.5 space-y-1 border-t border-border pt-1.5">
          {section.notes.slice(0, 3).map((note, i) => (
            <li key={`${section.key}-note-${i}`} className="flex gap-1.5 text-[11px] leading-snug">
              <span aria-hidden className="text-muted-foreground">
                •
              </span>
              <span className="min-w-0 text-muted-foreground">{note}</span>
            </li>
          ))}
          {section.notes.length > 3 && (
            <li className="text-[10.5px] text-muted-foreground/80">
              +{section.notes.length - 3} more in the full narrative below
            </li>
          )}
        </ul>
      )}
    </div>
  );
}

/** Risk / priority / recommended action, exactly as the narrative stated them. */
function RiskActionStrip({ riskAction }: { riskAction: SvRiskAndAction }) {
  const tile = (label: string, value: string, colour: string, wide = false) => (
    <div
      key={label}
      className={`rounded-md border px-2.5 py-1.5${wide ? " sm:col-span-2" : ""}`}
      style={{ borderColor: `${colour}55`, backgroundColor: `${colour}0d` }}
    >
      <div className="text-[10.5px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-[13px] font-semibold leading-snug" style={{ color: colour }}>
        {value}
      </div>
    </div>
  );

  return (
    <div className="grid gap-2 sm:grid-cols-4">
      {riskAction.risk &&
        tile("Risk level", riskAction.risk.value, TONE_COLOUR[riskAction.risk.tone])}
      {riskAction.priority &&
        tile("Priority", riskAction.priority.value, TONE_COLOUR[riskAction.priority.tone])}
      {riskAction.action && tile("Recommended action", riskAction.action, TONE_COLOUR.info, true)}
    </div>
  );
}

export function SvCombinedReportPanel({ analysisId }: { analysisId: string | null }) {
  const q = useSvCombined(analysisId);
  const report = q.data;
  const view = useMemo(() => buildCombinedReport(report), [report]);

  const columns: Column<SvIncident>[] = useMemo(
    () => [
      { key: "type", header: "Incident", render: (i) => i.incident_type ?? "—" },
      { key: "title", header: "Title", render: (i) => i.title ?? "—" },
      {
        key: "status",
        header: "Status",
        render: (i) => (
          <StatusChip
            label={i.status ?? "—"}
            tone={i.status === "SUCCESS" ? "ok" : i.status ? "warn" : "neutral"}
          />
        ),
      },
      {
        key: "validation",
        header: "Validation",
        render: (i) => (
          <StatusChip
            label={i.validation_status ?? "—"}
            tone={validationTone(i.validation_status)}
          />
        ),
      },
      { key: "confidence", header: "Confidence", render: (i) => fmtConfidence(i.confidence) },
      { key: "camera", header: "Camera", render: (i) => <SvCameraCell camera={i.camera} /> },
      { key: "when", header: "Detected", render: (i) => <DetectedAt incident={i} /> },
      {
        key: "evidence",
        header: "Evidence",
        render: (i) => <SvEvidenceThumb url={i.image_url} alt="Incident frame" />,
      },
    ],
    [],
  );

  if (!analysisId) return null;
  if (q.error) return <SvUnavailable error={q.error} onRetry={() => void q.refetch()} />;

  return (
    <div className="space-y-3">
      <Card className="p-3">
        <SvSectionHeader
          icon={ScanLine}
          title="Combined incident report"
          subtitle="Operator summary of every analyzer that fired for this clip"
          right={
            <StatusChip
              label={`${view.incidents.length} detection${view.incidents.length === 1 ? "" : "s"}`}
              tone={view.incidents.length ? "info" : "neutral"}
            />
          }
        />

        {q.isLoading ? (
          <Loading />
        ) : (
          <div className="space-y-2.5">
            {view.summary && (
              <div
                className="rounded border p-2.5"
                style={{
                  borderColor: `${STATUS.warning}55`,
                  backgroundColor: `${STATUS.warning}0d`,
                }}
              >
                <div className="mb-1 flex items-center gap-2">
                  <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                    Summary
                  </span>
                  {view.aiGenerated && <SvAiBadge />}
                </div>
                <p className="text-sm leading-snug text-foreground">{view.summary}</p>
                <p className="mt-1 text-[10.5px] text-muted-foreground">
                  Written by a language model from the detections below. Verify against the evidence
                  before acting on it.
                </p>
              </div>
            )}

            {view.riskAction && <RiskActionStrip riskAction={view.riskAction} />}

            {view.sections.length > 0 && (
              <div className="grid gap-2 md:grid-cols-3">
                {view.sections.map((section) => (
                  <ReportSectionCard key={section.key} section={section} />
                ))}
              </div>
            )}

            {view.other.length > 0 && (
              <ul className="space-y-1">
                {view.other.map((note, i) => (
                  <li key={`other-${i}`} className="flex gap-1.5 text-[11px] leading-snug">
                    <AlertTriangle
                      className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground"
                      aria-hidden
                    />
                    <span className="min-w-0 text-muted-foreground">{note}</span>
                  </li>
                ))}
              </ul>
            )}

            {!view.summary && !view.riskAction && view.sections.length === 0 && (
              <EmptyState>No incidents fired for this clip.</EmptyState>
            )}

            {view.narrative && (
              <details className="group rounded-md border border-border">
                <summary className="flex cursor-pointer list-none items-center gap-2 px-2.5 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground transition hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                  <span className="transition-transform group-open:rotate-90">›</span>
                  View full narrative
                  <span className="ml-auto text-[10px] font-normal normal-case tracking-normal text-muted-foreground/70">
                    verbatim from SecureVision
                  </span>
                </summary>
                <p className="whitespace-pre-line border-t border-border px-2.5 py-2 text-xs leading-relaxed text-foreground">
                  {view.narrative}
                </p>
              </details>
            )}

            <details className="group rounded-md border border-border">
              <summary className="flex cursor-pointer list-none items-center gap-2 px-2.5 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground transition hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                <span className="transition-transform group-open:rotate-90">›</span>
                View detection details ({view.incidents.length})
              </summary>
              <div className="border-t border-border p-2">
                <DataTable
                  columns={columns}
                  rows={view.incidents}
                  rowKey={(i) => `${i.analysis_id}-${i.incident_type}-${i.track_id ?? "x"}`}
                  status={{
                    isLoading: q.isLoading,
                    isFetching: q.isFetching,
                    isError: q.isError,
                    error: q.error,
                  }}
                  onRetry={() => void q.refetch()}
                  emptyLabel="No incidents fired for this clip."
                  pageSize={8}
                />
              </div>
            </details>
          </div>
        )}
      </Card>
    </div>
  );
}
