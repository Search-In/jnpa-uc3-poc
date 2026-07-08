/**
 * basemapFallback — ArcGIS token-death / offline survival (spec §3, an explicit
 * acceptance test: "if the ArcGIS API key/token is unavailable at runtime, fall
 * back automatically to a bundled offline vector basemap … the demo must survive
 * token death").
 *
 * The online Esri basemaps ('hybrid', 'gray-vector', …) fetch tiles from Esri's
 * CDN and need a valid token. On a venue with no Wi-Fi, or when the ArcGIS trial
 * token expires, those tiles blank out. This module:
 *   1. `initialBasemap()` — returns the online basemap normally, or a fully
 *      local neutral Basemap immediately when `?offline=1` is set (so the
 *      fallback is rehearsable before the demo).
 *   2. `installBasemapFallback(view)` — watches the view; if the basemap fails
 *      to load or tiles never arrive within a timeout, it swaps in the local
 *      Basemap so the operational layers (terminals, gates, yard, rail, flows)
 *      stay fully legible on a neutral canvas — the map never goes blank.
 *
 * The local base is generated in-memory from a single full-extent graphic (no
 * external tiles, no token) — an honest "bundled offline vector basemap".
 */
import Basemap from "@arcgis/core/Basemap";
import GraphicsLayer from "@arcgis/core/layers/GraphicsLayer";
import Graphic from "@arcgis/core/Graphic";
import Extent from "@arcgis/core/geometry/Extent";
import SimpleFillSymbol from "@arcgis/core/symbols/SimpleFillSymbol";
import type MapView from "@arcgis/core/views/MapView";
import type SceneView from "@arcgis/core/views/SceneView";

/** True when the operator asked for the offline rehearsal (`?offline=1`). */
export function isOfflineRequested(): boolean {
  try {
    return new URLSearchParams(window.location.search).get("offline") === "1";
  } catch {
    return false;
  }
}

/**
 * A fully local Basemap: one world-covering polygon in a neutral "land" tone
 * (dark-slate, matching the Calcite-dark shell) with no external tile source.
 * Renders instantly and needs no token — the operational layers draw on top.
 */
export function makeOfflineBasemap(): Basemap {
  const bg = new GraphicsLayer({ title: "Offline base (local, no tiles)" });
  bg.add(
    new Graphic({
      geometry: new Extent({
        xmin: -20037508,
        ymin: -20037508,
        xmax: 20037508,
        ymax: 20037508,
        spatialReference: { wkid: 3857 },
      }),
      symbol: new SimpleFillSymbol({
        color: [22, 30, 42, 1], // slate — reads as neutral land/water under dark UI
        outline: { color: [40, 52, 68, 1], width: 0.5 },
      }),
    }),
  );
  return new Basemap({
    baseLayers: [bg],
    title: "Offline (bundled, no external tiles)",
    id: "jnpa-offline",
  });
}

/** The basemap to start with: local when offline is requested, else the given
 *  online basemap id (defaults to the UC3 dark-gray-vector, no API key needed). */
export function initialBasemap(online: string = "dark-gray-vector"): string | Basemap {
  return isOfflineRequested() ? makeOfflineBasemap() : online;
}

/**
 * Watch the view and swap to the local offline basemap ONLY on a genuine load
 * failure — a bad/expired token, or the base tile layer failing to create its
 * LayerView (network/token death). It deliberately does NOT use a `view.updating`
 * timeout: `updating` is true during every normal pan/zoom and while imagery
 * tiles stream in, so a timeout heuristic false-positives and wrongly blanks a
 * perfectly good online basemap. Idempotent; returns a cleanup fn. `onFallback`
 * fires once so the caller can surface an "offline basemap engaged" badge.
 */
export function installBasemapFallback(
  view: MapView | SceneView,
  opts: { onFallback?: () => void } = {},
): () => void {
  // Already offline — nothing to watch.
  if (isOfflineRequested()) {
    opts.onFallback?.();
    return () => {};
  }
  let swapped = false;
  const handles: Array<{ remove: () => void }> = [];

  const swap = (reason: string) => {
    if (swapped || !view.map) return;
    swapped = true;
    // eslint-disable-next-line no-console
    console.warn(
      `[basemapFallback] online basemap unavailable (${reason}); engaging local offline basemap.`,
    );
    try {
      view.map.basemap = makeOfflineBasemap();
    } catch {
      /* view may be tearing down */
    }
    opts.onFallback?.();
  };

  view
    .when()
    .then(() => {
      const bm = view.map?.basemap;
      if (!bm) return;
      // 1) Basemap definition itself fails to load (e.g. expired/absent token).
      if (typeof bm.load === "function") {
        bm.load().catch(() => swap("basemap load rejected"));
      }
      // 2) A base tile layer can't create its LayerView (token death / offline).
      //    This is the reliable "tiles genuinely cannot be fetched" signal —
      //    it fires on real failure, never on ordinary tile streaming.
      const h = view.on("layerview-create-error", (e: __esri.ViewLayerviewCreateErrorEvent) => {
        const inBasemap = bm.baseLayers?.includes(e.layer) || bm.referenceLayers?.includes(e.layer);
        if (inBasemap) swap("base layerview-create-error");
      });
      handles.push(h);
    })
    .catch(() => swap("view failed to initialise"));

  return () => {
    handles.forEach((h) => h.remove());
  };
}
