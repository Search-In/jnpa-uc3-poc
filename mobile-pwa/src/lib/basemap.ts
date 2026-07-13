// Basemap strategy for the Driver PWA — now on the ArcGIS Maps SDK (Esri), in
// lockstep with the dashboard's web/src/components/map/ArcgisMap.tsx so both apps
// render the same Esri basemap engine (not just the same imagery). The PWA's
// MiniMap consumes `basemapId()` to pick a well-known Esri basemap id; the SDK
// streams the tiles token-free for these ids.
//
//   default -> "hybrid"  : Esri World Imagery + a road/label reference overlay,
//                          so the driver sees real satellite imagery AND can
//                          still read street/gate names (best for navigation).
//   roads   -> "streets-navigation-vector" : Esri vector street basemap, opt-in
//                          via the MiniMap `roads` prop for a clean road map.
//
// NOTE: this stays a deliberate copy of the dashboard's basemap intent rather
// than a shared import — `web/` and `mobile-pwa/` are independent Vite packages
// with separate build graphs. Keep the Esri basemap ids aligned with the
// dashboard's BASEMAP_OPTIONS (web/src/lib/mapSettings.ts) when either changes.

const BASEMAP = (import.meta.env.VITE_BASEMAP as string | undefined) || "hybrid";

export const JNPA_CENTER: [number, number] = [73.0, 18.86]; // [lon, lat] corridor mid
export const JNPA_ZOOM = 11.2;

// Well-known Esri basemap id for the ArcGIS SDK. `roads` forces the vector
// street basemap; otherwise an env override (VITE_BASEMAP) wins, defaulting to
// the imagery+labels hybrid.
export function basemapId(roads?: boolean): string {
  if (roads) return "streets-navigation-vector";
  return BASEMAP;
}

// Retained for callers that only want to know the active provider family. The
// PWA now renders exclusively through the Esri/ArcGIS SDK.
export function activeBasemapProvider(): "esri" {
  return "esri";
}
