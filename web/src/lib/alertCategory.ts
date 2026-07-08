// Shared alert categorisation for the Notification Center / Alerts Center.
// One classifier so the header drawer and the full page agree on categories.

import type { Alert } from "@/lib/types";

export type AlertCategory =
  | "all"
  | "critical"
  | "traffic"
  | "parking"
  | "geofence"
  | "customs"
  | "ai"
  | "vehicle";

export const ALERT_CATEGORIES: AlertCategory[] = [
  "all",
  "critical",
  "traffic",
  "parking",
  "geofence",
  "customs",
  "ai",
  "vehicle",
];

export function categoryOf(a: Alert): Exclude<AlertCategory, "all"> {
  const sev = String(a.severity);
  if (sev === "critical" || sev === "REPORT_TO_POLICE") return "critical";
  if (a.kind === "ILLEGAL_PARKING") return "parking";
  if (a.kind === "WRONG_WAY" || a.kind === "ROUTE_DEVIATION") return "traffic";
  if (a.kind === "ABANDONED") return "ai";
  if (a.payload?.zone_id) return "geofence";
  if (
    a.kind === "CUSTOMS_FLAG" ||
    a.kind === "ELEVATED_SCRUTINY" ||
    a.kind === "PROVISIONAL_VEHICLE"
  )
    return "customs";
  return "vehicle";
}

export type TimeRange = "today" | "24h" | "7d";
export const TIME_RANGES: TimeRange[] = ["today", "24h", "7d"];

export function withinRange(a: Alert, range: TimeRange, now: number): boolean {
  const ts = Date.parse(a.ts);
  if (Number.isNaN(ts)) return true;
  const ageMs = now - ts;
  if (range === "24h") return ageMs <= 24 * 3600_000;
  if (range === "7d") return ageMs <= 7 * 24 * 3600_000;
  const d = new Date(ts).toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata" });
  const today = new Date(now).toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata" });
  return d === today;
}
