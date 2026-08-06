import { useState } from "react";
import { useTranslation } from "react-i18next";
import { clearToken, setPairing } from "@/lib/device";
import { ensureDeviceToken, api } from "@/lib/api";
import { classifyLoginInput } from "@/lib/vehicleLogin";
import { enablePush } from "@/lib/pwa";
import { IconTruck, IconChevronRight } from "@/components/icons";

// Production sign-in. The driver authenticates with the VEHICLE NUMBER painted
// on their truck (MH04LZ1507) — the only identifier a driver actually knows.
// The backend keys every driver record on the internal Vehicle ID (TRK-######),
// so sign-in resolves number -> id first and the rest of the flow is unchanged:
//
//   0. resolve the registration    -> POST /api/driver/login          (public)
//   1. mint the DRIVER-scoped JWT  -> POST /api/auth/device-token     (existing)
//   2. confirm the vehicle is live -> GET  /api/trucks/{device_id}    (existing)
//
// The TRK id / bare pairing code are still ACCEPTED (operations reading an id to
// a driver over the phone) but never advertised — the UI speaks vehicle numbers
// only. Only on success do we persist the pairing (id + number), register the
// push token, and enter the app.

export default function Pairing({ onPaired }: { onPaired: (deviceId: string) => void }) {
  const { t } = useTranslation();
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const signIn = async () => {
    setError(null);
    const input = classifyLoginInput(value);
    if (input.kind === "invalid") {
      setError(
        t("pairing.invalidId", {
          defaultValue: "Enter a valid vehicle number, e.g. MH04LZ1507",
        }),
      );
      return;
    }

    setBusy(true);
    try {
      // 0) Resolve the registration to the internal Vehicle ID. The driver never
      //    sees that id — it only keys the token mint and the truck probe below.
      let deviceId = input.value;
      let vehicleNumber: string | null = input.kind === "plate" ? input.value : null;
      if (input.kind === "plate") {
        try {
          const res = await api.driverLogin(input.value);
          deviceId = res.vehicle_id;
          vehicleNumber = res.vehicle_number || input.value;
        } catch (err) {
          const status = (err as { status?: number })?.status;
          setError(
            status === 404
              ? t("pairing.notFound", {
                  defaultValue:
                    "This vehicle number isn't registered. Check the number and try again.",
                })
              : status === 403
                ? t("pairing.inactive", {
                    defaultValue:
                      "This vehicle is not active in the fleet. Contact your transporter.",
                  })
                : t("pairing.authFailed", {
                    defaultValue: "Could not sign in. Check your connection.",
                  }),
          );
          return;
        }
      }

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
        const env = await api.truck(deviceId);
        // The live snapshot's plate is authoritative for display when the driver
        // signed in with an id form and we have no number yet.
        vehicleNumber = vehicleNumber || env.record?.plate || null;
      } catch (err) {
        const status = (err as { status?: number })?.status;
        if (status === 404) {
          clearToken();
          setError(
            t("pairing.notFound", {
              defaultValue: "This vehicle number isn't registered. Check the number and try again.",
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

      // Persist the pairing WITH the registration, so every screen (Home strip,
      // Profile, session) shows the number immediately — no TRK id anywhere.
      setPairing(deviceId, vehicleNumber);
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
          {t("pairing.tagline", { defaultValue: "Enter your vehicle number to sign in." })}
        </p>
      </div>

      {/* Vehicle-number sign-in — the only credential a driver needs */}
      <div className="login-card">
        <div className="login-head">
          <span className="login-head-ico">
            <IconTruck size={18} />
          </span>
          <div>
            <div className="login-title">
              {t("pairing.vehicleId", { defaultValue: "Vehicle Number" })}
            </div>
            <div className="login-sub">
              {t("pairing.vehicleIdSub", {
                defaultValue: "The registration number of your assigned vehicle",
              })}
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
          placeholder={t("pairing.vehicleIdHint", {
            defaultValue: "Enter assigned vehicle number",
          })}
          aria-label={t("pairing.vehicleId", { defaultValue: "Vehicle Number" })}
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
