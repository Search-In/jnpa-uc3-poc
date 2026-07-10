import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { IconPhone, IconShare, IconAlertTriangle, IconChevronRight } from "@/components/icons";

// Emergency / Help — a persistent, always-reachable SOS control for a driver
// working inside the port. Every action here is intentionally offline-capable:
// `tel:` calls place a real phone call with zero network, and the location
// share degrades to SMS / clipboard when the Web Share API is absent. This is
// the "big red button" a logistics / fleet app must have and the JNPA UC-3 spec
// explicitly requires — it is deliberately NOT wired to a flaky backend so it
// never fails a driver in a genuine emergency.

// Control-room number is baked in at build time (Vite inlines VITE_*). Falls
// back to the national emergency number so the button is never a dead end.
const CONTROL_ROOM: string =
  (import.meta.env.VITE_CONTROL_ROOM_PHONE as string | undefined)?.trim() || "112";

type ShareState = "idle" | "locating" | "ready" | "failed";

function ActionRow({
  icon,
  title,
  sub,
  tone,
  onClick,
  href,
}: {
  icon: ReactNode;
  title: string;
  sub?: string;
  tone?: "danger" | "default";
  onClick?: () => void;
  href?: string;
}) {
  const inner = (
    <>
      <span className="sos-row-icon" aria-hidden>
        {icon}
      </span>
      <span className="sos-row-text">
        <span className="sos-row-title">{title}</span>
        {sub ? <span className="sos-row-sub">{sub}</span> : null}
      </span>
      <span className="sos-row-chevron" aria-hidden>
        <IconChevronRight size={20} />
      </span>
    </>
  );
  const cls = `sos-row ${tone === "danger" ? "danger" : ""}`;
  return href ? (
    <a className={cls} href={href} onClick={onClick}>
      {inner}
    </a>
  ) : (
    <button className={cls} onClick={onClick}>
      {inner}
    </button>
  );
}

export default function EmergencyButton() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [share, setShare] = useState<ShareState>("idle");

  // Buzz once when the sheet opens so a driver gets tactile confirmation without
  // needing to look at the screen.
  useEffect(() => {
    if (open && navigator.vibrate) navigator.vibrate(30);
  }, [open]);

  // Lock body scroll while the sheet is up.
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  const shareLocation = () => {
    if (!navigator.geolocation) {
      setShare("failed");
      return;
    }
    setShare("locating");
    navigator.geolocation.getCurrentPosition(
      async (p) => {
        const { latitude, longitude } = p.coords;
        const maps = `https://maps.google.com/?q=${latitude.toFixed(6)},${longitude.toFixed(6)}`;
        const text = `JNPA driver needs assistance. My location: ${maps}`;
        setShare("ready");
        try {
          if (navigator.share) {
            await navigator.share({ title: "JNPA — My location", text, url: maps });
            return;
          }
        } catch {
          /* user cancelled the share sheet — fall through to SMS */
        }
        // No Web Share API (or cancelled): open an SMS draft to the control room.
        location.href = `sms:${CONTROL_ROOM}?body=${encodeURIComponent(text)}`;
      },
      () => setShare("failed"),
      { enableHighAccuracy: true, timeout: 8000, maximumAge: 5000 },
    );
  };

  return (
    <>
      <button
        className="sos-fab"
        aria-label={t("emergency.title", { defaultValue: "Emergency & Help" })}
        onClick={() => setOpen(true)}
      >
        {t("emergency.sos", { defaultValue: "SOS" })}
      </button>

      {open && (
        <div
          className="sos-backdrop"
          role="dialog"
          aria-modal="true"
          aria-label={t("emergency.title", { defaultValue: "Emergency & Help" })}
          onClick={() => setOpen(false)}
        >
          <div className="sos-sheet" onClick={(e) => e.stopPropagation()}>
            <div className="sos-grab" aria-hidden />
            <h2 className="sos-title">
              <span style={{ color: "var(--red)", verticalAlign: "-3px", marginRight: 4 }}>
                <IconAlertTriangle size={20} />
              </span>
              {t("emergency.title", { defaultValue: "Emergency & Help" })}
            </h2>
            <p className="sos-sub">
              {t("emergency.subtitle", {
                defaultValue: "Choose an action. Calls work even without internet.",
              })}
            </p>

            <ActionRow
              icon={<IconPhone size={24} />}
              tone="danger"
              title={t("emergency.callControl", { defaultValue: "Call Port Control Room" })}
              sub={t("emergency.callControlSub", { defaultValue: "JNPA gate operations" })}
              href={`tel:${CONTROL_ROOM}`}
              onClick={() => setOpen(false)}
            />
            <ActionRow
              icon={<IconAlertTriangle size={24} />}
              tone="danger"
              title={t("emergency.call112", { defaultValue: "Emergency 112" })}
              sub={t("emergency.call112Sub", { defaultValue: "Police · Fire · Ambulance" })}
              href="tel:112"
              onClick={() => setOpen(false)}
            />
            <ActionRow
              icon={<IconShare size={24} />}
              title={
                share === "locating"
                  ? t("emergency.locating", { defaultValue: "Getting your location…" })
                  : share === "failed"
                    ? t("emergency.locationFailed", {
                        defaultValue: "Location unavailable — call directly",
                      })
                    : t("emergency.shareLocation", { defaultValue: "Share my live location" })
              }
              sub={t("emergency.shareLocationSub", {
                defaultValue: "Send your GPS position for help",
              })}
              onClick={shareLocation}
            />

            <button className="btn ghost sos-cancel" onClick={() => setOpen(false)}>
              {t("emergency.cancel", { defaultValue: "Cancel" })}
            </button>
          </div>
        </div>
      )}
    </>
  );
}
