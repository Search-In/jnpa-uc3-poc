import { useState } from "react";
import { useTranslation } from "react-i18next";
import { codeToDeviceId, clearToken, setPairing } from "@/lib/device";
import { ensureDeviceToken, api } from "@/lib/api";
import { enablePush } from "@/lib/pwa";
import { IconTruck, IconChevronRight } from "@/components/icons";

// Production sign-in. The driver authenticates with their assigned Vehicle ID
// — the in-cab device id the backend keys every driver record on (format
// TRK-######). There is NO demo path and NO mobile/OTP fallback: the id is
// validated against the live backend before the session opens.
//
//   1. mint the DRIVER-scoped JWT   -> POST /api/auth/device-token   (existing)
//   2. confirm the vehicle is live  -> GET  /api/trucks/{device_id}  (existing)
//
// Only on success do we persist the pairing, register the FCM push token, and
// enter the app. Everything downstream (assigned vehicle, trips, WebSocket,
// re-routes, push) is scoped to this id exactly as the gateway models it.

const CANONICAL = /^TRK-\d{6}$/;

// Normalise driver input to the canonical device id. Accepts the full id
// ("TRK-000123"), or a bare numeric code ("000123" / "123") which maps
// deterministically to TRK-######, mirroring the truck simulator's id scheme.
function toDeviceId(raw: string): string | null {
  const v = raw.trim().toUpperCase();
  if (CANONICAL.test(v)) return v;
  if (/^\d{1,6}$/.test(v)) return codeToDeviceId(v);
  return null;
}

export default function Pairing({ onPaired }: { onPaired: (deviceId: string) => void }) {
  const { t } = useTranslation();
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const signIn = async () => {
    setError(null);
    const deviceId = toDeviceId(value);
    if (!deviceId) {
      setError(t("pairing.invalidId", { defaultValue: "Enter a valid Vehicle ID" }));
      return;
    }

    setBusy(true);
    try {
      // A previous session may have left a token bound to a DIFFERENT device.
      // Clear it so ensureDeviceToken always mints a fresh DRIVER JWT for the id
      // being signed in with (the gateway scopes the token to one device_id).
      clearToken();

      // 1) Acquire the DRIVER-scoped JWT for this device (production seam).
      const authed = await ensureDeviceToken(deviceId);
      if (!authed && import.meta.env.PROD) {
        setError(
          t("pairing.authFailed", { defaultValue: "Could not sign in. Check your connection." }),
        );
        return;
      }

      // 2) Validate the id against the live backend. GET /api/trucks/{id} 404s
      //    for an unknown / inactive vehicle — that is our rejection signal.
      try {
        await api.truck(deviceId);
      } catch (err) {
        const status = (err as { status?: number })?.status;
        if (status === 404) {
          clearToken();
          setError(
            t("pairing.notFound", {
              defaultValue: "This Vehicle ID isn't active. Check the ID and try again.",
            }),
          );
          return;
        }
        // Non-404 (network / 5xx): fail closed in production, but let a local
        // dev build through so the demo works while the truck-sim warms up.
        if (import.meta.env.PROD) {
          clearToken();
          setError(
            t("pairing.authFailed", { defaultValue: "Could not sign in. Check your connection." }),
          );
          return;
        }
      }

      setPairing(deviceId);
      // Register this device for push the moment it signs in. enablePush does the
      // WebPush/VAPID leg (the primary transport — populates push_subscriptions.webpush)
      // and, if Firebase is configured, the FCM leg too. Fire-and-forget: the
      // promise keeps running even after onPaired() unmounts this screen.
      void enablePush(deviceId);
      onPaired(deviceId);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="pair-wrap">
      {/* Brand + welcome */}
      <div className="pair-hero">
        <img className="logo" src={`${import.meta.env.BASE_URL}icons/icon.svg`} alt="JNPA" />
        <h1>JNPA Trucking</h1>
        <p className="pair-welcome">{t("pairing.welcome", { defaultValue: "Driver sign-in" })}</p>
        <p className="pair-tagline">
          {t("pairing.tagline", { defaultValue: "Enter your assigned Vehicle ID to sign in." })}
        </p>
      </div>

      {/* Vehicle ID sign-in — the only credential */}
      <div className="login-card">
        <div className="login-head">
          <span className="login-head-ico">
            <IconTruck size={18} />
          </span>
          <div>
            <div className="login-title">
              {t("pairing.vehicleId", { defaultValue: "Vehicle ID" })}
            </div>
            <div className="login-sub">
              {t("pairing.vehicleIdSub", { defaultValue: "Your assigned in-cab unit ID" })}
            </div>
          </div>
        </div>

        <input
          className="id-input"
          data-testid="pair-vehicle-id"
          inputMode="text"
          autoCapitalize="characters"
          autoCorrect="off"
          spellCheck={false}
          placeholder={t("pairing.vehicleIdHint", { defaultValue: "TRK-000123" })}
          aria-label={t("pairing.vehicleId", { defaultValue: "Vehicle ID" })}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !busy) void signIn();
          }}
        />

        <button
          className="btn primary"
          data-testid="pair-submit"
          disabled={busy || value.trim() === ""}
          onClick={() => void signIn()}
        >
          {busy
            ? t("pairing.signingIn", { defaultValue: "Signing in…" })
            : t("pairing.signIn", { defaultValue: "Sign in" })}{" "}
          {!busy && <IconChevronRight size={18} />}
        </button>

        {error && (
          <div className="login-error" role="alert" data-testid="pair-error">
            {error}
          </div>
        )}
      </div>
    </div>
  );
}
