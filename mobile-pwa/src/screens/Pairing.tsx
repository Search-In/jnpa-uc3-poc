import { useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import { codeToDeviceId, setPairing, setToken } from "@/lib/device";
import { api } from "@/lib/api";
import { IconTruck, IconPhone, IconChevronRight } from "@/components/icons";

// Plate the demo device (TRK-000001) resolves to — shown on the demo card for a
// realistic look. DISPLAY ONLY; pairing still uses the device id / existing flow.
const DEMO_PLATE = "MH04KN3106";

// Derive a stable device id for an OTP login from the mobile number's last 6
// digits, keeping the TRK-###### shape the rest of the platform expects.
function mobileToDeviceId(mobile: string): string {
  const d = mobile.replace(/\D/g, "").slice(-6).padStart(6, "0");
  return `TRK-${d}`;
}

// Pairing — PoC authentication is a simple device_id pairing: scan the QR (which
// encodes the PWA URL with ?device=TRK-...) or type the 6-digit code printed on
// the in-cab unit. No real OTP — but the screen exists and looks right. A code
// like "000001" maps deterministically to device "TRK-000001" (the ids the
// truck-sim mints), so the demo pairs without a pairing server.

const DEFAULT_CODE = "000001";

export default function Pairing({ onPaired }: { onPaired: (deviceId: string) => void }) {
  const [digits, setDigits] = useState<string[]>(Array(6).fill(""));
  const inputs = useRef<(HTMLInputElement | null)[]>([]);
  const qrRef = useRef<HTMLCanvasElement>(null);

  // Render a QR that pairs a second screen (e.g. evaluator on a laptop) to the
  // same device via the web variant ?device= param.
  useEffect(() => {
    const code = digits.join("") || DEFAULT_CODE;
    const deviceId = codeToDeviceId(code);
    const url = `${location.origin}${import.meta.env.BASE_URL}?device=${deviceId}`;
    if (qrRef.current) {
      QRCode.toCanvas(qrRef.current, url, { width: 184, margin: 1 }).catch(() => undefined);
    }
  }, [digits]);

  const setDigit = (i: number, val: string) => {
    const v = val.replace(/\D/g, "").slice(-1);
    setDigits((prev) => {
      const next = [...prev];
      next[i] = v;
      return next;
    });
    if (v && i < 5) inputs.current[i + 1]?.focus();
  };

  const onKeyDown = (i: number, e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Backspace" && !digits[i] && i > 0) inputs.current[i - 1]?.focus();
  };

  const pair = (code?: string) => {
    const c = code ?? digits.join("");
    const deviceId = codeToDeviceId(c || DEFAULT_CODE);
    setPairing(deviceId);
    onPaired(deviceId);
  };

  const ready = digits.every((d) => d !== "");

  // --- OTP login (real device auth; replaces static-only pairing) ----------
  const [mobile, setMobile] = useState("");
  const [otp, setOtp] = useState("");
  const [otpStep, setOtpStep] = useState<"mobile" | "code">("mobile");
  const [otpBusy, setOtpBusy] = useState(false);
  const [otpMsg, setOtpMsg] = useState<string | null>(null);

  const requestOtp = async () => {
    const m = mobile.replace(/\D/g, "");
    if (m.length < 10) {
      setOtpMsg("Enter a valid 10-digit mobile");
      return;
    }
    setOtpBusy(true);
    setOtpMsg(null);
    try {
      const r = await api.otpRequest(m, mobileToDeviceId(m));
      setOtpStep("code");
      // SECURITY: never surface the OTP in the UI in a production build — the
      // gateway returns dev_otp only as a local-demo convenience, and echoing it
      // on screen would defeat the second factor. Show it in dev builds only.
      setOtpMsg(
        import.meta.env.DEV && r.dev_otp
          ? `OTP sent (demo: ${r.dev_otp})`
          : "OTP sent to your mobile",
      );
    } catch {
      // Never surface a raw exception to a driver — keep it plain-language.
      setOtpMsg("Could not send OTP. Check your connection and try again.");
    } finally {
      setOtpBusy(false);
    }
  };

  const verifyOtp = async () => {
    const m = mobile.replace(/\D/g, "");
    const deviceId = mobileToDeviceId(m);
    setOtpBusy(true);
    setOtpMsg(null);
    try {
      const r = await api.otpVerify(m, otp.replace(/\D/g, ""), deviceId);
      if (r.verified && r.access_token) {
        setToken(r.access_token);
        setPairing(deviceId);
        onPaired(deviceId);
      } else {
        setOtpMsg("Invalid OTP");
      }
    } catch {
      setOtpMsg("Invalid or expired OTP");
    } finally {
      setOtpBusy(false);
    }
  };

  return (
    <div className="pair-wrap">
      {/* Brand + welcome */}
      <div className="pair-hero">
        <img className="logo" src={`${import.meta.env.BASE_URL}icons/icon.svg`} alt="JNPA" />
        <h1>JNPA Trucking</h1>
        <p className="pair-welcome">Welcome, Driver</p>
        <p className="pair-tagline">Live gate slots, ETA and re-route advisories for the port.</p>
      </div>

      {/* PRIMARY — Start Demo Vehicle (same demo action, restyled) */}
      <button
        type="button"
        className="demo-card"
        data-testid="pair-demo"
        onClick={() => pair(DEFAULT_CODE)}
      >
        <span className="demo-card-top">
          <span className="demo-card-icon">
            <IconTruck size={26} />
          </span>
          <span className="demo-card-body">
            <span className="demo-card-title">Start Demo Vehicle</span>
            <span className="demo-card-plate">{DEMO_PLATE}</span>
            <span className="demo-card-device">Device · TRK-000001</span>
          </span>
        </span>
        <span className="demo-card-cta">
          Start Demo Journey <IconChevronRight size={18} />
        </span>
      </button>

      {/* SECONDARY — Driver Login (OTP) */}
      <div className="login-card">
        <div className="login-head">
          <span className="login-head-ico">
            <IconPhone size={18} />
          </span>
          <div>
            <div className="login-title">Driver Login</div>
            <div className="login-sub">
              {otpStep === "mobile" ? "Enter your mobile number" : "Enter the OTP we sent you"}
            </div>
          </div>
        </div>

        {otpStep === "mobile" ? (
          <>
            <div className="phone-field">
              <span className="phone-prefix">+91</span>
              <input
                inputMode="numeric"
                placeholder="Mobile number"
                aria-label="Mobile number"
                value={mobile}
                onChange={(e) => setMobile(e.target.value)}
              />
            </div>
            <button className="btn primary" disabled={otpBusy} onClick={requestOtp}>
              {otpBusy ? "Sending…" : "Send OTP"}
            </button>
          </>
        ) : (
          <>
            <input
              className="otp-input"
              inputMode="numeric"
              placeholder="6-digit OTP"
              aria-label="6-digit OTP"
              value={otp}
              onChange={(e) => setOtp(e.target.value)}
              maxLength={6}
            />
            <button className="btn primary" disabled={otpBusy} onClick={verifyOtp}>
              {otpBusy ? "Verifying…" : "Verify & Login"}
            </button>
            <button className="btn ghost" onClick={() => setOtpStep("mobile")}>
              Change number
            </button>
          </>
        )}
        {otpMsg && <div className="login-msg">{otpMsg}</div>}
      </div>

      {/* ADVANCED — In-cab unit pairing (collapsed by default) */}
      <details className="pair-advanced">
        <summary>
          <span>In-cab unit pairing</span>
          <span className="pair-advanced-chev">
            <IconChevronRight size={18} />
          </span>
        </summary>
        <div className="pair-advanced-body">
          <p className="pair-advanced-hint">Scan this code with the in-cab tablet to pair it.</p>
          <div className="qr-box">
            <canvas ref={qrRef} data-testid="pair-qr" />
          </div>
          <div className="pair-code-label">Or enter the 6-digit pairing code</div>
          <div className="code-input">
            {digits.map((d, i) => (
              <input
                key={i}
                ref={(el) => (inputs.current[i] = el)}
                inputMode="numeric"
                maxLength={1}
                value={d}
                data-testid={`pair-digit-${i}`}
                onChange={(e) => setDigit(i, e.target.value)}
                onKeyDown={(e) => onKeyDown(i, e)}
              />
            ))}
          </div>
          <button
            className="btn primary"
            data-testid="pair-submit"
            disabled={!ready}
            onClick={() => pair()}
          >
            Pair device
          </button>
        </div>
      </details>
    </div>
  );
}
