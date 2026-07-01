import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Camera,
  CameraOff,
  CheckCircle2,
  FileDown,
  Image as ImageIcon,
  ReceiptText,
  ScanLine,
  Send,
  Video as VideoIcon,
  Zap,
} from "lucide-react";
import { getAdapter } from "@/data";
import type { ViolationDetectResult, ViolationEnforceResult, ViolationIncident } from "@/lib/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/misc";
import { STATUS } from "@/lib/tokens";
import { fmtDateTimeIST } from "@/lib/utils";
import { useWebcam } from "@/hooks/useWebcam";

// Vehicle Violation Detection (Reports-page enforcement console). Orchestration
// only — the panel captures ONE frame (uploaded image, a frame grabbed from an
// uploaded video, or a live-camera snapshot), sends it to /api/violations/detect
// (which reuses ANPR + vehicle_master + the driver store), lets the operator
// confirm the applicable violation(s) and their fines (reports e-Challan
// schedule), then /api/violations/commit writes one jnpa.alerts incident per
// kind so the case appears on this very Reports page and its PDF export.

const GATES = ["G-NSICT", "G-JNPCT", "G-NSIGT", "G-BMCT"];

type CaptureMode = "image" | "video" | "camera";

/** Draw the current video frame to a JPEG blob (client-side, no model in UI). */
function frameToBlob(video: HTMLVideoElement): Promise<Blob | null> {
  return new Promise((resolve) => {
    if (!video.videoWidth) return resolve(null);
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const ctx = canvas.getContext("2d");
    if (!ctx) return resolve(null);
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    canvas.toBlob((b) => resolve(b), "image/jpeg", 0.9);
  });
}

function fmtInr(v?: number | null): string {
  return v == null ? "—" : `₹${v.toLocaleString("en-IN")}`;
}

export function ViolationDetectionPanel() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const cam = useWebcam();

  const [mode, setMode] = useState<CaptureMode>("image");
  const [gate, setGate] = useState<string>("");
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [captureBlob, setCaptureBlob] = useState<Blob | null>(null);
  const [plate, setPlate] = useState<string>("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [notice, setNotice] = useState<{ kind: "warn" | "ok"; text: string } | null>(null);

  const imageInputRef = useRef<HTMLInputElement>(null);
  const videoInputRef = useRef<HTMLInputElement>(null);
  const uploadedVideoRef = useRef<HTMLVideoElement>(null);
  // Canvas for the annotated frame (red plate box) + the cropped plate image.
  const annotatedRef = useRef<HTMLCanvasElement>(null);
  const [cropUrl, setCropUrl] = useState<string | null>(null);

  const detect = useMutation({
    mutationFn: (blob: Blob) => getAdapter().violationDetect(blob, gate || undefined),
    onSuccess: (res: ViolationDetectResult) => {
      setPlate(res.plate ?? "");
      setSelected(new Set());
      setNotice(
        res.degraded
          ? {
              kind: "warn",
              text: t("violations.degradedNote", {
                defaultValue: "ANPR ran in fallback mode — verify the plate before filing.",
              }),
            }
          : null,
      );
    },
    onError: (e: any) =>
      setNotice({ kind: "warn", text: String(e?.message ?? "detection failed") }),
  });

  const commit = useMutation({
    // issue=true → Generate Challan / Send to Police; issue=false → Save Case
    // (stops the lifecycle at CONFIRMED, no challan minted).
    mutationFn: (issue: boolean) =>
      getAdapter().violationCommit({
        case_id: detect.data?.case_id,
        plate: plate || detect.data?.plate || null,
        gate_id: gate || detect.data?.gate_id || null,
        evidence_url: detect.data?.evidence_url ?? null,
        evidence_sha256: detect.data?.evidence_sha256 ?? null,
        confidence: detect.data?.confidence ?? null,
        driver_id: detect.data?.driver?.driver_id ?? null,
        vehicle_class: detect.data?.vehicle_class ?? null,
        issue_challan: issue,
        violations: Array.from(selected),
      }),
    onSuccess: (inc: ViolationIncident) => {
      setNotice({
        kind: "ok",
        text: t("violations.filedNote", {
          defaultValue: "Incident filed — now visible in Traffic-Police Reports.",
          count: inc.alert_ids.length,
        }),
      });
      // The Reports table polls every 10 s; invalidate so it refreshes instantly.
      void qc.invalidateQueries({ queryKey: ["police"] });
    },
    onError: (e: any) => setNotice({ kind: "warn", text: String(e?.message ?? "commit failed") }),
  });

  // Fully-automatic pipeline: one click → ANPR → case → challan → notification.
  const enforce = useMutation({
    mutationFn: () => {
      if (!captureBlob) throw new Error("no frame captured");
      return getAdapter().violationEnforce(captureBlob, { gateId: gate || undefined });
    },
    onSuccess: () => {
      setNotice({
        kind: "ok",
        text: t("violations.challanGenerated", { defaultValue: "Challan Generated Successfully" }),
      });
      void qc.invalidateQueries({ queryKey: ["police"] });
    },
    onError: (e: any) => setNotice({ kind: "warn", text: String(e?.message ?? "enforce failed") }),
  });

  const detection = detect.data ?? null;
  const incident = commit.data ?? null;
  const enforced: ViolationEnforceResult | null = enforce.data ?? null;
  const catalog = useMemo(() => detection?.available_violations ?? [], [detection]);

  // Real ANPR (LIVE) vs synthetic fallback — drives the explicit source label.
  const realAnpr = !!(detection?.anpr_real ?? detection?.anpr_decision_path === "LIVE");
  const enforcedReal = !!(enforced?.anpr_real ?? enforced?.anpr_decision_path === "LIVE");

  const fineTotal = useMemo(
    () => catalog.filter((v) => selected.has(v.kind)).reduce((a, v) => a + (v.fine_inr ?? 0), 0),
    [catalog, selected],
  );

  // Draw the red plate box on the uploaded frame and crop the "Detected Plate"
  // whenever a detection (with a real ANPR bbox) lands.
  useEffect(() => {
    setCropUrl(null);
    if (!previewUrl || !detection) return;
    const bbox = detection.bbox;
    const img = new Image();
    img.onload = () => {
      const cv = annotatedRef.current;
      if (cv) {
        cv.width = img.naturalWidth;
        cv.height = img.naturalHeight;
        const ctx = cv.getContext("2d");
        if (ctx) {
          ctx.drawImage(img, 0, 0);
          if (bbox && bbox.length === 4) {
            const [x1, y1, x2, y2] = bbox;
            ctx.lineWidth = Math.max(3, Math.round(img.naturalWidth * 0.006));
            ctx.strokeStyle = "#ef4444"; // red
            ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
          }
        }
      }
      if (bbox && bbox.length === 4) {
        const [x1, y1, x2, y2] = bbox;
        const w = Math.max(1, x2 - x1);
        const h = Math.max(1, y2 - y1);
        const crop = document.createElement("canvas");
        crop.width = w;
        crop.height = h;
        const cctx = crop.getContext("2d");
        if (cctx) {
          cctx.drawImage(img, x1, y1, w, h, 0, 0, w, h);
          setCropUrl(crop.toDataURL("image/jpeg", 0.92));
        }
      }
    };
    img.src = previewUrl;
  }, [detection, previewUrl]);

  function resetCapture() {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    setPreviewUrl(null);
    setCaptureBlob(null);
    detect.reset();
    commit.reset();
    enforce.reset();
    setSelected(new Set());
    setPlate("");
    setNotice(null);
  }

  function setCaptured(blob: Blob) {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    setCaptureBlob(blob);
    setPreviewUrl(URL.createObjectURL(blob));
    detect.reset();
    commit.reset();
    enforce.reset();
    setSelected(new Set());
    setNotice(null);
  }

  function onPickMode(next: CaptureMode) {
    if (next === mode) return;
    if (cam.status === "live") cam.stop();
    setMode(next);
    resetCapture();
  }

  function onImageChosen(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (file) setCaptured(file);
  }

  function onVideoChosen(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (file && uploadedVideoRef.current) {
      uploadedVideoRef.current.src = URL.createObjectURL(file);
    }
  }

  async function grabFromUploadedVideo() {
    const v = uploadedVideoRef.current;
    if (!v) return;
    const blob = await frameToBlob(v);
    if (blob) setCaptured(blob);
    else
      setNotice({
        kind: "warn",
        text: t("violations.frameFailed", {
          defaultValue: "Could not grab a frame — let the video load and try again.",
        }),
      });
  }

  async function grabFromCamera() {
    const v = cam.videoRef.current;
    if (!v) return;
    const blob = await frameToBlob(v);
    if (blob) setCaptured(blob);
    else
      setNotice({
        kind: "warn",
        text: t("violations.frameFailed", {
          defaultValue: "Could not grab a frame from the camera.",
        }),
      });
  }

  function toggle(kind: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  }

  async function exportEvidencePdf(id?: string) {
    const target = id ?? incident?.alert_ids[0];
    if (!target) return;
    try {
      await getAdapter().downloadPolicePdf({ id: target });
    } catch {
      setNotice({
        kind: "warn",
        text: t("violations.pdfFailed", {
          defaultValue: "Could not export the evidence PDF.",
        }),
      });
    }
  }

  const live = cam.status === "live";
  const busy = detect.isPending || commit.isPending || enforce.isPending;
  const canDetect = !!captureBlob && !busy;
  const canCommit = !!detection && selected.size > 0 && !busy && !incident;
  const canEnforce = !!captureBlob && !busy && !enforced;

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          {t("violations.title", { defaultValue: "Vehicle Violation Detection" })}
        </CardTitle>
        <p className="text-[11px] text-muted-foreground">
          {t("violations.subtitle", {
            defaultValue:
              "ANPR → vehicle/driver lookup → rule engine → e-Challan · files to Police Reports",
          })}
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* ---- Upload section ---- */}
        <div className="flex flex-wrap items-center gap-2">
          <ModeButton
            active={mode === "image"}
            onClick={() => onPickMode("image")}
            icon={<ImageIcon className="h-4 w-4" />}
          >
            {t("violations.uploadImage", { defaultValue: "Upload Image" })}
          </ModeButton>
          <ModeButton
            active={mode === "video"}
            onClick={() => onPickMode("video")}
            icon={<VideoIcon className="h-4 w-4" />}
          >
            {t("violations.uploadVideo", { defaultValue: "Upload Video" })}
          </ModeButton>
          <ModeButton
            active={mode === "camera"}
            onClick={() => onPickMode("camera")}
            icon={<Camera className="h-4 w-4" />}
          >
            {t("violations.liveCamera", { defaultValue: "Live Camera" })}
          </ModeButton>

          <label className="ml-auto text-[11px] text-muted-foreground">
            {t("violations.gate", { defaultValue: "Gate (optional)" })}
            <select
              value={gate}
              onChange={(e) => setGate(e.target.value)}
              className="ml-1 rounded-md border border-border bg-background px-2 py-1 text-xs"
            >
              <option value="">{t("reports.all", { defaultValue: "All" })}</option>
              {GATES.map((g) => (
                <option key={g} value={g}>
                  {g.replace("G-", "")}
                </option>
              ))}
            </select>
          </label>
        </div>

        {/* ---- Capture surface ---- */}
        <div className="relative aspect-[4/3] w-full overflow-hidden rounded-lg border border-border bg-black">
          {/* Preview of the captured frame takes precedence once we have one. */}
          {previewUrl ? (
            <img src={previewUrl} alt="capture" className="h-full w-full object-contain" />
          ) : mode === "camera" ? (
            <>
              <video ref={cam.videoRef} muted playsInline className="h-full w-full object-cover" />
              {!live && (
                <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-center text-xs text-white/80">
                  {cam.status === "requesting" ? (
                    <>
                      <Spinner />{" "}
                      {t("violations.startingCamera", { defaultValue: "Starting camera…" })}
                    </>
                  ) : cam.status === "denied" ? (
                    <>
                      <CameraOff className="h-7 w-7 opacity-70" />{" "}
                      {t("violations.permissionDenied", {
                        defaultValue: "Camera permission denied",
                      })}
                    </>
                  ) : (
                    <>
                      <Camera className="h-7 w-7 opacity-70" />{" "}
                      {t("violations.cameraOff", { defaultValue: "Camera is off" })}
                    </>
                  )}
                </div>
              )}
            </>
          ) : mode === "video" ? (
            <video
              ref={uploadedVideoRef}
              muted
              playsInline
              controls
              className="h-full w-full object-contain"
            />
          ) : (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-center text-xs text-white/70">
              <ImageIcon className="h-7 w-7 opacity-70" />
              {t("violations.chooseImage", { defaultValue: "Choose an image to analyse" })}
            </div>
          )}
        </div>

        {/* Hidden native file pickers */}
        <input ref={imageInputRef} type="file" accept="image/*" hidden onChange={onImageChosen} />
        <input ref={videoInputRef} type="file" accept="video/*" hidden onChange={onVideoChosen} />

        {/* ---- Capture controls ---- */}
        <div className="flex flex-wrap items-center gap-2">
          {mode === "image" && (
            <Button size="sm" variant="outline" onClick={() => imageInputRef.current?.click()}>
              <ImageIcon className="h-4 w-4" />{" "}
              {t("violations.chooseFile", { defaultValue: "Choose image" })}
            </Button>
          )}
          {mode === "video" && (
            <>
              <Button size="sm" variant="outline" onClick={() => videoInputRef.current?.click()}>
                <VideoIcon className="h-4 w-4" />{" "}
                {t("violations.chooseVideo", { defaultValue: "Choose video" })}
              </Button>
              <Button size="sm" variant="outline" onClick={() => void grabFromUploadedVideo()}>
                <ScanLine className="h-4 w-4" />{" "}
                {t("violations.grabFrame", { defaultValue: "Capture frame" })}
              </Button>
            </>
          )}
          {mode === "camera" && (
            <>
              {!live ? (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => void cam.start()}
                  disabled={cam.status === "requesting"}
                >
                  <Camera className="h-4 w-4" />{" "}
                  {t("violations.startCamera", { defaultValue: "Start camera" })}
                </Button>
              ) : (
                <>
                  <Button size="sm" variant="outline" onClick={() => void grabFromCamera()}>
                    <ScanLine className="h-4 w-4" />{" "}
                    {t("violations.grabFrame", { defaultValue: "Capture frame" })}
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => cam.stop()}>
                    <CameraOff className="h-4 w-4" />{" "}
                    {t("violations.stop", { defaultValue: "Stop" })}
                  </Button>
                </>
              )}
            </>
          )}

          <Button
            size="sm"
            variant="outline"
            onClick={() => captureBlob && detect.mutate(captureBlob)}
            disabled={!canDetect}
          >
            {detect.isPending ? <Spinner /> : <ScanLine className="h-4 w-4" />}
            {t("violations.detect", { defaultValue: "Run detection" })}
          </Button>
          {/* One-click automatic pipeline — no manual confirm step. */}
          <Button size="sm" onClick={() => enforce.mutate()} disabled={!canEnforce}>
            {enforce.isPending ? <Spinner /> : <Zap className="h-4 w-4" />}
            {t("violations.autoEnforce", { defaultValue: "Auto-Enforce" })}
          </Button>
          {(previewUrl || detection || enforced) && (
            <Button size="sm" variant="ghost" onClick={resetCapture} disabled={busy}>
              {t("violations.reset", { defaultValue: "Reset" })}
            </Button>
          )}
        </div>

        {notice && (
          <div
            className="flex items-start gap-1.5 rounded-md border px-3 py-2 text-[11px]"
            style={{
              borderColor: `${notice.kind === "ok" ? STATUS.ok : STATUS.warning}80`,
              backgroundColor: `${notice.kind === "ok" ? STATUS.ok : STATUS.warning}1a`,
            }}
          >
            {notice.kind === "ok" ? (
              <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            ) : (
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            )}
            {notice.text}
          </div>
        )}

        {/* ---- Detection output ---- */}
        {detection && (
          <div className="space-y-3 rounded-lg border border-border bg-background p-3">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                {t("violations.detectionOutput", { defaultValue: "Detection output" })}
              </span>
              <Badge colour={realAnpr ? STATUS.ok : STATUS.warning}>
                {realAnpr ? "REAL ANPR" : "SYNTHETIC"}
              </Badge>
            </div>

            {/* Source — real ANPR vs synthetic fallback (never silent). */}
            <div
              className="flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-[11px]"
              style={{
                borderColor: `${realAnpr ? STATUS.ok : STATUS.warning}80`,
                backgroundColor: `${realAnpr ? STATUS.ok : STATUS.warning}1a`,
              }}
            >
              <span
                className="h-1.5 w-1.5 rounded-full"
                style={{ backgroundColor: realAnpr ? STATUS.ok : STATUS.warning }}
              />
              {realAnpr
                ? detection.degraded
                  ? t("violations.sourceRealDegraded", {
                      defaultValue: "Source: Real ANPR (degraded OCR — low confidence)",
                    })
                  : t("violations.sourceReal", { defaultValue: "Source: Real ANPR pipeline" })
                : t("violations.sourceSynthetic", {
                    defaultValue:
                      "Source: Synthetic fallback — ANPR service unavailable. Not a real OCR read.",
                  })}
            </div>

            {/* Annotated frame (red plate box) + cropped Detected Plate. */}
            <div className="flex gap-3">
              <div className="min-w-0 flex-1">
                <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
                  {t("violations.annotated", { defaultValue: "Annotated frame" })}
                </div>
                <canvas ref={annotatedRef} className="w-full rounded-md border border-border" />
              </div>
              <div className="w-28 shrink-0">
                <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
                  {t("violations.detectedPlate", { defaultValue: "Detected Plate" })}
                </div>
                {cropUrl ? (
                  <img
                    src={cropUrl}
                    alt="detected plate"
                    className="w-full rounded-md border-2 border-severity-critical/70"
                  />
                ) : (
                  <div className="rounded-md border border-dashed border-border px-2 py-3 text-center text-[10px] text-muted-foreground">
                    {realAnpr
                      ? t("violations.noPlateBox", { defaultValue: "No plate region detected" })
                      : t("violations.synthNoBox", { defaultValue: "n/a (synthetic)" })}
                  </div>
                )}
              </div>
            </div>

            <div className="grid grid-cols-2 gap-x-4 gap-y-3">
              <label className="min-w-0">
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                  {t("violations.plate", { defaultValue: "Vehicle number (OCR)" })}
                </div>
                <input
                  value={plate}
                  onChange={(e) => setPlate(e.target.value.toUpperCase())}
                  className="mt-0.5 w-full rounded-md border border-border bg-background px-2 py-1 font-mono text-sm"
                />
              </label>
              <Field
                k={t("violations.confidence", { defaultValue: "OCR confidence" })}
                v={
                  detection.confidence != null ? `${(detection.confidence * 100).toFixed(1)}%` : "—"
                }
              />
              {detection.vehicle ? (
                <>
                  <Field
                    k={t("violations.owner", { defaultValue: "Owner (masked)" })}
                    v={detection.vehicle.owner_name_masked ?? "—"}
                  />
                  <Field
                    k={t("violations.vehicleClass", { defaultValue: "Vehicle class" })}
                    v={detection.vehicle.vehicle_class ?? "—"}
                  />
                </>
              ) : (
                <div className="col-span-2 rounded-md border border-severity-warning/40 bg-severity-warning/10 px-3 py-2 text-xs font-semibold text-severity-warning">
                  {realAnpr
                    ? t("violations.vehicleNotFound", {
                        defaultValue: "Vehicle Not Found — plate not in vehicle_master",
                      })
                    : t("violations.vehicleSyntheticSkip", {
                        defaultValue: "Vehicle lookup skipped — synthetic read (ANPR unavailable)",
                      })}
                </div>
              )}
            </div>

            {/* Driver info — only when a vehicle actually matched. */}
            {detection.vehicle && (
              <div className="rounded-md border border-border/70 px-3 py-2">
                <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
                  {t("violations.driverInfo", { defaultValue: "Driver (mapped via vehicle)" })}
                </div>
                {detection.driver ? (
                  <div className="flex items-center justify-between text-sm">
                    <span>
                      {detection.driver.name ?? "—"}{" "}
                      <span className="font-mono text-xs text-muted-foreground">
                        {detection.driver.driver_id}
                      </span>
                    </span>
                    <Badge
                      colour={detection.driver.status === "ACTIVE" ? STATUS.ok : STATUS.warning}
                    >
                      {detection.driver.status ?? "—"}
                    </Badge>
                  </div>
                ) : (
                  <div className="text-xs text-muted-foreground">
                    {t("violations.noDriver", {
                      defaultValue: "No enrolled driver mapped to this vehicle.",
                    })}
                  </div>
                )}
              </div>
            )}

            {/* Violation selection (rule engine fines) */}
            <div>
              <div className="mb-1.5 text-[10px] uppercase tracking-wide text-muted-foreground">
                {t("violations.detectedViolations", { defaultValue: "Applicable violations" })}
              </div>
              <div className="space-y-1.5">
                {catalog.map((v) => (
                  <label
                    key={v.kind}
                    className="flex cursor-pointer items-center justify-between rounded-md border border-border/70 px-3 py-2 text-sm hover:bg-muted/40"
                  >
                    <span className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={selected.has(v.kind)}
                        onChange={() => toggle(v.kind)}
                      />
                      <span>{v.label}</span>
                      <span className="font-mono text-[10px] text-muted-foreground">{v.kind}</span>
                    </span>
                    <span className="font-mono text-xs text-muted-foreground">
                      {fmtInr(v.fine_inr)}
                    </span>
                  </label>
                ))}
              </div>
            </div>

            {/* Fine summary */}
            <div className="flex items-center justify-between rounded-md border border-severity-warning/40 bg-severity-warning/10 px-3 py-2">
              <span className="flex items-center gap-1.5 text-xs font-semibold">
                <ReceiptText className="h-4 w-4 text-severity-warning" />
                {t("violations.totalFine", { defaultValue: "Total fine" })}
              </span>
              <span className="font-mono text-base font-semibold text-severity-critical">
                {fmtInr(fineTotal)}
              </span>
            </div>
          </div>
        )}

        {/* ---- Actions ---- */}
        {detection && (
          <div className="flex flex-wrap items-center gap-2">
            <Button onClick={() => commit.mutate(true)} disabled={!canCommit}>
              {commit.isPending ? <Spinner /> : <ReceiptText className="h-4 w-4" />}
              {t("violations.generateChallan", { defaultValue: "Generate Challan" })}
            </Button>
            <Button variant="outline" onClick={() => commit.mutate(false)} disabled={!canCommit}>
              <CheckCircle2 className="h-4 w-4" />{" "}
              {t("violations.saveCase", { defaultValue: "Save Case" })}
            </Button>
            <Button variant="outline" onClick={() => commit.mutate(true)} disabled={!canCommit}>
              <Send className="h-4 w-4" />{" "}
              {t("violations.sendToPolice", { defaultValue: "Send to Police" })}
            </Button>
            <Button variant="outline" onClick={() => void exportEvidencePdf()} disabled={!incident}>
              <FileDown className="h-4 w-4" />{" "}
              {t("violations.exportEvidence", { defaultValue: "Export Evidence PDF" })}
            </Button>
          </div>
        )}

        {/* ---- Filed incident summary (case + challan + lifecycle) ---- */}
        {incident && (
          <div className="space-y-2 rounded-lg border border-severity-info/40 bg-severity-info/5 p-3 text-xs">
            <div className="flex items-center justify-between">
              <span className="flex items-center gap-1.5 font-semibold text-severity-info">
                <CheckCircle2 className="h-4 w-4" />
                {t("violations.caseFiled", { defaultValue: "Case filed" })}
              </span>
              {incident.status && <Badge colour={STATUS.ok}>{incident.status}</Badge>}
            </div>

            <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5">
              <Field
                k={t("violations.caseId", { defaultValue: "Case ID" })}
                v={<span className="font-mono text-[11px]">{incident.case_id}</span>}
              />
              {incident.challan_no && (
                <Field
                  k={t("violations.challanNo", { defaultValue: "Challan No." })}
                  v={<span className="font-mono text-[11px]">{incident.challan_no}</span>}
                />
              )}
              <Field
                k={t("violations.totalFine", { defaultValue: "Total fine" })}
                v={fmtInr(incident.fine_total)}
              />
              <Field
                k={t("violations.filedAt", { defaultValue: "Filed at" })}
                v={fmtDateTimeIST(incident.timestamp)}
              />
            </dl>

            {/* Lifecycle stepper — highlights the case's current state. */}
            <CaseLifecycle status={incident.status} />

            {incident.evidence_sha256 && (
              <div
                className="truncate font-mono text-[10px] text-muted-foreground"
                title={incident.evidence_sha256}
              >
                {t("violations.evidenceHash", { defaultValue: "evidence sha256" })}:{" "}
                {incident.evidence_sha256}
              </div>
            )}
            {incident.skipped && incident.skipped.length > 0 && (
              <div className="text-[10px] text-muted-foreground">
                {t("violations.dedup", {
                  defaultValue: "Skipped {{n}} duplicate violation(s) already on this case.",
                  n: incident.skipped.length,
                })}
              </div>
            )}
          </div>
        )}

        {/* ---- Automatic-pipeline result (Auto-Enforce) ---- */}
        {enforced && (
          <div className="space-y-3 rounded-lg border border-severity-info/40 bg-severity-info/5 p-3 text-xs">
            <div className="flex items-center justify-between">
              <span className="flex items-center gap-1.5 font-semibold text-severity-info">
                <CheckCircle2 className="h-4 w-4" />
                {t("violations.challanGenerated", {
                  defaultValue: "Challan Generated Successfully",
                })}
              </span>
              <span className="flex items-center gap-2">
                <Badge colour={enforcedReal ? STATUS.ok : STATUS.warning}>
                  {enforcedReal ? "REAL ANPR" : "SYNTHETIC"}
                </Badge>
                {enforced.status && <Badge colour={STATUS.ok}>{enforced.status}</Badge>}
              </span>
            </div>

            <div
              className="text-[10px]"
              style={{ color: enforcedReal ? STATUS.ok : STATUS.warning }}
            >
              {enforcedReal
                ? t("violations.sourceReal", { defaultValue: "Source: Real ANPR pipeline" })
                : t("violations.sourceSynthetic", {
                    defaultValue: "Source: Synthetic fallback — ANPR service unavailable.",
                  })}
            </div>

            <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5">
              <Field
                k={t("violations.plate", { defaultValue: "Vehicle number" })}
                v={<span className="font-mono">{enforced.plate ?? "—"}</span>}
              />
              <Field
                k={t("violations.confidence", { defaultValue: "Confidence" })}
                v={enforced.confidence != null ? `${(enforced.confidence * 100).toFixed(1)}%` : "—"}
              />
              {enforced.vehicle ? (
                <>
                  <Field
                    k={t("violations.owner", { defaultValue: "Owner (masked)" })}
                    v={enforced.vehicle.owner_name_masked ?? "—"}
                  />
                  <Field
                    k={t("violations.driverInfo", { defaultValue: "Driver" })}
                    v={enforced.driver?.name ?? "—"}
                  />
                </>
              ) : (
                <div className="col-span-2 text-[11px] font-medium text-severity-warning">
                  {enforcedReal
                    ? t("violations.vehicleNotFound", {
                        defaultValue: "Vehicle Not Found — plate not in vehicle_master",
                      })
                    : t("violations.vehicleSyntheticSkip", {
                        defaultValue: "Vehicle lookup skipped — synthetic read",
                      })}
                </div>
              )}
              <Field
                k={t("violations.caseId", { defaultValue: "Case ID" })}
                v={<span className="font-mono text-[11px]">{enforced.case_id}</span>}
              />
              {enforced.challan_no && (
                <Field
                  k={t("violations.challanNo", { defaultValue: "Challan No." })}
                  v={<span className="font-mono text-[11px]">{enforced.challan_no}</span>}
                />
              )}
            </dl>

            <div className="space-y-1">
              {enforced.violations.map((v) => (
                <div
                  key={v.kind}
                  className="flex items-center justify-between rounded-md border border-border/70 px-3 py-1.5"
                >
                  <span>
                    {v.label}{" "}
                    <span className="font-mono text-[10px] text-muted-foreground">{v.kind}</span>
                  </span>
                  <span className="font-mono text-xs text-muted-foreground">
                    {fmtInr(v.fine_inr)}
                  </span>
                </div>
              ))}
            </div>

            <div className="flex items-center justify-between rounded-md border border-severity-warning/40 bg-severity-warning/10 px-3 py-2">
              <span className="flex items-center gap-1.5 text-xs font-semibold">
                <ReceiptText className="h-4 w-4 text-severity-warning" />
                {t("violations.totalFine", { defaultValue: "Total fine" })}
              </span>
              <span className="font-mono text-base font-semibold text-severity-critical">
                {fmtInr(enforced.total_fine)}
              </span>
            </div>

            <CaseLifecycle status={enforced.status} />

            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={() => void exportEvidencePdf(enforced.alert_ids[0])}
                disabled={!enforced.alert_ids[0]}
              >
                <FileDown className="h-4 w-4" />{" "}
                {t("violations.exportEvidence", { defaultValue: "Export Evidence PDF" })}
              </Button>
              <span className="flex items-center gap-1 text-[10px] text-muted-foreground">
                <span
                  className="h-1.5 w-1.5 rounded-full"
                  style={{
                    backgroundColor: enforced.notification_sent ? STATUS.ok : STATUS.unknown,
                  }}
                />
                {enforced.notification_sent
                  ? t("violations.notified", { defaultValue: "real-time notification sent" })
                  : t("violations.notNotified", { defaultValue: "notification unavailable" })}
              </span>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ModeButton({
  active,
  onClick,
  icon,
  children,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <Button size="sm" variant={active ? "default" : "outline"} onClick={onClick}>
      {icon} {children}
    </Button>
  );
}

// Canonical lifecycle (mirrors the gateway state machine). The current state and
// everything before it are marked done; later states are pending.
const LIFECYCLE = ["DETECTED", "REVIEWED", "CONFIRMED", "CHALLAN_ISSUED", "PAID", "CLOSED"];

function CaseLifecycle({ status }: { status?: string }) {
  const idx = status ? LIFECYCLE.indexOf(status) : -1;
  return (
    <div className="flex flex-wrap items-center gap-1">
      {LIFECYCLE.map((s, i) => {
        const done = idx >= 0 && i <= idx;
        return (
          <span key={s} className="flex items-center gap-1">
            <span
              className="rounded px-1.5 py-0.5 text-[9px] font-medium"
              style={{
                backgroundColor: done ? `${STATUS.ok}26` : "transparent",
                color: done ? STATUS.ok : "var(--muted-foreground, #888)",
                border: `1px solid ${done ? STATUS.ok : "transparent"}40`,
              }}
            >
              {s}
            </span>
            {i < LIFECYCLE.length - 1 && (
              <span className="text-[9px] text-muted-foreground">›</span>
            )}
          </span>
        );
      })}
    </div>
  );
}

function Field({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{k}</div>
      <div className="truncate text-sm text-foreground">{v}</div>
    </div>
  );
}

export default ViolationDetectionPanel;
