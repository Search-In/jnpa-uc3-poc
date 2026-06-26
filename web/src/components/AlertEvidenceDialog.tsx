// AlertEvidenceDialog — the incident evidence modal (photo/clip + e-Challan +
// raw payload). Extracted verbatim from LiveOperations so the new header
// notification drawer can reuse it. No behavioural changes.

import { useTranslation } from "react-i18next";
import { FileText, ImageOff } from "lucide-react";
import type { Alert } from "@/lib/types";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { severityColour } from "@/lib/palette";
import { fmtTimeIST } from "@/lib/utils";

export function AlertEvidenceDialog({
  alert,
  onClose,
}: {
  alert: Alert | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const evidence = alert?.payload?.evidence_url as string | undefined;
  const mp4 = alert?.payload?.evidence_mp4_url as string | undefined;
  const echallanId = alert?.payload?.echallan_id as string | undefined;
  const echallanPdf = alert?.payload?.echallan_pdf_url as string | undefined;
  const sev = alert ? severityColour(alert.severity) : undefined;
  return (
    <Dialog open={!!alert} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        {alert && (
          <>
            <DialogHeader className="flex items-center gap-3">
              {/* Left severity rail mirrors the alert cards in the drawer. */}
              <span
                className="h-9 w-1 shrink-0 rounded-full"
                style={{ backgroundColor: sev }}
                aria-hidden
              />
              <DialogTitle className="flex min-w-0 flex-col gap-0.5">
                <span className="truncate text-base font-semibold leading-tight tracking-tight">
                  {t(`alertKind.${alert.kind}`, { defaultValue: alert.kind })}
                </span>
                <span className="font-mono text-xs font-medium text-muted-foreground">
                  {alert.plate ?? "—"}
                </span>
              </DialogTitle>
            </DialogHeader>

            <div className="space-y-4 p-5">
              {/* Incident metadata. */}
              <div className="grid grid-cols-2 gap-x-4 gap-y-3.5">
                <Field k={t("notifications.time", { defaultValue: "Time (IST)" })}>
                  <span className="font-medium tabular-nums">{fmtTimeIST(alert.ts)}</span>
                </Field>
                <Field k={t("notifications.severity", { defaultValue: "Severity" })}>
                  <span className="inline-flex items-center gap-1.5 font-medium">
                    <span
                      className="h-2 w-2 shrink-0 rounded-full"
                      style={{ backgroundColor: sev }}
                      aria-hidden
                    />
                    {alert.severity}
                  </span>
                </Field>
                <Field k={t("notifications.gate", { defaultValue: "Gate" })}>
                  <span className="font-medium">{alert.gate_id ?? "—"}</span>
                </Field>
                <Field k={t("notifications.zone", { defaultValue: "Zone" })}>
                  <span className="font-medium">{(alert.payload?.zone_id as string) ?? "—"}</span>
                </Field>
              </div>

              {/* TFC-2: play the last-10s evidence clip when present. */}
              {mp4 ? (
                <video
                  src={mp4}
                  controls
                  autoPlay
                  muted
                  loop
                  className="w-full rounded-lg border border-border bg-black shadow-sm"
                  data-testid="evidence-video"
                >
                  Your browser does not support the video tag.
                </video>
              ) : evidence ? (
                <img
                  src={evidence}
                  alt="incident evidence from MinIO"
                  className="w-full rounded-lg border border-border shadow-sm"
                  onError={(e) => ((e.target as HTMLImageElement).style.display = "none")}
                />
              ) : (
                <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-muted/30 px-6 py-10 text-center text-sm text-muted-foreground">
                  <ImageOff className="h-7 w-7 opacity-40" aria-hidden />
                  No photographic evidence attached.
                </div>
              )}

              {echallanId && (
                <div className="flex items-center justify-between rounded-lg border border-severity-warning/50 bg-severity-warning/10 px-3.5 py-2.5 text-xs">
                  <span>
                    e-Challan <span className="font-mono font-semibold">{echallanId}</span>
                  </span>
                  {echallanPdf && (
                    <a
                      href={echallanPdf}
                      target="_blank"
                      rel="noreferrer"
                      className="font-semibold text-severity-info hover:underline"
                    >
                      open PDF
                    </a>
                  )}
                </div>
              )}

              {alert.payload && Object.keys(alert.payload).length > 0 && (
                <div className="space-y-1.5">
                  <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                    <FileText className="h-3.5 w-3.5" aria-hidden />
                    {t("notifications.details", { defaultValue: "Details" })}
                  </div>
                  <dl className="overflow-hidden rounded-lg border border-border">
                    {Object.entries(alert.payload).map(([key, value]) => (
                      <div
                        key={key}
                        className="flex items-start justify-between gap-3 border-b border-border/60 bg-muted/30 px-3 py-2 text-xs last:border-b-0"
                      >
                        <dt className="shrink-0 font-medium text-muted-foreground">
                          {humanizeKey(key)}
                        </dt>
                        <dd
                          className="min-w-0 truncate text-right font-mono font-medium text-foreground"
                          title={formatValue(value)}
                        >
                          {formatValue(value)}
                        </dd>
                      </div>
                    ))}
                  </dl>
                </div>
              )}
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

/** "camera_id" → "Camera id" — readable label from a payload key. */
function humanizeKey(key: string): string {
  const spaced = key.replace(/[_-]+/g, " ").trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** Render a payload value as a compact, human-readable string. */
function formatValue(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function Field({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="mb-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
        {k}
      </div>
      <div className="truncate text-sm text-foreground">{children}</div>
    </div>
  );
}

export default AlertEvidenceDialog;
