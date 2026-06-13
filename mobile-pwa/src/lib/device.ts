// Device pairing for the PWA. Authentication in the PoC is a simple device_id
// pairing (QR + 6-digit code) — no real OTP — so "paired" just means we have a
// device_id persisted. Two entry points set it:
//
//   * the Pairing screen (driver scans a QR / types the 6-digit code), and
//   * the WEB VARIANT (?device=DEV-...): an evaluator without a phone opens
//     http://localhost:3000/pwa?device=DEV-000001 and is paired instantly so the
//     re-route push can be demoed live.
//
// The id is kept in localStorage under a stable key.

const KEY = "jnpa.pwa.device_id";
const PLATE_KEY = "jnpa.pwa.plate";

export interface Pairing {
  deviceId: string;
  plate?: string | null;
}

// The device-id format the truck-sim mints (DEV-000001 ...). The 6-digit pairing
// code maps to a device id: code "000001" -> "DEV-000001". This keeps the demo
// deterministic without a real pairing server.
export function codeToDeviceId(code: string): string {
  const digits = code.replace(/\D/g, "").padStart(6, "0").slice(-6);
  return `DEV-${digits}`;
}

export function deviceIdToCode(deviceId: string): string {
  const m = deviceId.match(/(\d{1,6})$/);
  return (m ? m[1] : "000000").padStart(6, "0");
}

function fromQuery(): string | null {
  try {
    const params = new URLSearchParams(location.search);
    const d = params.get("device");
    return d && d.trim() ? d.trim() : null;
  } catch {
    return null;
  }
}

export function getPairing(): Pairing | null {
  // The web-variant query param wins and is persisted so a refresh keeps it.
  const q = fromQuery();
  if (q) {
    localStorage.setItem(KEY, q);
  }
  const deviceId = localStorage.getItem(KEY);
  if (!deviceId) return null;
  return { deviceId, plate: localStorage.getItem(PLATE_KEY) };
}

export function setPairing(deviceId: string, plate?: string | null): Pairing {
  localStorage.setItem(KEY, deviceId);
  if (plate) localStorage.setItem(PLATE_KEY, plate);
  return { deviceId, plate };
}

export function clearPairing(): void {
  localStorage.removeItem(KEY);
  localStorage.removeItem(PLATE_KEY);
}

export function isPaired(): boolean {
  return getPairing() !== null;
}
