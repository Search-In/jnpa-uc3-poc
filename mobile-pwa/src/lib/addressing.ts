// Notification addressing — the single rule deciding whether a frame is ours.
//
// The gateway stamps every driver advisory with an explicit address:
//
//   device_id : "TRK-000001"        the driver this advisory is for
//   audience  : "driver"            targeted — exactly one driver
//             | "broadcast"         genuinely for every driver (e.g. congestion)
//
// Two consumers apply it, so it lives here rather than being written twice:
//
//   * workers/realtime.worker.ts -> drops frames addressed to ANOTHER device
//     before they ever reach the page (transport-level).
//   * hooks/RealtimeContext.tsx  -> decides whether to raise a notification
//     (presentation-level, stricter: requires a positive match).
//
// The rule that caused the leak was "accept unless it is provably someone
// else's". It is now "accept only when it is provably ours". Absence of an
// address is NEVER evidence that a frame is ours.

export const AUDIENCE_BROADCAST = "broadcast";
export const AUDIENCE_DRIVER = "driver";

export interface Addressable {
  device_id?: string | null;
  plate?: string | null;
  audience?: string | null;
  [k: string]: unknown;
}

/** True when the frame is explicitly marked as being for every driver. */
export function isBroadcast(payload: unknown): boolean {
  const p = payload as Addressable | null;
  return !!p && typeof p === "object" && p.audience === AUDIENCE_BROADCAST;
}

/**
 * Transport-level drop rule (realtime worker).
 *
 * True only when the frame carries an address that belongs to a DIFFERENT
 * device. Unaddressed frames (traffic, decision, bottleneck, operator_banner …)
 * return false so they still reach the page — the page decides relevance.
 */
export function isForOtherDevice(payload: unknown, deviceId: string | null | undefined): boolean {
  const p = payload as Addressable | null;
  if (!p || typeof p !== "object") return false;
  if (isBroadcast(p)) return false;
  const target = p.device_id;
  if (!target || !deviceId) return false;
  return target !== deviceId;
}

/**
 * Presentation-level accept rule (alert -> notification).
 *
 * Requires a POSITIVE match: an explicit broadcast, our device id, or our plate.
 * Anything else belongs to another driver and must stay silent.
 */
export function isForThisDriver(
  payload: unknown,
  deviceId: string | null | undefined,
  plate: string | null | undefined,
): boolean {
  const p = payload as Addressable | null;
  if (!p || typeof p !== "object") return false;
  if (isBroadcast(p)) return true;
  if (p.device_id && deviceId && p.device_id === deviceId) return true;
  if (p.plate && plate && p.plate === plate) return true;
  return false;
}
