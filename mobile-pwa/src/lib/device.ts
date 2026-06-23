// Device pairing for the PWA. Authentication in the PoC is a simple device_id
// pairing (QR + 6-digit code) — no real OTP — so "paired" just means we have a
// device_id persisted. Two entry points set it:
//
//   * the Pairing screen (driver scans a QR / types the 6-digit code), and
//   * the WEB VARIANT (?device=TRK-...): an evaluator without a phone opens
//     http://localhost:3000/pwa?device=TRK-000001 and is paired instantly so the
//     re-route push can be demoed live.
//
// The id is kept in localStorage under a stable key.

const KEY = "jnpa.pwa.device_id";
const PLATE_KEY = "jnpa.pwa.plate";
const TOKEN_KEY = "jnpa.pwa.token";

export interface Pairing {
  deviceId: string;
  plate?: string | null;
}

// Canonical device-id format. MUST match the truck simulator, which mints
// `TRK-{i:06d}` (ingest/trucking_app/trucking_app/fleet.py) — that id is what
// appears in jnpa.truck_telemetry and is later requested at /api/trucks/{id}.
// The DRIVER JWT is scoped to this id, so the pairing id, the token's device_id,
// and the truck id must all be identical. The 6-digit pairing code maps
// deterministically: code "000001" -> "TRK-000001".
export const DEVICE_PREFIX = "TRK-";

export function codeToDeviceId(code: string): string {
  const digits = code.replace(/\D/g, "").padStart(6, "0").slice(-6);
  return `${DEVICE_PREFIX}${digits}`;
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
  localStorage.removeItem(TOKEN_KEY);
}

// --- DRIVER JWT (issued by the gateway at pairing) -------------------------
// The PWA carries a DRIVER-scoped bearer token so the gateway's auth middleware
// (AUTH_ENABLED=true) admits its requests and scopes them to this device. When
// auth is disabled the gateway ignores the header, so attaching it is harmless.

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* storage unavailable (private mode) — requests just go unauthenticated */
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

// Decode a JWT payload (no verification — only to read `exp` for refresh). Returns
// null on any malformed input.
function decodeExp(token: string): number | null {
  try {
    const [, payload] = token.split(".");
    const json = JSON.parse(atob(payload.replace(/-/g, "+").replace(/_/g, "/")));
    return typeof json.exp === "number" ? json.exp : null;
  } catch {
    return null;
  }
}

// True when the stored token is missing or expires within the next 60 s.
export function tokenNeedsRefresh(): boolean {
  const tok = getToken();
  if (!tok) return true;
  const exp = decodeExp(tok);
  if (exp == null) return false; // opaque token — assume the server manages TTL
  return exp - 60 < Math.floor(Date.now() / 1000);
}

export function isPaired(): boolean {
  return getPairing() !== null;
}
