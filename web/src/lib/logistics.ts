// Pure presentation helpers for the logistics surface (LogisticsTile).
// Kept free of React/DOM so they are unit-testable with vitest (see
// logistics.test.ts), the same split as lib/air_quality.ts. All data comes
// from GET /api/logistics/* — the browser never talks to the ULIP platform.
import type { Tone } from "@/components/ui/dtccc";
import type {
  LogisticsCurrent,
  LogisticsEvent,
  LogisticsTrackingStatus,
} from "./types";

/** Tone for the LIVE / DEGRADED / OFFLINE status chip. */
export function logisticsStatusTone(status?: LogisticsCurrent["status"]): Tone {
  if (status === "LIVE") return "ok";
  if (status === "DEGRADED") return "warn";
  if (status === "OFFLINE") return "critical";
  return "neutral";
}

/** Tone for the source chip (rung that answered). NONE is the explicitly
 * empty fallback — informational, not an error state. */
export function logisticsSourceTone(source?: LogisticsCurrent["source"]): Tone {
  if (source === "ULIP") return "ok";
  if (source === "NONE") return "info";
  if (source == null) return "neutral";
  return "warn"; // cache / database rungs
}

/** Tone for a per-reference IN_TRANSIT / IDLE / UNKNOWN tracking chip. */
export function trackingStatusTone(status?: LogisticsTrackingStatus | null): Tone {
  if (status === "IN_TRANSIT") return "ok";
  if (status === "IDLE") return "warn";
  return "neutral"; // UNKNOWN / absent
}

/** Human label for a normalised logistics event type. */
const EVENT_LABEL: Record<string, string> = {
  TOLL_CROSSING: "Toll crossing",
  CONTAINER_MOVEMENT: "Container movement",
};
export function eventTypeLabel(eventType?: string | null): string {
  if (!eventType) return "Event";
  return EVENT_LABEL[eventType] ?? eventType.replace(/_/g, " ").toLowerCase();
}

/** One-line caption for an event row: "Toll crossing · Karal Phata (MH46AB1234)". */
export function eventCaption(event: LogisticsEvent): string {
  const parts = [eventTypeLabel(event.event_type)];
  if (event.location) parts.push(event.location);
  const head = parts.join(" · ");
  return event.ref_id ? `${head} (${event.ref_id})` : head;
}

/** "—"-safe integer formatter for the Stat cells. */
export function fmtCount(value: number | null | undefined): string {
  return value == null ? "—" : String(Math.max(0, Math.round(value)));
}
