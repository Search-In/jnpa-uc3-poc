// useWebcam — browser camera lifecycle for the driver identity verification flow.
//
// Owns getUserMedia start/stop, exposes a <video> ref for the live preview, a
// frame `capture()` (→ JPEG data-URL), and a `validate()` alignment/liveness
// gate. The camera starts only on explicit user action (start()) and is always
// torn down on unmount, so leaving the modal releases the device.
//
// Liveness is a deliberate seam: it uses the experimental FaceDetector API when
// the browser exposes it, and otherwise degrades to "unchecked" so the workflow
// still functions. A passive-liveness model can replace `validate()` later
// without touching the UI.

import { useCallback, useEffect, useRef, useState } from "react";

export type WebcamStatus = "idle" | "requesting" | "live" | "denied" | "error";

export interface CaptureValidation {
  ok: boolean;
  /** Machine reason: ok | liveness_unchecked | no_face_detected | multiple_faces |
   *  face_not_centered | move_closer | move_back | camera_not_ready. */
  reason: string;
}

export interface UseWebcam {
  videoRef: React.RefObject<HTMLVideoElement>;
  status: WebcamStatus;
  error: string | null;
  start: () => Promise<void>;
  stop: () => void;
  capture: () => string | null;
  validate: () => Promise<CaptureValidation>;
}

export function useWebcam(): UseWebcam {
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const [status, setStatus] = useState<WebcamStatus>("idle");
  const [error, setError] = useState<string | null>(null);

  const stop = useCallback(() => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    if (videoRef.current) videoRef.current.srcObject = null;
    setStatus("idle");
  }, []);

  const start = useCallback(async () => {
    if (!navigator.mediaDevices?.getUserMedia) {
      setStatus("error");
      setError("Camera API not available in this browser.");
      return;
    }
    setStatus("requesting");
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } },
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
  }, []);

  const capture = useCallback((): string | null => {
    const v = videoRef.current;
    if (!v || !v.videoWidth) return null;
    const canvas = document.createElement("canvas");
    canvas.width = v.videoWidth;
    canvas.height = v.videoHeight;
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    ctx.drawImage(v, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL("image/jpeg", 0.9);
  }, []);

  const validate = useCallback(async (): Promise<CaptureValidation> => {
    const v = videoRef.current;
    if (!v || !v.videoWidth) return { ok: false, reason: "camera_not_ready" };

    // Experimental FaceDetector (Chromium). Absent elsewhere → degrade gracefully.
    const FaceDetector = (window as unknown as { FaceDetector?: any }).FaceDetector;
    if (!FaceDetector) return { ok: true, reason: "liveness_unchecked" };
    try {
      const detector = new FaceDetector({ fastMode: true, maxDetectedFaces: 2 });
      const faces = await detector.detect(v);
      if (!faces || faces.length === 0) return { ok: false, reason: "no_face_detected" };
      if (faces.length > 1) return { ok: false, reason: "multiple_faces" };
      const box = faces[0].boundingBox as DOMRectReadOnly;
      const cx = box.x + box.width / 2;
      const cy = box.y + box.height / 2;
      const offX = Math.abs(cx - v.videoWidth / 2) / v.videoWidth;
      const offY = Math.abs(cy - v.videoHeight / 2) / v.videoHeight;
      if (offX > 0.18 || offY > 0.18) return { ok: false, reason: "face_not_centered" };
      const frac = box.width / v.videoWidth;
      if (frac < 0.2) return { ok: false, reason: "move_closer" };
      if (frac > 0.85) return { ok: false, reason: "move_back" };
      return { ok: true, reason: "ok" };
    } catch {
      return { ok: true, reason: "liveness_unchecked" };
    }
  }, []);

  // Release the camera when the component using the hook unmounts (e.g. the
  // verification modal closes).
  useEffect(() => () => stop(), [stop]);

  return { videoRef, status, error, start, stop, capture, validate };
}
