// SecureVision annotated replay (MJPEG).
//
// This is the one genuinely new frontend capability in the integration: nothing
// in this console previously consumed multipart/x-mixed-replace.
//
// Why a plain <img> and not fetch+Blob (the vendor guide's Option A):
//
//   * The browser's own <img> pipeline decodes multipart/x-mixed-replace
//     natively — that is exactly what the format exists for. The fetch+Blob
//     alternative reimplements JPEG boundary scanning in JS, allocates an object
//     URL per frame, and leaks blobs on any early-return path. At 5 fps for
//     minutes at a time, that is real GC churn for no gain.
//   * An <img> cannot send an Authorization header. Rather than making camera
//     footage public, the gateway mints a short-lived ticket scoped to ONE
//     analysis and accepts it in the query string — the same shape /api/ws
//     already uses for the same browser limitation.
//
// The vendor's own bearer token never reaches this component; the gateway
// attaches it upstream.
//
// Failure states are explicit. A 409 (the vendor evicted the sampled frames,
// e.g. after a restart) is NOT a broken image — it is a "re-run analysis"
// prompt, because the incident results survive but the replay cannot.

import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { AlertTriangle, Pause, Play, RefreshCw } from "lucide-react";

import { getAdapter } from "@/data";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { FilterSelect } from "@/components/ui/dtccc";
import { Spinner } from "@/components/ui/misc";
import { useAuthedImage } from "@/hooks/useAuthedImage";
import { STATUS } from "@/lib/tokens";
import { isAnalysisExpired, svErrorMessage } from "@/lib/securevision";
import { SvSectionHeader } from "./SvCommon";

type StreamState = "idle" | "starting" | "playing" | "expired" | "error";

export function SvLiveDetectionStream({
  analysisId,
  posterUrl,
  onReRun,
}: {
  analysisId: string | null;
  /** A best-frame snapshot to show before playback starts / after it stops. */
  posterUrl?: string | null;
  /** Invoked by the "Re-run analysis" action after a 409. */
  onReRun?: () => void;
}) {
  const [state, setState] = useState<StreamState>("idle");
  const [src, setSrc] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [fps, setFps] = useState(5);
  const imgRef = useRef<HTMLImageElement>(null);
  const poster = useAuthedImage(state === "playing" ? null : (posterUrl ?? null));

  const stop = useCallback(() => {
    // Dropping the src closes the still-open HTTP connection, which is what
    // frees the relay slot on the gateway. Leaving it attached would hold an
    // upstream connection open for as long as the tab lives.
    setSrc(null);
    setState("idle");
  }, []);

  const start = useMutation({
    mutationFn: () => getAdapter().svStreamTicket(analysisId as string),
    onMutate: () => {
      setState("starting");
      setMessage(null);
    },
    onSuccess: (ticket) => {
      const url = new URL(ticket.stream_url, window.location.origin);
      url.searchParams.set("fps", String(fps));
      // loop=false: a finite replay ends by itself, which releases the upstream
      // connection instead of holding one open indefinitely per viewer.
      url.searchParams.set("loop", "false");
      setSrc(url.pathname + url.search);
      setState("playing");
    },
    onError: (err) => {
      if (isAnalysisExpired(err)) {
        setState("expired");
        setMessage(null);
        return;
      }
      setState("error");
      setMessage(svErrorMessage(err));
    },
  });

  // Changing analysis must never leave the previous clip's stream attached.
  useEffect(() => {
    setSrc(null);
    setState("idle");
    setMessage(null);
  }, [analysisId]);

  // Release the connection when the panel unmounts.
  useEffect(() => () => setSrc(null), []);

  const disabled = !analysisId || start.isPending;

  return (
    <Card className="p-3">
      <SvSectionHeader
        icon={Play}
        title="Annotated AI replay"
        subtitle="Cached YOLOv11 detections redrawn over the analysed frames. No new inference runs, and this is a clip replay — not a live camera feed."
        right={
          <div className="flex items-center gap-1.5">
            <FilterSelect
              label="Playback speed"
              value={String(fps)}
              onChange={(v) => setFps(Number(v))}
              options={[
                { value: "2", label: "2 fps" },
                { value: "5", label: "5 fps" },
                { value: "10", label: "10 fps" },
                { value: "15", label: "15 fps" },
              ]}
            />
            {state === "playing" ? (
              <Button size="sm" variant="outline" onClick={stop}>
                <Pause className="mr-1 h-3.5 w-3.5" /> Stop
              </Button>
            ) : (
              <Button size="sm" disabled={disabled} onClick={() => start.mutate()}>
                {start.isPending ? (
                  <Spinner className="mr-1 h-3 w-3" />
                ) : (
                  <Play className="mr-1 h-3.5 w-3.5" />
                )}
                Play replay
              </Button>
            )}
          </div>
        }
      />

      <div className="flex min-h-[220px] items-center justify-center overflow-hidden rounded border border-border bg-black/80">
        {state === "playing" && src ? (
          <img
            ref={imgRef}
            src={src}
            alt="SecureVision annotated detection replay"
            className="max-h-[60vh] w-full object-contain"
            onError={() => {
              // The browser cannot tell us the status code of a failed <img>.
              // Everything we can distinguish has already been decided when the
              // ticket was minted, so this only reports that playback stopped.
              setSrc(null);
              setState("error");
              setMessage("The replay stream ended unexpectedly. Try playing it again.");
            }}
          />
        ) : state === "expired" ? (
          <ExpiredState onReRun={onReRun} />
        ) : state === "error" ? (
          <div className="flex flex-col items-center gap-2 p-6 text-center">
            <AlertTriangle className="h-5 w-5" style={{ color: STATUS.warning }} aria-hidden />
            <span className="text-sm text-white/80">{message}</span>
            <Button size="sm" variant="outline" onClick={() => start.mutate()} disabled={disabled}>
              <RefreshCw className="mr-1 h-3.5 w-3.5" /> Try again
            </Button>
          </div>
        ) : poster.status === "ready" && poster.src ? (
          <img
            src={poster.src}
            alt="Best frame"
            className="max-h-[60vh] w-full object-contain opacity-70"
          />
        ) : (
          <span className="p-6 text-xs text-white/60">
            {analysisId
              ? "Press Play replay to watch the annotated detections."
              : "No analysis selected."}
          </span>
        )}
      </div>

      {state === "playing" && (
        <p className="mt-2 text-[10.5px] text-muted-foreground">
          Streaming through the JNPA gateway — the SecureVision token never reaches this browser.
        </p>
      )}
    </Card>
  );
}

/** The 409 case, given its own state because "the frames are gone" has a
 *  specific remedy the operator can act on. */
function ExpiredState({ onReRun }: { onReRun?: () => void }) {
  return (
    <div className="flex flex-col items-center gap-2 p-6 text-center">
      <AlertTriangle className="h-5 w-5" style={{ color: STATUS.warning }} aria-hidden />
      <span className="text-sm text-white/85">Analysis frames expired — re-run analysis</span>
      <span className="max-w-md text-[11px] text-white/60">
        SecureVision keeps sampled frames in memory only, so a restart clears them. The incident
        results above are unaffected; only the replay needs a fresh upload of the same clip.
      </span>
      {onReRun && (
        <Button size="sm" variant="outline" onClick={onReRun}>
          <RefreshCw className="mr-1 h-3.5 w-3.5" /> Re-run analysis
        </Button>
      )}
    </div>
  );
}
