// BhuvanWmsLayer — ArcGIS WMSLayer factory for the Bhuvan (ISRO/NRSC)
// geospatial overlay on the Live Operations map.
//
// Flow (spec): Bhuvan WMS Server → ArcGIS map layer → JNPA Digital Twin UI.
// The gateway is control-plane only: /api/bhuvan/layers hands the browser the
// WMS endpoint + named layers (env-driven, no API key), and the ArcGIS
// WMSLayer then requests GetMap tiles from the Bhuvan server directly — no
// imagery ever passes through the backend.
//
// This module is the ONLY place UI code touches @arcgis/core for Bhuvan; the
// pure state/config helpers live in ./bhuvan.ts so they stay unit-testable in
// the repo's Node vitest environment.

import WMSLayer from "@arcgis/core/layers/WMSLayer";

import { api } from "@/lib/api";
import {
  BHUVAN_SOURCE_LABEL,
  DEFAULT_BHUVAN_OPACITY,
  clampOpacity,
  parseBhuvanConfig,
  resolveWmsUrl,
  type BhuvanConfig,
} from "./bhuvan";

export const BHUVAN_LAYER_ID = "uc3-bhuvan-wms";

/**
 * Fetch + validate the gateway's Bhuvan configuration. Returns null when the
 * integration is disabled (BHUVAN_ENABLED=false) or the answer is unusable —
 * callers surface that as the layer's error state.
 */
export async function fetchBhuvanConfig(): Promise<BhuvanConfig | null> {
  const cfg = parseBhuvanConfig(await api.bhuvanLayers());
  if (!cfg || !cfg.enabled) return null;
  return cfg;
}

/**
 * Build the WMSLayer for the configured Bhuvan layer. Fully client-side
 * definition (url + sublayers + version) so drawing starts immediately from
 * the gateway-validated config. The layer belongs at the BOTTOM of the
 * operational stack — above the basemap, below every GraphicsLayer.
 *
 * The url is the gateway's same-origin /api/bhuvan/wms relay whenever the
 * gateway advertises one — the Bhuvan server sends no CORS headers, so the
 * browser can never fetch it directly (TypeError: Failed to fetch).
 */
export function createBhuvanWmsLayer(
  config: BhuvanConfig,
  opacity: number = DEFAULT_BHUVAN_OPACITY,
): WMSLayer {
  return new WMSLayer({
    id: BHUVAN_LAYER_ID,
    title: "Bhuvan Satellite Layer",
    url: resolveWmsUrl(config, window.location.origin),
    sublayers: [{ name: config.default_layer }],
    version: "1.1.1",
    imageFormat: "image/png",
    imageTransparency: true,
    spatialReferences: [3857, 4326],
    opacity: clampOpacity(opacity),
    copyright: `© ${BHUVAN_SOURCE_LABEL} / NRSC`,
  });
}

/**
 * Load the layer, then pin its GetMap endpoint to the same-origin relay.
 *
 * ArcGIS derives GetMap requests from the capabilities document's
 * OnlineResource href (runtime property `mapUrl`), NOT from `layer.url` — so
 * a live Bhuvan capabilities answer would send every map-image request
 * straight to nrsc.gov.in, where the missing CORS headers kill it with
 * "Failed to fetch". `mapUrl` is not in the public typings but is a stable
 * runtime member (see @arcgis/core/layers/WMSLayer.js fetchImageBitmap);
 * overwriting it after load() forces all imagery through the gateway relay.
 */
export async function loadBhuvanLayer(layer: WMSLayer, config: BhuvanConfig): Promise<WMSLayer> {
  await layer.load();
  (layer as unknown as { mapUrl?: string }).mapUrl = resolveWmsUrl(config, window.location.origin);
  return layer;
}
