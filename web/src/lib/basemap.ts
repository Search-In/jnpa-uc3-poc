import type { StyleSpecification } from "maplibre-gl";

// Two basemap providers, per spec:
//   Primary  -> Mapbox style (VITE_MAPBOX_TOKEN, free tier OK)
//   Fallback -> Bhuvan (ISRO) WMS raster tiles
// When a Mapbox token is present we return its style URL; otherwise we build a
// raster style backed by the Bhuvan WMS so the map always renders without a
// paid key. A neutral dark canvas is the last resort if even Bhuvan is blocked.

const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN as string | undefined;
const BHUVAN_WMS =
  (import.meta.env.VITE_BHUVAN_WMS as string | undefined) ||
  "https://bhuvan-vec1.nrsc.gov.in/bhuvan/wms";

export const JNPA_CENTER: [number, number] = [73.0, 18.86]; // [lon, lat] corridor mid
export const JNPA_ZOOM = 11.2;

export function activeBasemapProvider(): "mapbox" | "bhuvan" {
  return MAPBOX_TOKEN ? "mapbox" : "bhuvan";
}

export function mapStyle(): string | StyleSpecification {
  if (MAPBOX_TOKEN) {
    return `https://api.mapbox.com/styles/v1/mapbox/dark-v11?access_token=${MAPBOX_TOKEN}`;
  }
  return bhuvanStyle();
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
      { id: "bg", type: "background", paint: { "background-color": "#0b1220" } },
      { id: "bhuvan", type: "raster", source: "bhuvan", paint: { "raster-opacity": 0.85 } },
    ],
  };
}
