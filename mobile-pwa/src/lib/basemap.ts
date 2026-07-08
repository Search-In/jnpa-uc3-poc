import type { StyleSpecification } from "maplibre-gl";

// Basemap strategy for the Driver PWA — kept in lockstep with the dashboard's
// web/src/lib/basemap.ts so both apps render the same map. Provider preference:
//   1. Mapbox satellite      -> only if VITE_MAPBOX_TOKEN is set (optional upgrade)
//   2. Esri World Imagery    -> free, token-free satellite raster tiles (DEFAULT)
//   3. Carto Positron        -> opt-in via VITE_BASEMAP=carto (light road map)
//   4. Bhuvan (ISRO) WMS      -> opt-in via VITE_BASEMAP=bhuvan (govt basemap)
//
// The PoC ships without any paid key, so the default renders token-free. Esri
// World Imagery is the satellite layer that backs the dashboard's ArcGIS
// "satellite" basemap, so the driver sees the same real imagery the operations
// dashboard does. Overlays (corridor polyline, gates, truck marker) are added on
// top of this raster source by MiniMap, so they always draw above the imagery.
//
// NOTE: this is a deliberate copy of the dashboard util rather than a shared
// import — `web/` and `mobile-pwa/` are independent Vite packages with separate
// build graphs. Keep the two files identical when either changes; a future
// refactor could hoist this into a shared workspace package consumed by both.

const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN as string | undefined;
const BASEMAP = (import.meta.env.VITE_BASEMAP as string | undefined) || "satellite";
const BHUVAN_WMS =
  (import.meta.env.VITE_BHUVAN_WMS as string | undefined) ||
  "https://bhuvan-vec1.nrsc.gov.in/bhuvan/wms";

export const JNPA_CENTER: [number, number] = [73.0, 18.86]; // [lon, lat] corridor mid
export const JNPA_ZOOM = 11.2;

export function activeBasemapProvider(): "mapbox" | "esri" | "carto" | "bhuvan" {
  if (MAPBOX_TOKEN) return "mapbox";
  if (BASEMAP === "carto") return "carto";
  if (BASEMAP === "bhuvan") return "bhuvan";
  return "esri"; // satellite — default
}

// Google-Maps-style road basemap (Carto Positron light) regardless of the
// configured default — used by the driver navigation map so routes read clearly.
export function roadStyle(): StyleSpecification {
  return cartoLightStyle();
}

export function mapStyle(): string | StyleSpecification {
  if (MAPBOX_TOKEN) {
    // Mapbox satellite-streets keeps road labels on top of satellite imagery.
    return `https://api.mapbox.com/styles/v1/mapbox/satellite-streets-v12?access_token=${MAPBOX_TOKEN}`;
  }
  if (BASEMAP === "carto") {
    return cartoLightStyle();
  }
  if (BASEMAP === "bhuvan") {
    return bhuvanStyle();
  }
  return esriSatelliteStyle();
}

// Free, token-free satellite basemap (Esri World Imagery). Served as 256px raster
// tiles from ArcGIS Online — the same imagery that backs the dashboard's ArcGIS
// "satellite"/"hybrid" basemap, keeping the Driver PWA and dashboard aligned.
// Note the {z}/{y}/{x} tile order (Esri serves row-before-column).
function esriSatelliteStyle(): StyleSpecification {
  return {
    version: 8,
    sources: {
      esri: {
        type: "raster",
        tiles: [
          "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        ],
        tileSize: 256,
        attribution: "Imagery © Esri, Maxar, Earthstar Geographics, and the GIS User Community",
      },
    },
    layers: [
      { id: "bg", type: "background", paint: { "background-color": "#0b1f33" } },
      { id: "esri", type: "raster", source: "esri" },
    ],
  };
}

// Free, token-free, light-themed OSM basemap (Carto Positron). Served as 256px
// raster tiles from Carto's public CDN with subdomain sharding for throughput.
function cartoLightStyle(): StyleSpecification {
  return {
    version: 8,
    sources: {
      carto: {
        type: "raster",
        tiles: [
          "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png",
          "https://b.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png",
          "https://c.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png",
          "https://d.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png",
        ],
        tileSize: 256,
        attribution:
          '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors © <a href="https://carto.com/attributions">CARTO</a>',
      },
    },
    layers: [
      { id: "bg", type: "background", paint: { "background-color": "#eaeaea" } },
      { id: "carto", type: "raster", source: "carto" },
    ],
  };
}

function bhuvanStyle(): StyleSpecification {
  // Bhuvan publishes OGC WMS layers; we request its base imagery as 256px tiles.
  // The {bbox-epsg-3857} token is substituted by MapLibre per tile.
  const wms =
    `${BHUVAN_WMS}?service=WMS&version=1.1.1&request=GetMap&layers=india3` +
    `&styles=&format=image/png&transparent=false&srs=EPSG:3857` +
    `&width=256&height=256&bbox={bbox-epsg-3857}`;
  return {
    version: 8,
    sources: {
      bhuvan: {
        type: "raster",
        tiles: [wms],
        tileSize: 256,
        attribution: "© ISRO Bhuvan / NRSC",
      },
    },
    layers: [
      { id: "bg", type: "background", paint: { "background-color": "#f2f2f2" } },
      { id: "bhuvan", type: "raster", source: "bhuvan", paint: { "raster-opacity": 0.9 } },
    ],
  };
}
