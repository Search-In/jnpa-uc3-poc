import { useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import { codeToDeviceId, setPairing, setToken } from "@/lib/device";
import { api } from "@/lib/api";

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
      setOtpMsg(r.dev_otp ? `OTP sent (demo: ${r.dev_otp})` : "OTP sent to your mobile");
    } catch (e) {
      setOtpMsg(String(e));
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
      <div className="brand">
        <img className="logo" src={`${import.meta.env.BASE_URL}icons/icon.svg`} alt="JNPA" />
        <h1>JNPA Trucking</h1>
        <p>Login with your mobile OTP to receive gate slots & live re-routes.</p>
      </div>

      {/* OTP login (primary) */}
      <div className="otp-box" style={{ width: "100%", maxWidth: 320 }}>
        {otpStep === "mobile" ? (
          <>
            <input
              inputMode="numeric"
              placeholder="Mobile number"
              value={mobile}
              onChange={(e) => setMobile(e.target.value)}
              style={{
                width: "100%",
                height: 44,
                fontSize: 18,
                textAlign: "center",
                marginBottom: 8,
              }}
            />
            <button
              className="btn primary"
              disabled={otpBusy}
              onClick={requestOtp}
              style={{ width: "100%" }}
            >
              {otpBusy ? "…" : "Send OTP"}
            </button>
          </>
        ) : (
          <>
            <input
              inputMode="numeric"
              placeholder="6-digit OTP"
              value={otp}
              onChange={(e) => setOtp(e.target.value)}
              maxLength={6}
              style={{
                width: "100%",
                height: 44,
                fontSize: 22,
                textAlign: "center",
                letterSpacing: 6,
                marginBottom: 8,
              }}
            />
            <button
              className="btn primary"
              disabled={otpBusy}
              onClick={verifyOtp}
              style={{ width: "100%" }}
            >
              {otpBusy ? "…" : "Verify & Login"}
            </button>
            <button
              className="btn ghost"
              onClick={() => setOtpStep("mobile")}
              style={{ width: "100%" }}
            >
              Change number
            </button>
          </>
        )}
        {otpMsg && (
          <div className="muted" style={{ fontSize: 12, textAlign: "center", marginTop: 6 }}>
            {otpMsg}
          </div>
        )}
      </div>

      <div className="muted" style={{ textAlign: "center", fontSize: 12, margin: "14px 0 6px" }}>
        — or pair an in-cab unit —
      </div>

      <div className="qr-box">
        <canvas ref={qrRef} data-testid="pair-qr" />
      </div>

      <div>
        <div className="muted" style={{ textAlign: "center", fontSize: 12, marginBottom: 10 }}>
          Or enter the 6-digit pairing code
        </div>
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
      </div>

      <button
        className="btn primary"
        data-testid="pair-submit"
        disabled={!ready}
        onClick={() => pair()}
      >
        Pair device
      </button>

      <button className="btn ghost" data-testid="pair-demo" onClick={() => pair(DEFAULT_CODE)}>
        Use demo device (TRK-000001)
      </button>
    </div>
  );
}
