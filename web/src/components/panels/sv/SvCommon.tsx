// Shared SecureVision UI atoms.
//
// Every SecureVision element in the console carries a visible source badge:
// vendor data sits beside JNPA data on the same screens, and an operator must
// never have to guess which system asserted something. These atoms are built
// from the existing DTCCC kit (StatusChip / Card / Dialog / Badge) so the
// integration looks native rather than like a bolted-on second design system.

import { useState, type ReactNode } from "react";
import { AlertTriangle, Image as ImageIcon, ShieldQuestion, Sparkles } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EmptyState, Spinner } from "@/components/ui/misc";
import { StatusChip } from "@/components/ui/dtccc";
import { useAuthedImage } from "@/hooks/useAuthedImage";
import { STATUS } from "@/lib/tokens";
import {
  SV_SOURCE,
  cameraHint,
  cameraLabel,
  containerAgreementLabel,
  containerAgreementTone,
  isNotConfigured,
  personVerdictHint,
  personVerdictLabel,
  personVerdictTone,
  svErrorMessage,
  type SvCamera,
  type SvContainerAgreement,
  type SvPersonStatus,
} from "@/lib/securevision";

/** Attribution for any element whose data came from the vendor. */
export function SvSourceBadge({ label = SV_SOURCE }: { label?: string }) {
  return (
    <Badge colour={STATUS.info} dot={false}>
      {label}
    </Badge>
  );
}

/** Provenance for a language-model narrative. Rendered next to the text itself,
 *  never once at the top of a page — the badge has to travel with the claim. */
export function SvAiBadge({ provider }: { provider?: string | null }) {
  return (
    <span
      className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10.5px] font-medium"
      style={{ backgroundColor: `${STATUS.warning}1a`, color: STATUS.warning }}
      title={
        provider
          ? `Written by ${provider}. Treat as a summary, not a verified fact.`
          : "Written by a language model. Treat as a summary, not a verified fact."
      }
    >
      <Sparkles className="h-3 w-3" aria-hidden />
      AI-generated
    </span>
  );
}

/** Camera attribution that refuses to guess.
 *
 *  A mapped code shows the JNPA camera; an unmapped one shows the vendor's own
 *  code plus an explicit "mapping unavailable" note, so a detection is never
 *  silently attributed to the wrong physical camera. */
export function SvCameraCell({ camera }: { camera?: SvCamera | null }) {
  const hint = cameraHint(camera);
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="font-mono text-xs">{cameraLabel(camera)}</span>
      {hint && (
        <span title={hint} className="inline-flex items-center" aria-label={hint}>
          <AlertTriangle className="h-3.5 w-3.5" style={{ color: STATUS.warning }} aria-hidden />
        </span>
      )}
    </span>
  );
}

/** The three-state person verdict. UNVERIFIED never renders as UNAUTHORIZED. */
export function SvVerdictChip({ status }: { status: SvPersonStatus | null | undefined }) {
  return (
    <span title={personVerdictHint(status)} className="inline-flex items-center gap-1">
      {status === "UNVERIFIED" && (
        <ShieldQuestion className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
      )}
      <StatusChip label={personVerdictLabel(status)} tone={personVerdictTone(status)} />
    </span>
  );
}

/** SecureVision's container verdict cross-checked against our ISO-6346 rule. */
export function SvContainerAgreementChip({
  agreement,
  vendorValid,
  jnpaValid,
}: {
  agreement: SvContainerAgreement | null | undefined;
  vendorValid?: boolean | null;
  jnpaValid?: boolean | null;
}) {
  const title =
    `SecureVision says ${vendorValid == null ? "unknown" : vendorValid ? "valid" : "invalid"}; ` +
    `JNPA ISO-6346 check says ${jnpaValid == null ? "unknown" : jnpaValid ? "valid" : "invalid"}.`;
  return (
    <span title={title}>
      <StatusChip
        label={containerAgreementLabel(agreement)}
        tone={containerAgreementTone(agreement)}
      />
    </span>
  );
}

/** An evidence frame behind the bearer token, with a click-to-enlarge dialog.
 *  Mirrors the existing AlertEvidenceDialog interaction rather than inventing a
 *  second lightbox. */
export function SvEvidenceThumb({
  url,
  alt,
  caption,
  size = 44,
}: {
  url: string | null | undefined;
  alt: string;
  caption?: ReactNode;
  size?: number;
}) {
  const [open, setOpen] = useState(false);
  const thumb = useAuthedImage(url ?? null);
  const full = useAuthedImage(open ? (url ?? null) : null);

  if (!url) {
    return (
      <span className="text-xs text-muted-foreground" title="No evidence image for this detection">
        —
      </span>
    );
  }
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex items-center justify-center overflow-hidden rounded border border-border bg-muted/40"
        style={{ width: size, height: size }}
        title="View evidence"
        aria-label={alt}
      >
        {thumb.status === "loading" && <Spinner className="h-3 w-3" />}
        {thumb.status === "ready" && thumb.src && (
          <img src={thumb.src} alt={alt} className="h-full w-full object-cover" />
        )}
        {thumb.status === "error" && (
          <ImageIcon className="h-4 w-4 text-muted-foreground" aria-hidden />
        )}
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              {alt} <SvSourceBadge />
            </DialogTitle>
          </DialogHeader>
          <div className="flex items-center justify-center">
            {full.status === "loading" && <Spinner />}
            {full.status === "ready" && full.src && (
              <img src={full.src} alt={alt} className="max-h-[70vh] w-auto rounded" />
            )}
            {full.status === "error" && (
              <EmptyState>Evidence image is no longer available from SecureVision.</EmptyState>
            )}
          </div>
          {caption && <div className="mt-2 text-xs text-muted-foreground">{caption}</div>}
        </DialogContent>
      </Dialog>
    </>
  );
}

/** One consistent "SecureVision cannot answer right now" surface.
 *
 *  A missing integration is a normal state, not a crash: an unconfigured or
 *  unreachable vendor must read as a calm explanation on an otherwise working
 *  screen, because every other panel around it is unaffected. */
export function SvUnavailable({
  error,
  onRetry,
  compact,
}: {
  error: unknown;
  onRetry?: () => void;
  compact?: boolean;
}) {
  const message = svErrorMessage(error);
  const notConfigured = isNotConfigured(error);
  const body = (
    <div className="flex flex-col items-center gap-2 py-4 text-center">
      <AlertTriangle
        className="h-5 w-5"
        style={{ color: notConfigured ? STATUS.unknown : STATUS.warning }}
        aria-hidden
      />
      <div className="text-sm text-muted-foreground">{message}</div>
      {onRetry && !notConfigured && (
        <button
          type="button"
          onClick={onRetry}
          className="rounded border border-border px-2 py-1 text-xs hover:bg-muted"
        >
          Retry
        </button>
      )}
    </div>
  );
  return compact ? body : <Card className="p-3">{body}</Card>;
}

/** Section header used by every embedded SecureVision block. */
export function SvSectionHeader({
  icon: Icon,
  title,
  subtitle,
  right,
}: {
  icon?: React.ComponentType<{ className?: string }>;
  title: string;
  subtitle?: ReactNode;
  right?: ReactNode;
}) {
  return (
    <div className="mb-2 flex items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          {Icon && <Icon className="h-4 w-4 text-muted-foreground" />}
          <h3 className="truncate text-sm font-semibold text-foreground">{title}</h3>
          <SvSourceBadge />
        </div>
        {subtitle && <div className="mt-0.5 text-[11px] text-muted-foreground">{subtitle}</div>}
      </div>
      {right}
    </div>
  );
}
