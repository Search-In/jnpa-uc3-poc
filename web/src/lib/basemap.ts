import type { StyleSpecification } from "maplibre-gl";

// Basemap providers, in preference order:
//   1. Mapbox light style   -> only if VITE_MAPBOX_TOKEN is set (optional upgrade)
//   2. Carto Positron       -> free, token-free light raster tiles (DEFAULT)
//   3. Bhuvan (ISRO) WMS     -> opt-in via VITE_BASEMAP=bhuvan (govt basemap)
//
// The PoC ships without any paid map key, so the default must render with no
// token. Carto Positron is a clean, light-themed OSM raster basemap that needs
// no key and matches the dashboard's light theme. (Note: a Google Maps API key
// cannot drive MapLibre — Google does not serve MapLibre vector/raster styles —
// so GOOGLE_MAPS_API_KEY is intentionally not used here.)

const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN as string | undefined;
const BASEMAP = (import.meta.env.VITE_BASEMAP as string | undefined) || "carto";
const BHUVAN_WMS =
  (import.meta.env.VITE_BHUVAN_WMS as string | undefined) ||
  "https://bhuvan-vec1.nrsc.gov.in/bhuvan/wms";

export const JNPA_CENTER: [number, number] = [73.0, 18.86]; // [lon, lat] corridor mid
export const JNPA_ZOOM = 11.2;

export function activeBasemapProvider(): "mapbox" | "carto" | "bhuvan" {
  if (MAPBOX_TOKEN) return "mapbox";
  if (BASEMAP === "bhuvan") return "bhuvan";
  return "carto";
}

export function mapStyle(): string | StyleSpecification {
  if (MAPBOX_TOKEN) {
    return `https://api.mapbox.com/styles/v1/mapbox/light-v11?access_token=${MAPBOX_TOKEN}`;
  }
  if (BASEMAP === "bhuvan") {
    return bhuvanStyle();
  }
  return cartoLightStyle();
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
