// Vehicle Intelligence — Identity & Detection camera workflows.
//
// Two modal workflows launched from the RC card header on the Vehicle
// Intelligence page. Neither trusts the client for identity — the backend
// resolves vehicle -> active driver -> face enrollment.
//
//   Identity : capture a face -> POST /api/vehicle/{plate}/identity -> MATCHED?
//   Detection: upload/capture the plate image -> POST /api/vehicle/detection
//              -> detected number + match against the searched vehicle.
//
// The Detection dialog reuses the SAME capture pattern as the Reports-page
// Vehicle Violation Detection panel (Upload Image / Live Camera / preview /
// Analyze) via the shared useWebcam hook — no separate camera-only flow, no
// duplicated ANPR implementation (it calls the existing detection adapter).

import { useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  ScanFace,
  ShieldAlert,
  CheckCircle2,
  XCircle,
  RefreshCw,
  Camera,
  CameraOff,
  Image as ImageIcon,
  ScanLine,
} from "lucide-react";
import CameraCapture from "@/components/CameraCapture";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/misc";
import { useWebcam } from "@/hooks/useWebcam";
import { STATUS } from "@/lib/tokens";
import { getAdapter } from "@/data";
import type { VehicleDetectionResult, VehicleIdentityResult } from "@/lib/types";

function ResultBanner({ ok, title }: { ok: boolean; title: string }) {
  const colour = ok ? STATUS.ok : STATUS.critical;
  return (
    <div
      className="flex items-center gap-2 rounded-md border px-3 py-2 text-sm font-semibold"
      style={{ borderColor: `${colour}80`, backgroundColor: `${colour}1a`, color: colour }}
    >
      {ok ? <CheckCircle2 className="h-4 w-4" /> : <XCircle className="h-4 w-4" />}
      {title}
    </div>
  );
}

function ResultRow({ k, v }: { k: string; v?: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1 text-sm">
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">{k}</span>
      <span className="font-medium text-foreground">{v ?? "—"}</span>
    </div>
  );
}

// ------------------------------------------------------------------ Identity
export function VehicleIdentityDialog({
  vehicleNumber,
  open,
  onOpenChange,
}: {
  vehicleNumber: string;
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const [result, setResult] = useState<VehicleIdentityResult | null>(null);
  const verify = useMutation({
    mutationFn: (image: string) => getAdapter().vehicleIdentity(vehicleNumber, image),
    onSuccess: setResult,
  });

  const reset = () => {
    setResult(null);
    verify.reset();
  };
  const close = (o: boolean) => {
    if (!o) reset();
    onOpenChange(o);
  };

  const matched = result?.matched ?? false;
  // Map the driver_name from /api/vehicle/{n}/identity; fall back to the id.
  const driverName = result?.driver_name || result?.driver_id || "—";

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-w-md p-0">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ScanFace className="h-4 w-4 text-primary" /> Identity · {vehicleNumber}
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-3 p-4">
          {!result ? (
            <>
              <p className="text-xs text-muted-foreground">
                Capture the person&apos;s face to verify against the driver assigned to this
                vehicle.
              </p>
              <CameraCapture
                facing="user"
                busy={verify.isPending}
                busyLabel="Verifying identity…"
                captureLabel="Capture & Verify"
                onCapture={(img) => verify.mutate(img)}
              />
              {verify.isError && (
                <div className="text-xs text-red-500">{(verify.error as Error).message}</div>
              )}
            </>
          ) : (
            <>
              <ResultBanner
                ok={matched}
                title={matched ? "Identity Verified" : "Identity Not Matched"}
              />
              {matched ? (
                <div className="rounded-md border border-border p-2">
                  <ResultRow k="Driver Name" v={driverName} />
                  <ResultRow k="Vehicle" v={result?.vehicle_number || vehicleNumber} />
                  <ResultRow k="Confidence" v={`${result?.confidence ?? 0}%`} />
                  <ResultRow k="Status" v={result?.status} />
                </div>
              ) : (
                <div className="rounded-md border border-border p-2">
                  <ResultRow
                    k="Reason"
                    v={
                      result?.reason === "no_active_driver"
                        ? "No active driver linked"
                        : result?.reason === "vehicle_not_registered"
                          ? "Vehicle not registered"
                          : (result?.message ?? "Face mismatch")
                    }
                  />
                  {(result?.confidence ?? 0) > 0 && (
                    <ResultRow k="Confidence" v={`${result?.confidence}%`} />
                  )}
                </div>
              )}
              <button
                type="button"
                onClick={reset}
                className="inline-flex w-full items-center justify-center gap-1.5 rounded-md border border-border px-3 py-2 text-sm font-medium hover:bg-muted"
              >
                <RefreshCw className="h-4 w-4" /> Retry
              </button>
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ----------------------------------------------------------------- Detection
// Mirrors the Reports-page Vehicle Violation Detection capture pattern: choose an
// Upload Image or Live Camera source, preview the frame, then Analyze. The Analyze
// call goes to the existing /api/vehicle/detection adapter (ANPR + match vs the
// searched plate) — no duplicate detection engine.
type DetectMode = "image" | "camera";

export function VehicleDetectionDialog({
  vehicleNumber,
  open,
  onOpenChange,
}: {
  vehicleNumber: string;
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const cam = useWebcam();
  const [mode, setMode] = useState<DetectMode>("image");
  const [image, setImage] = useState<string | null>(null); // data-URL preview + payload
  const [result, setResult] = useState<VehicleDetectionResult | null>(null);
  const imageInputRef = useRef<HTMLInputElement>(null);

  const detect = useMutation({
    mutationFn: (img: string) => getAdapter().vehicleDetection(img, vehicleNumber),
    onSuccess: setResult,
  });

  const resetCapture = () => {
    setImage(null);
    setResult(null);
    detect.reset();
  };
  const close = (o: boolean) => {
    if (!o) {
      cam.stop();
      resetCapture();
    }
    onOpenChange(o);
  };
  const pickMode = (next: DetectMode) => {
    if (next === mode) return;
    if (cam.status === "live") cam.stop();
    setMode(next);
    resetCapture();
  };

  const onImageChosen = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      setImage(typeof reader.result === "string" ? reader.result : null);
      setResult(null);
      detect.reset();
    };
    reader.readAsDataURL(file);
  };

  const grabFromCamera = () => {
    const img = cam.capture();
    if (img) {
      setImage(img);
      setResult(null);
      detect.reset();
    }
  };

  const live = cam.status === "live";
  const matched = result?.match === true;

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-w-md p-0">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ShieldAlert className="h-4 w-4 text-primary" /> Detection · {vehicleNumber}
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-3 p-4">
          {/* ---- Source selector (Upload Image / Live Camera) ---- */}
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant={mode === "image" ? "default" : "outline"}
              onClick={() => pickMode("image")}
            >
              <ImageIcon className="h-4 w-4" /> Upload Image
            </Button>
            <Button
              size="sm"
              variant={mode === "camera" ? "default" : "outline"}
              onClick={() => pickMode("camera")}
            >
              <Camera className="h-4 w-4" /> Live Camera
            </Button>
          </div>

          {/* ---- Capture / preview surface ---- */}
          <div className="relative aspect-[4/3] w-full overflow-hidden rounded-lg border border-border bg-black">
            {image ? (
              <img src={image} alt="capture" className="h-full w-full object-contain" />
            ) : mode === "camera" ? (
              <>
                <video
                  ref={cam.videoRef}
                  muted
                  playsInline
                  className="h-full w-full object-cover"
                />
                {!live && (
                  <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-center text-xs text-white/80">
                    {cam.status === "requesting" ? (
                      <>
                        <Spinner /> Opening camera…
                      </>
                    ) : cam.status === "denied" ? (
                      <>
                        <CameraOff className="h-7 w-7 opacity-70" /> Camera permission denied
                      </>
                    ) : (
                      <>
                        <Camera className="h-7 w-7 opacity-70" /> Camera is off
                      </>
                    )}
                  </div>
                )}
              </>
            ) : (
              <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-center text-xs text-white/70">
                <ImageIcon className="h-7 w-7 opacity-70" /> Choose a vehicle image to analyse
              </div>
            )}
          </div>

          <input ref={imageInputRef} type="file" accept="image/*" hidden onChange={onImageChosen} />

          {/* ---- Capture controls ---- */}
          <div className="flex flex-wrap items-center gap-2">
            {mode === "image" && (
              <Button size="sm" variant="outline" onClick={() => imageInputRef.current?.click()}>
                <ImageIcon className="h-4 w-4" /> Choose image
              </Button>
            )}
            {mode === "camera" &&
              (!live ? (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => void cam.start()}
                  disabled={cam.status === "requesting"}
                >
                  <Camera className="h-4 w-4" /> Start camera
                </Button>
              ) : (
                <>
                  <Button size="sm" variant="outline" onClick={grabFromCamera}>
                    <ScanLine className="h-4 w-4" /> Capture frame
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => cam.stop()}>
                    <CameraOff className="h-4 w-4" /> Stop
                  </Button>
                </>
              ))}
            <Button
              size="sm"
              onClick={() => image && detect.mutate(image)}
              disabled={!image || detect.isPending}
            >
              {detect.isPending ? <Spinner /> : <ScanLine className="h-4 w-4" />} Analyze
            </Button>
            {(image || result) && (
              <Button size="sm" variant="ghost" onClick={resetCapture} disabled={detect.isPending}>
                Reset
              </Button>
            )}
          </div>

          {detect.isError && (
            <div className="text-xs text-red-500">{(detect.error as Error).message}</div>
          )}

          {/* ---- Detection result ---- */}
          {result && (
            <>
              <ResultBanner
                ok={matched}
                title={matched ? "Vehicle Detection" : "Vehicle mismatch detected"}
              />
              <div className="rounded-md border border-border p-2">
                {matched ? (
                  <>
                    <ResultRow k="Detected Vehicle" v={result.detected_vehicle} />
                    <ResultRow k="Confidence" v={`${result.confidence}%`} />
                    <ResultRow k="Match" v="Verified" />
                  </>
                ) : (
                  <>
                    <ResultRow k="Expected" v={vehicleNumber} />
                    <ResultRow k="Detected Vehicle" v={result.detected_vehicle} />
                    <ResultRow k="Confidence" v={`${result.confidence}%`} />
                  </>
                )}
              </div>
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
