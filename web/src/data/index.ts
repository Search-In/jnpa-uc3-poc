// Data-adapter selector. The mode is decided at BUILD time by vite.config.ts
// (compile-time constant `__JNPA_DATA_MODE__`), NOT at runtime:
//   - production build (`vite build`) -> "live" (LiveAdapter -> gateway /api)
//   - production build can only be "mock" with an explicit VITE_ALLOW_MOCK=true
//   - dev/serve -> configurable via VITE_DATA_MODE, defaults to "mock"
// Because the mode is a compile-time constant, the unused adapter branch below
// is dead-code-eliminated and MockAdapter is tree-shaken out of prod bundles.

import type { DataAdapter, DataMode } from "./types";
import { LiveAdapter } from "./live";
import { MockAdapter } from "./mock";

export type { DataAdapter, DataMode } from "./types";

export function resolveMode(): DataMode {
  return __JNPA_DATA_MODE__ === "live" ? "live" : "mock";
}

export const DATA_MODE: DataMode = resolveMode();

let _adapter: DataAdapter | null = null;

export function getAdapter(): DataAdapter {
  if (_adapter) return _adapter;
  // The condition is a compile-time constant -> the false branch and its
  // import are eliminated by the bundler in production (no MockAdapter shipped).
  if (__JNPA_DATA_MODE__ === "live") {
    _adapter = new LiveAdapter();
  } else {
    _adapter = new MockAdapter();
  }
  return _adapter;
}

// Greppable build marker baked into the shipped bundle as a single string
// literal, so a deployment guard can assert the image is live. See
// web/Dockerfile and scripts/verify_web_live_build.sh.
if (typeof window !== "undefined") {
  (window as Window & { __JNPA_BUILD?: string }).__JNPA_BUILD = __JNPA_DATA_MODE_MARKER__;
}
