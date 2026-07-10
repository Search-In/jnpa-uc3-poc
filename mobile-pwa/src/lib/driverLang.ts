// Driver-language mappers — the single place that turns backend enums into words
// a truck driver reads at a glance. Presentation only; no business logic. Keeps
// raw values (state, decision_path, jam factor) out of the driver-facing UI.

export type Tone = "moving" | "waiting" | "alert" | "idle";

export interface DriverStatus {
  tone: Tone;
  icon: string;
  /** i18n key under `status.*`, with an English default baked in */
  key: string;
  label: string;
}

// Map the truck telemetry `state` (+ speed) to a big, human status card.
export function statusFromState(state?: string | null, speedKmh?: number | null): DriverStatus {
  const s = (state || "").toUpperCase();
  if (/QUEUE|WAIT|GATE|BOOM|DOCK/.test(s))
    return { tone: "waiting", icon: "🔵", key: "waitingGate", label: "Waiting at gate" };
  if (/IDLE|STOP|PARK|HALT/.test(s) || (speedKmh != null && speedKmh <= 2))
    return { tone: "idle", icon: "🟠", key: "stopped", label: "Stopped" };
  if (/DEVIAT|ALERT|RESTRICT|SCRUT/.test(s))
    return { tone: "alert", icon: "🔴", key: "actionRequired", label: "Action required" };
  return { tone: "moving", icon: "🟢", key: "moving", label: "Moving" };
}

export interface TrafficLevel {
  key: string;
  label: string;
  tone: "ok" | "warn" | "down";
}

// A driver-facing traffic read from current speed. Heuristic, presentation-only —
// avoids surfacing jam-factor numbers.
export function trafficFromSpeed(speedKmh?: number | null): TrafficLevel | null {
  if (speedKmh == null || !Number.isFinite(speedKmh)) return null;
  if (speedKmh >= 28) return { key: "light", label: "Light", tone: "ok" };
  if (speedKmh >= 10) return { key: "medium", label: "Medium", tone: "warn" };
  return { key: "heavy", label: "Heavy", tone: "down" };
}

// The Vahan/orchestration `decision_path` (LIVE_PRIMARY / CACHED / PROVISIONAL …)
// is an internal data-source label; drivers should see trust, not the enum.
export function verifiedLabel(decisionPath?: string | null): { label: string; ok: boolean } {
  const p = (decisionPath || "").toUpperCase();
  if (p.startsWith("LIVE")) return { label: "Verified", ok: true };
  if (p === "CACHED") return { label: "Verified (recent)", ok: true };
  if (p === "PROVISIONAL") return { label: "Provisional", ok: false };
  return { label: "Verified", ok: true };
}
