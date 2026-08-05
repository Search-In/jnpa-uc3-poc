// Centralised refresh strategy for the whole control-room dashboard.
//
// Enterprise dashboards make refresh EXPLICIT: the operator decides when data
// moves. This provider is the single source of truth for that behaviour —
//   • the Refresh button is the primary mechanism (calls `refresh()`),
//   • Auto-Refresh is OFF by default and opt-in per user (Off/10s/30s/1m/5m),
//   • there is exactly ONE background timer in the app (owned here), and it is
//     disabled entirely when Auto-Refresh is Off — no polling, no interval.
//
// `refresh()` refetches every *active* (mounted) query. React Query keeps the
// cache keyed, so data is replaced in place: component-local state (table
// pagination, selected row, filters) and the map camera are never reset by a
// refresh — only the underlying rows/markers update.

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { useIsFetching, useQueryClient } from "@tanstack/react-query";

export interface RefreshOption {
  /** i18n key (with an English fallback baked into the option). */
  labelKey: string;
  label: string;
  /** Poll interval in ms; 0 means Off (no timer at all). */
  ms: number;
}

export const REFRESH_OPTIONS: RefreshOption[] = [
  { labelKey: "refresh.off", label: "Off", ms: 0 },
  { labelKey: "refresh.10s", label: "10 sec", ms: 10_000 },
  { labelKey: "refresh.30s", label: "30 sec", ms: 30_000 },
  { labelKey: "refresh.1m", label: "1 min", ms: 60_000 },
  { labelKey: "refresh.5m", label: "5 min", ms: 300_000 },
];

const STORAGE_KEY = "dtccc.autoRefreshMs";
// Off by default — manual refresh is the primary mechanism.
const DEFAULT_MS = 0;

interface RefreshCtx {
  /** Current auto-refresh interval in ms (0 = Off). */
  intervalMs: number;
  /** Convenience flag: is auto-refresh currently enabled? */
  autoRefreshOn: boolean;
  /** Change the auto-refresh interval (persisted to localStorage). */
  setIntervalMs: (ms: number) => void;
  /** Manually refetch all active queries and stamp the "Updated" time. */
  refresh: () => void;
  /** Epoch-ms of the last manual/auto refresh, or undefined until the first. */
  lastRefreshAt: number | undefined;
  /** True whenever any query is in flight (drives the spinner). */
  isRefreshing: boolean;
}

const Ctx = createContext<RefreshCtx | null>(null);

function readStored(): number {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw == null) return DEFAULT_MS;
    const n = Number(raw);
    return REFRESH_OPTIONS.some((o) => o.ms === n) ? n : DEFAULT_MS;
  } catch {
    return DEFAULT_MS;
  }
}

export function RefreshProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [intervalMs, setIntervalMsState] = useState<number>(readStored);
  const [lastRefreshAt, setLastRefreshAt] = useState<number | undefined>(undefined);
  const fetching = useIsFetching();

  const refresh = useCallback(() => {
    // `type: "active"` = only queries with mounted observers (the current page).
    // Keys are preserved, so results replace data in place — no remount, no lost
    // selection/pagination/scroll, no map re-centre.
    void qc.refetchQueries({ type: "active" });
    setLastRefreshAt(Date.now());
  }, [qc]);

  const setIntervalMs = useCallback((ms: number) => {
    setIntervalMsState(ms);
    try {
      localStorage.setItem(STORAGE_KEY, String(ms));
    } catch {
      /* private-mode / storage disabled — session-only is fine */
    }
  }, []);

  // The ONE background timer for the entire app. When Auto-Refresh is Off
  // (intervalMs <= 0) this effect installs no timer whatsoever.
  useEffect(() => {
    if (intervalMs <= 0) return;
    const id = window.setInterval(() => {
      // Don't poll a tab nobody is looking at — avoids a refetch storm when the
      // operator returns to a long-backgrounded tab.
      if (document.visibilityState === "visible") refresh();
    }, intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs, refresh]);

  const value: RefreshCtx = {
    intervalMs,
    autoRefreshOn: intervalMs > 0,
    setIntervalMs,
    refresh,
    lastRefreshAt,
    isRefreshing: fetching > 0,
  };

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useRefresh(): RefreshCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useRefresh must be used within RefreshProvider");
  return v;
}
