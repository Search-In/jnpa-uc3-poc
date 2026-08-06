// Sign-in input classification for the Driver PWA.
//
// The driver signs in with the REGISTRATION NUMBER painted on their truck
// (MH04LZ1507) — the internal Vehicle ID (TRK-000011) is the gateway's key, not
// something a driver should ever need to know or type. This module decides what
// the driver typed; the actual number -> id resolution happens server-side
// (POST /api/driver/login), so the client never embeds fleet knowledge.
//
// The TRK id and the bare 6-digit code are still ACCEPTED (never advertised):
// operations staff reading a pairing id to a driver over the phone must not be
// locked out, and the web variant (?device=TRK-…) keeps working. Display always
// prefers the registration regardless of which form was typed.

/** Internal Vehicle-ID format — the Vehicle Master / device key. */
export const DEVICE_ID = /^TRK-\d{6}$/;

// Indian registration formats, both current and BH-series:
//   MH04LZ1507 · MH 04 LZ 1507 · MH-04-LZ-1507   (state series)
//   22BH1234AB                                    (Bharat series)
// Spaces/hyphens are cosmetic and stripped before matching.
const PLATE_STATE = /^[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{3,4}$/;
const PLATE_BH = /^\d{2}BH\d{4}[A-Z]{1,2}$/;

export type LoginInput =
  | { kind: "plate"; value: string }
  | { kind: "device"; value: string }
  | { kind: "invalid"; value: string };

/** Uppercase and drop the separators drivers naturally type into a plate. */
export function normalizePlate(raw: string): string {
  return (raw || "").toUpperCase().replace(/[\s-]/g, "");
}

/**
 * Classify what the driver typed.
 *
 *   plate   -> registration number; resolve to a Vehicle ID via /api/driver/login
 *   device  -> an internal id form (TRK-000011 or a bare numeric code) — the
 *              legacy operational path, used verbatim
 *   invalid -> matches neither; the caller shows the format error
 */
export function classifyLoginInput(raw: string): LoginInput {
  const v = (raw || "").trim().toUpperCase();
  if (!v) return { kind: "invalid", value: v };
  if (DEVICE_ID.test(v)) return { kind: "device", value: v };
  if (/^\d{1,6}$/.test(v)) {
    // Bare pairing code -> canonical device id (TRK-000123), same rule the
    // truck simulator mints ids with.
    return { kind: "device", value: `TRK-${v.padStart(6, "0")}` };
  }
  const plate = normalizePlate(v);
  if (PLATE_STATE.test(plate) || PLATE_BH.test(plate)) {
    return { kind: "plate", value: plate };
  }
  return { kind: "invalid", value: v };
}
