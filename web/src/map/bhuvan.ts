// Pure state/config helpers for the Bhuvan WMS geospatial layer (ISRO/NRSC).
// Framework-free and ArcGIS-free so they run under the repo's Node vitest
// environment (bhuvan.test.ts) — the same split as lib/air_quality.ts. The
// ArcGIS-coupled layer factory lives next door in BhuvanWmsLayer.ts.
//
// All configuration comes from the gateway (/api/bhuvan/layers) — the backend
// owns BHUVAN_WMS_URL / BHUVAN_LAYER / BHUVAN_ENABLED; the browser only ever
// renders WMS GetMap tiles from the URL the gateway hands it. No VITE_ var,
// no hardcoded vendor URL in UI code.

/** One named WMS layer as advertised by /api/bhuvan/layers. */
export interface BhuvanLayerInfo {
  name: string;
  title: string;
  type: "WMS";
}

/** The /api/bhuvan/layers answer the map consumes. */
export interface BhuvanConfig {
  provider: string;
  enabled: boolean;
  wms_url: string;
  default_layer: string;
  /** LIVE (capabilities parsed) | CONFIGURED (provider down, env fallback) | DISABLED. */
  source?: string;
  layers: BhuvanLayerInfo[];
}

/** The /api/bhuvan/health answer (posture panel / debugging). */
export interface BhuvanHealth {
  system: string;
  provider: string;
  configured: boolean;
  enabled: boolean;
  status: "AVAILABLE" | "UNAVAILABLE" | "DISABLED";
  wms_url: string;
  default_layer: string;
  layer_count?: number;
  detail?: string;
}

/** Attribution shown next to the layer toggle (spec: "Source: ISRO Bhuvan WMS"). */
export const BHUVAN_SOURCE_LABEL = "ISRO Bhuvan WMS";

/** Default overlay opacity — translucent enough that gates/corridor stay legible. */
export const DEFAULT_BHUVAN_OPACITY = 0.8;

/** Clamp any input to a valid [0,1] opacity; junk falls back to the default. */
export function clampOpacity(value: unknown): number {
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return DEFAULT_BHUVAN_OPACITY;
  return Math.min(1, Math.max(0, n));
}

/**
 * Validate the raw /api/bhuvan/layers JSON into a usable config. Returns null
 * when the shape is unusable (missing/empty WMS URL or no drawable layer) —
 * the caller surfaces that as the layer's error state instead of crashing.
 */
export function parseBhuvanConfig(raw: unknown): BhuvanConfig | null {
  if (typeof raw !== "object" || raw === null) return null;
  const o = raw as Record<string, unknown>;
  const wmsUrl = typeof o.wms_url === "string" ? o.wms_url.trim() : "";
  if (!wmsUrl) return null;
  const layers: BhuvanLayerInfo[] = Array.isArray(o.layers)
    ? o.layers
        .filter(
          (l): l is Record<string, unknown> =>
            typeof l === "object" && l !== null && typeof (l as any).name === "string",
        )
        .map((l) => ({
          name: String(l.name),
          title: typeof l.title === "string" && l.title ? l.title : String(l.name),
          type: "WMS" as const,
        }))
    : [];
  const defaultLayer =
    (typeof o.default_layer === "string" && o.default_layer.trim()) || layers[0]?.name || "";
  if (!defaultLayer) return null;
  return {
    provider: typeof o.provider === "string" ? o.provider : "BHUVAN",
    enabled: o.enabled !== false,
    wms_url: wmsUrl,
    default_layer: defaultLayer,
    source: typeof o.source === "string" ? o.source : undefined,
    layers,
  };
}

// ---- toggle / loading / error state machine ------------------------------

export type BhuvanStatus = "idle" | "loading" | "ready" | "error";

export interface BhuvanState {
  /** Operator's checkbox intent — the layer draws when visible AND ready. */
  visible: boolean;
  status: BhuvanStatus;
  opacity: number;
  error: string | null;
}

export const initialBhuvanState: BhuvanState = {
  visible: false,
  status: "idle",
  opacity: DEFAULT_BHUVAN_OPACITY,
  error: null,
};

export type BhuvanAction =
  | { type: "toggle" }
  | { type: "loadStart" }
  | { type: "loadSuccess" }
  | { type: "loadError"; error: string }
  | { type: "setOpacity"; opacity: number };

/**
 * Reducer for the toggle → load → ready/error lifecycle. Toggling on after an
 * error resets to idle so the next toggle retries the config fetch; a load
 * failure unchecks the box so the error is explicit, never a silent no-op.
 */
export function bhuvanReducer(state: BhuvanState, action: BhuvanAction): BhuvanState {
  switch (action.type) {
    case "toggle": {
      const visible = !state.visible;
      return {
        ...state,
        visible,
        status: visible && state.status === "error" ? "idle" : state.status,
        error: visible ? null : state.error,
      };
    }
    case "loadStart":
      return { ...state, status: "loading", error: null };
    case "loadSuccess":
      return { ...state, status: "ready", error: null };
    case "loadError":
      return { ...state, status: "error", error: action.error, visible: false };
    case "setOpacity":
      return { ...state, opacity: clampOpacity(action.opacity) };
    default:
      return state;
  }
}
