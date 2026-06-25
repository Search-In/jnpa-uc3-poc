// Shared alert helpers — used by the header notification drawer (and previously
// the live screen). Pure utilities; no API/business-logic changes.

import type { Alert } from "@/lib/types";

/** Stable identity for an alert (id when present, else a content composite). */
export function alertKey(a: Alert): string {
  return a.id || `${a.kind}-${a.ts}-${a.plate}`;
}

/** Merge WS-live alerts with the adapter seed, de-duped, newest-first, capped. */
export function mergeAlerts(live: Alert[], seed: Alert[], limit = 50): Alert[] {
  const seen = new Set<string>();
  const out: Alert[] = [];
  for (const a of [...live, ...seed]) {
    const key = alertKey(a);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(a);
  }
  return out.slice(0, limit);
}
