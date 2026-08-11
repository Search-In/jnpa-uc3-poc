// SecureVision AI intelligence inside the Vehicle 360 profile.
//
// This is the PRIMARY vehicle-facing destination for SecureVision data: the
// operator is already looking at one vehicle, and the question "what has the AI
// seen of this vehicle?" belongs beside its RC, FASTag and violation history —
// not on a separate screen they have to go and correlate by hand.
//
// It is deliberately a CONCISE section, not a second Camera AI page: plate read,
// vehicle classification, container link, camera, evidence, and a compact AI
// timeline. Anything deeper is one click away in the Video Analytics workbench.
//
// The honest limit, stated in the UI: SecureVision has no incident-history API,
// so this scans the most recent analyses from this session for reads matching
// the plate. It cannot search a history that the vendor does not expose.

import { Link } from "react-router-dom";
import { Camera, Container as ContainerIcon, ScanSearch, Truck } from "lucide-react";

import { Card } from "@/components/ui/card";
import { StatusChip } from "@/components/ui/dtccc";
import { EmptyState, Spinner } from "@/components/ui/misc";
import { useSvVehicleHits } from "@/hooks/useSecureVision";
import { fmtDateTimeIST } from "@/lib/utils";
import { fmtClipOffset, fmtConfidence, validationTone } from "@/lib/securevision";
import {
  SvAiBadge,
  SvCameraCell,
  SvContainerAgreementChip,
  SvEvidenceThumb,
  SvSectionHeader,
  SvUnavailable,
} from "./SvCommon";

export function SvVehicleIntelPanel({ plate }: { plate: string }) {
  const { hits, scanned, truncated, isLoading, error, refetch } = useSvVehicleHits(plate);

  if (error) {
    return (
      <Card className="p-3">
        <SvSectionHeader icon={ScanSearch} title="SecureVision AI Intelligence" />
        <SvUnavailable error={error} onRetry={refetch} compact />
      </Card>
    );
  }

  return (
    <Card className="p-3">
      <SvSectionHeader
        icon={ScanSearch}
        title="SecureVision AI Intelligence"
        subtitle={
          scanned > 0
            ? `Matched against ${scanned} analysed clip${scanned === 1 ? "" : "s"} from this session${
                truncated ? " (most recent only)" : ""
              }. SecureVision keeps no incident history.`
            : "No clip has been analysed in this session."
        }
        right={
          <Link
            to="/video-analytics"
            className="text-xs underline underline-offset-2 text-muted-foreground hover:text-foreground"
          >
            Video Analytics
          </Link>
        }
      />

      {isLoading ? (
        <div className="flex items-center gap-2 p-3 text-xs text-muted-foreground">
          <Spinner className="h-3 w-3" /> Matching SecureVision detections…
        </div>
      ) : hits.length === 0 ? (
        <EmptyState>
          No SecureVision detection of {plate} in the analysed clips. Analyse a clip from the
          vehicle&apos;s camera to see AI detections here.
        </EmptyState>
      ) : (
        <ul className="divide-y divide-border/50">
          {hits.map(({ analysis, plate: plateHit, container }) => (
            <li key={analysis.analysis_id} className="py-3 first:pt-1 last:pb-1">
              <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
                <Camera className="h-3.5 w-3.5" aria-hidden />
                <SvCameraCell camera={plateHit?.camera ?? container?.camera} />
                <span>·</span>
                <span>{analysis.filename ?? analysis.analysis_id}</span>
                <span>·</span>
                <span>
                  {plateHit?.detected_at
                    ? fmtDateTimeIST(plateHit.detected_at)
                    : fmtClipOffset(plateHit?.clip_offset_s)}
                </span>
              </div>

              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                {plateHit && (
                  <>
                    <Cell label="Plate read" icon={Truck}>
                      <span className="font-mono text-sm font-semibold">
                        {plateHit.plate?.plate ?? "—"}
                      </span>
                    </Cell>
                    <Cell label="Plate valid">
                      <StatusChip
                        label={plateHit.plate?.plate_valid ? "Valid" : "Invalid"}
                        tone={plateHit.plate?.plate_valid ? "ok" : "critical"}
                      />
                    </Cell>
                    <Cell label="OCR confidence">{fmtConfidence(plateHit.ocr_confidence)}</Cell>
                    <Cell label="Validation">
                      <StatusChip
                        label={plateHit.validation_status ?? "—"}
                        tone={validationTone(plateHit.validation_status)}
                      />
                    </Cell>
                    <Cell label="Classification">
                      {[plateHit.plate?.vehicle_color, plateHit.plate?.vehicle_type]
                        .filter(Boolean)
                        .join(" ") || "—"}
                    </Cell>
                    <Cell label="Track ID">{plateHit.track_id ?? "—"}</Cell>
                  </>
                )}

                {container && (
                  <>
                    <Cell label="Container" icon={ContainerIcon}>
                      <span className="font-mono text-sm">
                        {container.container?.number ?? "—"}
                      </span>
                    </Cell>
                    <Cell label="ISO-6346 cross-check">
                      <SvContainerAgreementChip
                        agreement={container.container?.agreement}
                        vendorValid={container.container?.vendor_valid}
                        jnpaValid={container.container?.jnpa_valid}
                      />
                    </Cell>
                  </>
                )}

                <Cell label="Evidence">
                  <div className="flex items-center gap-1.5">
                    <SvEvidenceThumb url={plateHit?.image_url} alt="Best frame" />
                    {(plateHit?.evidence ?? []).map((e, i) => (
                      <SvEvidenceThumb
                        key={`${e.url}-${i}`}
                        url={e.url}
                        alt={`${e.region_type ?? "evidence"} crop`}
                      />
                    ))}
                    {container && (
                      <SvEvidenceThumb url={container.image_url} alt="Container frame" />
                    )}
                  </div>
                </Cell>
              </div>

              {plateHit?.description && (
                <p className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  {plateHit.description}
                  {plateHit.ai_generated && <SvAiBadge provider={plateHit.vision_provider} />}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function Cell({
  label,
  icon: Icon,
  children,
}: {
  label: string;
  icon?: React.ComponentType<{ className?: string }>;
  children: React.ReactNode;
}) {
  return (
    <div className="min-w-0">
      <div className="flex items-center gap-1 text-[10.5px] uppercase tracking-wide text-muted-foreground">
        {Icon && <Icon className="h-3 w-3" />}
        {label}
      </div>
      <div className="mt-0.5 truncate text-sm text-foreground">{children}</div>
    </div>
  );
}
