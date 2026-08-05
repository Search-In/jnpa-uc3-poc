// CameraCapture — the single reusable browser-camera capture surface.
//
// Owns getUserMedia start/stop, a live <video> preview, front/back camera
// switching, and a frame capture() that emits a JPEG data-URL to the parent.
// Handles every state the workflows need: permission request ("Opening camera…"),
// permission denied, generic error with Retry, and a busy overlay while the
// parent processes a captured frame. Always releases the device on unmount.
//
// Both the Identity (face) and Detection (number-plate) workflows consume THIS
// component, so the camera code lives in exactly one place.

import { useCallback, useEffect, useRef, useState } from "react";
import { Camera, RefreshCw, SwitchCamera, AlertTriangle } from "lucide-react";
import { Spinner } from "@/components/ui/misc";

type Facing = "user" | "environment";
type Status = "idle" | "requesting" | "live" | "denied" | "error";

export interface CameraCaptureProps {
  /** Receives the captured frame as a JPEG data-URL. */
  onCapture: (dataUrl: string) => void;
  /** Parent is processing a captured frame — disables capture + shows an overlay. */
  busy?: boolean;
  /** Overlay caption while busy (e.g. "Verifying identity…"). */
  busyLabel?: string;
  /** Capture-button caption. */
  captureLabel?: string;
  /** Initial camera. Identity → "user" (front), Detection → "environment" (rear). */
  facing?: Facing;
}

export default function CameraCapture({
  onCapture,
  busy = false,
  busyLabel = "Processing…",
  captureLabel = "Capture",
  facing = "user",
}: CameraCaptureProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);
  const [facingMode, setFacingMode] = useState<Facing>(facing);

  const stop = useCallback(() => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    if (videoRef.current) videoRef.current.srcObject = null;
  }, []);

  const start = useCallback(
    async (mode: Facing) => {
      if (!navigator.mediaDevices?.getUserMedia) {
        setStatus("error");
        setError("Camera API is not available in this browser.");
        return;
      }
      stop();
      setStatus("requesting");
      setError(null);
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: mode, width: { ideal: 1280 }, height: { ideal: 720 } },
          audio: false,
        });
        streamRef.current = stream;
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          await videoRef.current.play().catch(() => {});
        }
        setStatus("live");
      } catch (e) {
        const name = (e as DOMException)?.name;
        setStatus(name === "NotAllowedError" || name === "SecurityError" ? "denied" : "error");
        setError((e as Error)?.message ?? "Could not start the camera.");
      }
    },
    [stop],
  );

  // Start on mount / whenever the chosen camera changes; release on unmount.
  useEffect(() => {
    void start(facingMode);
    return () => stop();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [facingMode]);

  const capture = useCallback(() => {
    const v = videoRef.current;
    if (!v || !v.videoWidth) return;
    const canvas = document.createElement("canvas");
    canvas.width = v.videoWidth;
    canvas.height = v.videoHeight;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.drawImage(v, 0, 0, canvas.width, canvas.height);
    onCapture(canvas.toDataURL("image/jpeg", 0.9));
  }, [onCapture]);

  const toggleFacing = () => setFacingMode((m) => (m === "user" ? "environment" : "user"));

  return (
    <div className="space-y-2">
      <div className="relative overflow-hidden rounded-lg border border-border bg-black/90 aspect-video">
        <video
          ref={videoRef}
          playsInline
          muted
          className="h-full w-full object-cover"
          style={{ transform: facingMode === "user" ? "scaleX(-1)" : undefined }}
        />

        {/* Requesting-permission state */}
        {status === "requesting" && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/70 text-xs text-white">
            <Spinner className="text-white" /> Opening camera…
          </div>
        )}

        {/* Denied / error state with retry */}
        {(status === "denied" || status === "error") && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/80 px-4 text-center text-xs text-white">
            <AlertTriangle className="h-6 w-6 text-amber-400" />
            <div>
              {status === "denied"
                ? "Camera permission was blocked. Allow camera access, then retry."
                : (error ?? "Could not start the camera.")}
            </div>
            <button
              type="button"
              onClick={() => void start(facingMode)}
              className="mt-1 inline-flex items-center gap-1.5 rounded-md bg-white/15 px-3 py-1.5 font-medium hover:bg-white/25"
            >
              <RefreshCw className="h-3.5 w-3.5" /> Retry
            </button>
          </div>
        )}

        {/* Busy overlay while the parent processes the frame */}
        {busy && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/70 text-xs text-white">
            <Spinner className="text-white" /> {busyLabel}
          </div>
        )}

        {/* Front/back switch */}
        {status === "live" && !busy && (
          <button
            type="button"
            onClick={toggleFacing}
            title="Switch camera"
            className="absolute right-2 top-2 inline-flex items-center gap-1 rounded-md bg-black/45 px-2 py-1 text-[11px] font-medium text-white hover:bg-black/65"
          >
            <SwitchCamera className="h-3.5 w-3.5" /> Flip
          </button>
        )}
      </div>

      <button
        type="button"
        disabled={status !== "live" || busy}
        onClick={capture}
        className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
      >
        <Camera className="h-4 w-4" /> {captureLabel}
      </button>
    </div>
  );
}
