/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_MAPBOX_TOKEN?: string;
  readonly VITE_BASEMAP?: string;
  readonly VITE_BHUVAN_WMS?: string;
  readonly VITE_GATEWAY_URL?: string;
  /**
   * `live` | `mock`. Honoured verbatim in dev/serve. In a production build
   * (`vite build`) the value is ignored unless VITE_ALLOW_MOCK=true — prod
   * builds compile to `live` by default. See vite.config.ts.
   */
  readonly VITE_DATA_MODE?: string;
  /** Escape hatch: allow a PRODUCTION build to compile in mock mode (local only). */
  readonly VITE_ALLOW_MOCK?: string;
  /** Optional ArcGIS API key. Basemaps work without one in dev. */
  readonly VITE_ARCGIS_API_KEY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

// Build-time constants injected by vite.config.ts `define` (and vitest.config.ts).
// `__JNPA_DATA_MODE__` is the authoritative, compile-time-resolved data mode.
declare const __JNPA_DATA_MODE__: "live" | "mock";
declare const __JNPA_DATA_MODE_MARKER__: string;
