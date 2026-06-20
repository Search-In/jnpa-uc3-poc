import { useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import { codeToDeviceId, setPairing } from "@/lib/device";

// Pairing — PoC authentication is a simple device_id pairing: scan the QR (which
// encodes the PWA URL with ?device=DEV-...) or type the 6-digit code printed on
// the in-cab unit. No real OTP — but the screen exists and looks right. A code
// like "000001" maps deterministically to device "DEV-000001" (the ids the
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

  return (
    <div className="pair-wrap">
      <div className="brand">
        <img className="logo" src={`${import.meta.env.BASE_URL}icons/icon.svg`} alt="JNPA" />
        <h1>JNPA Trucking</h1>
        <p>Pair your in-cab unit to receive gate slots & live re-routes.</p>
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
        Use demo device (DEV-000001)
      </button>
    </div>
  );
}
