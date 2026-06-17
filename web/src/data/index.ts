// Data-adapter selector. Reads VITE_DATA_MODE at startup; defaults to `mock`
// so a fresh `npm run dev` runs the full dashboard with zero credentials and no
// backend. Set VITE_DATA_MODE=live to talk to the gateway.

import type { DataAdapter, DataMode } from "./types";
import { LiveAdapter } from "./live";
import { MockAdapter } from "./mock";

export type { DataAdapter, DataMode } from "./types";

export function resolveMode(): DataMode {
  const raw = (import.meta.env.VITE_DATA_MODE ?? "mock").toString().toLowerCase();
  return raw === "live" ? "live" : "mock";
}

let _adapter: DataAdapter | null = null;

export function getAdapter(): DataAdapter {
  if (_adapter) return _adapter;
  _adapter = resolveMode() === "live" ? new LiveAdapter() : new MockAdapter();
  return _adapter;
}

export const DATA_MODE: DataMode = resolveMode();
