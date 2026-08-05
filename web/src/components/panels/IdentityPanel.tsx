import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Camera, CameraOff, ScanFace, UserCheck, AlertTriangle } from "lucide-react";
import { getAdapter } from "@/data";
import type { IdentityVerifyResult } from "@/lib/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Spinner, EmptyState } from "@/components/ui/misc";
import { STATUS } from "@/lib/tokens";
import { fmtDateTimeIST } from "@/lib/utils";
import { useWebcam } from "@/hooks/useWebcam";

// Driver identity verification (capability C2). The synthetic Simulate dropdown
// is replaced by a real browser-camera capture: Start Camera → live preview with
// a face guide → Capture & Verify, which sends the frame to /api/identity/verify.
// "Enroll reference" captures the selected driver's reference template first. The
// decision (VERIFIED / PROVISIONAL / REJECTED) + thresholds + DPDP are unchanged
// server-side; only the embedding source moved from a hash to a real frame.

function decisionColour(decision?: string): string {
  switch (decision) {
    case "VERIFIED":
      return STATUS.ok;
    case "PROVISIONAL":
      return STATUS.warning;
    case "REJECTED":
      return STATUS.critical;
    default:
      return STATUS.unknown;
  }
}

function confidenceBand(score: number): { labelKey: string; colour: string } {
  if (score >= 0.9) return { labelKey: "identityPanel.confidenceHigh", colour: STATUS.ok };
  if (score >= 0.5) return { labelKey: "identityPanel.confidenceMedium", colour: STATUS.warning };
  return { labelKey: "identityPanel.confidenceLow", colour: STATUS.critical };
}

// Friendly text for an alignment/liveness validation failure.
// Values are translation keys resolved with t() inside the component.
const VALIDATION_TEXT: Record<string, string> = {
  no_face_detected: "identityPanel.validationNoFace",
  multiple_faces: "identityPanel.validationMultipleFaces",
  face_not_centered: "identityPanel.validationNotCentered",
  move_closer: "identityPanel.validationMoveCloser",
  move_back: "identityPanel.validationMoveBack",
  camera_not_ready: "identityPanel.validationCameraNotReady",
};

export function IdentityPanel() {
  const { t } = useTranslation();
  const cam = useWebcam();

  const galleryQ = useQuery({
    queryKey: ["identity-gallery"],
    queryFn: () => getAdapter().identityGallery(),
  });
  const gallery = galleryQ.data ?? [];

  const [driverId, setDriverId] = useState<string>("");
  const selected = driverId || gallery[0]?.driver_id || "";
  const selectedDriver = gallery.find((d) => d.driver_id === selected);

  const [notice, setNotice] = useState<{ kind: "warn" | "ok"; text: string } | null>(null);
  const [verifiedAt, setVerifiedAt] = useState<string | null>(null);

  const verify = useMutation({
    mutationFn: (image: string) => getAdapter().identityVerify(selected, { image }),
    onSuccess: () => {
      setNotice(null);
      setVerifiedAt(new Date().toISOString());
    },
  });

  const enroll = useMutation({
    mutationFn: (image: string) => getAdapter().identityEnroll(selected, image),
    onSuccess: () => setNotice({ kind: "ok", text: t("identityPanel.enrolSuccess") }),
  });

  async function captureValidated(): Promise<string | null> {
    const v = await cam.validate();
    if (!v.ok) {
      const key = VALIDATION_TEXT[v.reason];
      setNotice({
        kind: "warn",
        text: key ? t(key) : t("identityPanel.faceCheckFailed"),
      });
      return null;
    }
    const image = cam.capture();
    if (!image) {
      setNotice({ kind: "warn", text: t("identityPanel.captureFailed") });
      return null;
    }
    return image;
  }

  async function onVerify() {
    const image = await captureValidated();
    if (image) verify.mutate(image);
  }

  async function onEnroll() {
    const image = await captureValidated();
    if (image) enroll.mutate(image);
  }

  const live = cam.status === "live";
  const busy = verify.isPending || enroll.isPending;

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("panels.identity.title")}</CardTitle>
        <p className="text-[11px] text-muted-foreground">{t("panels.identity.subtitle")}</p>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* DPDP note — always visible */}
        <div
          className="rounded-md border px-3 py-2 text-[11px]"
          style={{ borderColor: `${STATUS.info}80`, backgroundColor: `${STATUS.info}1a` }}
        >
          {t("panels.identity.dpdpNote")}
        </div>

        {galleryQ.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner /> {t("common.loading")}
          </div>
        ) : gallery.length === 0 ? (
          <EmptyState>{t("panels.identity.empty")}</EmptyState>
        ) : (
          <>
            {/* Driver selector */}
            <label className="block text-[11px] text-muted-foreground">
              {t("panels.identity.driver")}
              <select
                value={selected}
                onChange={(e) => setDriverId(e.target.value)}
                className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 text-xs"
              >
                {gallery.map((d) => (
                  <option key={d.driver_id} value={d.driver_id}>
                    {d.name} · {d.license_no}
                  </option>
                ))}
              </select>
            </label>

            {/* Live camera preview + face guide overlay */}
            <div className="relative aspect-[4/3] w-full overflow-hidden rounded-lg border border-border bg-black">
              <video
                ref={cam.videoRef}
                muted
                playsInline
                className="h-full w-full -scale-x-100 object-cover"
              />
              {/* Face guide */}
              {live && (
                <div
                  className="pointer-events-none absolute left-1/2 top-1/2 h-[72%] w-[52%] -translate-x-1/2 -translate-y-1/2 rounded-[50%] border-2 border-white/70 shadow-[0_0_0_9999px_rgba(0,0,0,0.28)]"
                  aria-hidden
                />
              )}
              {/* Idle / permission states */}
              {!live && (
                <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-center text-xs text-white/80">
                  {cam.status === "requesting" ? (
                    <>
                      <Spinner /> {t("identityPanel.startingCamera")}
                    </>
                  ) : cam.status === "denied" ? (
                    <>
                      <CameraOff className="h-7 w-7 opacity-70" />
                      {t("identityPanel.permissionDenied")}
                    </>
                  ) : cam.status === "error" ? (
                    <>
                      <CameraOff className="h-7 w-7 opacity-70" />
                      {cam.error ?? t("identityPanel.cameraUnavailable")}
                    </>
                  ) : (
                    <>
                      <Camera className="h-7 w-7 opacity-70" />
                      {t("identityPanel.cameraOff")}
                    </>
                  )}
                </div>
              )}
              {/* Status chip */}
              <span className="absolute left-2 top-2 inline-flex items-center gap-1 rounded-full bg-black/55 px-2 py-0.5 text-[10px] font-medium text-white">
                <span
                  className="h-1.5 w-1.5 rounded-full"
                  style={{ backgroundColor: live ? STATUS.ok : STATUS.unknown }}
                />
                {live ? t("identityPanel.live") : t("identityPanel.off")}
              </span>
            </div>

            {/* Camera + verify controls */}
            <div className="flex flex-wrap items-center gap-2">
              {!live ? (
                <Button
                  size="sm"
                  onClick={() => void cam.start()}
                  disabled={cam.status === "requesting"}
                >
                  <Camera className="h-4 w-4" /> {t("identityPanel.startCamera")}
                </Button>
              ) : (
                <>
                  <Button size="sm" onClick={() => void onVerify()} disabled={busy || !selected}>
                    {verify.isPending ? <Spinner /> : <ScanFace className="h-4 w-4" />}
                    {t("identityPanel.captureVerify")}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => void onEnroll()}
                    disabled={busy || !selected}
                  >
                    {enroll.isPending ? <Spinner /> : <UserCheck className="h-4 w-4" />}
                    {t("identityPanel.enrolReference")}
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => cam.stop()} disabled={busy}>
                    <CameraOff className="h-4 w-4" /> {t("identityPanel.stop")}
                  </Button>
                </>
              )}
            </div>

            {/* Validation / enrollment notice */}
            {notice && (
              <div
                className="flex items-start gap-1.5 rounded-md border px-3 py-2 text-[11px]"
                style={{
                  borderColor: `${notice.kind === "ok" ? STATUS.ok : STATUS.warning}80`,
                  backgroundColor: `${notice.kind === "ok" ? STATUS.ok : STATUS.warning}1a`,
                }}
              >
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                {notice.text}
              </div>
            )}

            {verify.data && (
              <VerifyResult
                result={verify.data}
                driverName={selectedDriver?.name}
                vehicle={undefined}
                at={verifiedAt}
              />
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

function VerifyResult({
  result,
  driverName,
  vehicle,
  at,
}: {
  result: IdentityVerifyResult;
  driverName?: string;
  vehicle?: string;
  at: string | null;
}) {
  const { t } = useTranslation();
  const colour = decisionColour(result.decision);
  const matchPct = `${(result.score * 100).toFixed(1)}%`;
  const conf = confidenceBand(result.score);
  const providerLabel =
    result.provider === "onnx" ? "ArcFace (ONNX)" : t("identityPanel.synthetic");

  return (
    <div className="space-y-2.5 rounded-lg border border-border bg-background p-3">
      <div className="flex items-center justify-between">
        <Badge colour={colour}>{result.decision}</Badge>
        <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
          {providerLabel}
        </span>
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
        <Field k={t("identityPanel.driverName")} v={driverName ?? "—"} />
        <Field k={t("identityPanel.driverId")} v={result.driver_id} mono />
        <Field k={t("identityPanel.vehicleNumber")} v={vehicle ?? "—"} mono />
        <Field k={t("identityPanel.matchScore")} v={matchPct} />
        <Field k={t("identityPanel.confidence")}>
          <span className="inline-flex items-center gap-1.5 font-medium">
            <span className="h-2 w-2 rounded-full" style={{ backgroundColor: conf.colour }} />
            {t(conf.labelKey)}
          </span>
        </Field>
        <Field k={t("identityPanel.status")} v={result.decision} />
        <Field k={t("identityPanel.timestamp")} v={at ? fmtDateTimeIST(at) : "—"} />
      </dl>

      {result.reason && <p className="text-[11px] text-muted-foreground">{result.reason}</p>}

      {result.decision === "PROVISIONAL" && (
        <div
          className="rounded-md border px-2 py-1.5 text-[11px]"
          style={{ borderColor: `${STATUS.warning}80` }}
        >
          {t("identityPanel.cureWindow")} {result.cure_window_h ?? 24}{" "}
          {t("identityPanel.hoursShort")}
          {result.provisional_until && (
            <span className="block text-muted-foreground">
              {t("identityPanel.provisionalUntil")} {fmtDateTimeIST(result.provisional_until)}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function Field({
  k,
  v,
  mono,
  children,
}: {
  k: string;
  v?: React.ReactNode;
  mono?: boolean;
  children?: React.ReactNode;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-[10px] uppercase tracking-wide text-muted-foreground">{k}</dt>
      <dd className={`truncate text-foreground ${mono ? "font-mono" : ""}`}>{children ?? v}</dd>
    </div>
  );
}

export default IdentityPanel;
