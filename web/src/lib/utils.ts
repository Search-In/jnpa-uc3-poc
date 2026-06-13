import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// All platform timestamps are UTC ISO strings; the dashboard layer converts to
// Asia/Kolkata for display (the only place the conversion happens, per the
// .env.local.example note).
const IST = "Asia/Kolkata";

export function fmtTimeIST(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString("en-IN", { timeZone: IST, hour12: false });
}

export function fmtDateTimeIST(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("en-IN", { timeZone: IST, hour12: false });
}

export function relativeAge(iso?: string | null): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

export function fmtEta(seconds?: number | null): string {
  if (seconds == null) return "—";
  const m = Math.round(seconds / 60);
  if (m < 1) return "<1 min";
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}
