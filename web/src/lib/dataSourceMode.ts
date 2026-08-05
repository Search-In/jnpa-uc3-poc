// Data-SOURCE mode — the provenance axis for gateway-served data. SEPARATE from
// the app's simulated/live scenario state: this switch chooses which backend
// rows the gateway returns, via an `X-Data-Mode` request header:
//   LIVE — rows sourced from the live JNPA integration APIs (the default)
//   DEMO — the manually-imported, reliable pre-loaded rows
//
// A tiny framework-free store (get / set / subscribe) persisted in localStorage.
// The value is injected as the `x-data-mode` header on every gateway request by
// lib/api.ts. React components bind via
// `useSyncExternalStore(subscribeDataSourceMode, getDataSourceMode)`.

export type DataSourceMode = "LIVE" | "DEMO";

/** localStorage key — namespaced so it never collides with app state. */
export const DATA_SOURCE_MODE_KEY = "jnpa.dataSourceMode";

/** Default LIVE: with the JNPA Port-Data sync running, the dashboards show
 * the API-ingested corpus out of the box; DEMO (the manually-imported dump)
 * stays one toggle away for offline/demo sessions. */
const DEFAULT_MODE: DataSourceMode = "LIVE";

function read(): DataSourceMode {
  try {
    const v = localStorage.getItem(DATA_SOURCE_MODE_KEY);
    return v === "LIVE" || v === "DEMO" ? v : DEFAULT_MODE;
  } catch {
    return DEFAULT_MODE;
  }
}

let current: DataSourceMode = read();
const listeners = new Set<() => void>();

/** The active mode. Read synchronously by lib/api.ts on every request. */
export function getDataSourceMode(): DataSourceMode {
  return current;
}

/** Persist + broadcast a new mode. No-op when unchanged or invalid. */
export function setDataSourceMode(mode: DataSourceMode): void {
  if (mode !== "LIVE" && mode !== "DEMO") return;
  if (mode === current) return;
  current = mode;
  try {
    localStorage.setItem(DATA_SOURCE_MODE_KEY, mode);
  } catch {
    /* private-mode / disabled storage — keep the in-memory value */
  }
  for (const l of listeners) l();
}

/** Subscribe to changes (for React's useSyncExternalStore). Returns unsubscribe. */
export function subscribeDataSourceMode(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
