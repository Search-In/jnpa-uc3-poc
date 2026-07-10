import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { fmtAgo } from "@/lib/format";

// GpsStatus — the live "Location updated N seconds ago · ±Nm" pill the UC-3 spec
// calls for (Real-time Location Experience). It self-ticks every second so the
// "ago" text counts up even between position polls, giving the driver a clear,
// glanceable signal that the feed is live (green), lagging (amber) or lost (red)
// — the same reassurance a navigation app's GPS dot provides.

const FRESH_MS = 30_000; // green: a fix within the last 30 s
const STALE_MS = 120_000; // amber: 30 s – 2 min; older = red / lost

export default function GpsStatus({
  at,
  accuracyM,
  className,
}: {
  /** epoch-millis of the last position fix, or null if none yet */
  at: number | null;
  /** horizontal accuracy in metres, if known */
  accuracyM?: number | null;
  className?: string;
}) {
  const { t } = useTranslation();
  // Re-render each second so the elapsed time and freshness tone stay current.
  const [, force] = useState(0);
  useEffect(() => {
    const iv = window.setInterval(() => force((n) => n + 1), 1000);
    return () => window.clearInterval(iv);
  }, []);

  const age = at == null ? Infinity : Date.now() - at;
  const tone = age < FRESH_MS ? "fresh" : age < STALE_MS ? "stale" : "lost";

  const label =
    at == null
      ? t("gps.awaiting", { defaultValue: "Awaiting GPS…" })
      : `${t("gps.updated", { defaultValue: "Updated" })} ${fmtAgo(at)}`;

  return (
    <span className={`gps-pill ${tone} ${className || ""}`} aria-live="polite">
      <span className="gps-dot" />
      <span>{label}</span>
      {accuracyM != null && Number.isFinite(accuracyM) ? (
        <span style={{ opacity: 0.8 }}>· ±{Math.round(accuracyM)}m</span>
      ) : null}
    </span>
  );
}
