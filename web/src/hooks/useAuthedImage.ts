// useAuthedImage — render an image that lives behind a bearer token.
//
// SecureVision evidence frames and enrolment photos are served by the gateway at
// /api/sv/media/* and /api/sv/faces/{id}/photo. Unlike /api/evidence (which is
// deliberately public because <img> cannot send headers), those routes stay
// AUTHENTICATED: the frames can contain identifiable people, so a guessable URL
// is not an acceptable credential for them.
//
// A finite image can be fetched with the token attached and rendered from an
// object URL, which is what this hook does. (The MJPEG replay cannot — an
// endless multipart stream has to be decoded by the browser's own <img>
// pipeline, which is why that ONE route uses a short-lived ticket instead.)
//
// The object URL is revoked on unmount and whenever the source changes, so a
// long-lived table of thumbnails does not leak blobs.

import { useEffect, useState } from "react";
import { getToken } from "@/lib/auth";

export type AuthedImageStatus = "idle" | "loading" | "ready" | "error";

export interface AuthedImage {
  src: string | null;
  status: AuthedImageStatus;
}

export function useAuthedImage(path: string | null | undefined): AuthedImage {
  const [src, setSrc] = useState<string | null>(null);
  const [status, setStatus] = useState<AuthedImageStatus>("idle");

  useEffect(() => {
    if (!path) {
      setSrc(null);
      setStatus("idle");
      return;
    }
    const controller = new AbortController();
    let objectUrl: string | null = null;
    let cancelled = false;

    setStatus("loading");
    const token = getToken();
    fetch(path, {
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      signal: controller.signal,
    })
      .then(async (res) => {
        if (!res.ok) throw new Error(String(res.status));
        const blob = await res.blob();
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
        setStatus("ready");
      })
      .catch(() => {
        if (cancelled) return;
        setSrc(null);
        setStatus("error");
      });

    return () => {
      cancelled = true;
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [path]);

  return { src, status };
}
