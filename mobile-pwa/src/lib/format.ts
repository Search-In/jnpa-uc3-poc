// Small display formatters shared across screens.

export function fmtEta(seconds?: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

export function fmtKm(km?: number | null): string {
  if (km == null || !Number.isFinite(km)) return "—";
  return `${km.toFixed(1)} km`;
}

export function fmtSpeed(kmh?: number | null): string {
  if (kmh == null || !Number.isFinite(kmh)) return "—";
  return `${Math.round(kmh)}`;
}

export function fmtClock(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function fmtRelative(iso?: string | null): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const delta = Math.max(0, Date.now() - t);
  const m = Math.floor(delta / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export function gateShort(gateId?: string | null): string {
  return gateId ? gateId.replace(/^G-/, "") : "—";
}
