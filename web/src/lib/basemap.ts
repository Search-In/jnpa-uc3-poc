import type { StyleSpecification } from "maplibre-gl";

// Basemap providers, in preference order:
//   1. Mapbox satellite      -> only if VITE_MAPBOX_TOKEN is set (optional upgrade)
//   2. Esri World Imagery    -> free, token-free satellite raster tiles (DEFAULT)
//   3. Carto Positron        -> opt-in via VITE_BASEMAP=carto (light road map)
//   4. Bhuvan (ISRO) WMS      -> opt-in via VITE_BASEMAP=bhuvan (govt basemap)
//
// The PoC ships without any paid map key, so the default must render with no
// token. Esri World Imagery is the satellite layer that backs the dashboard's
// ArcGIS "satellite" basemap, so this MapLibre surface matches the operations
// dashboard. (Note: a Google Maps API key cannot drive MapLibre — Google does
// not serve MapLibre vector/raster styles — so GOOGLE_MAPS_API_KEY is
// intentionally not used here.)

const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN as string | undefined;
const BASEMAP = (import.meta.env.VITE_BASEMAP as string | undefined) || "satellite";
const BHUVAN_WMS =
  (import.meta.env.VITE_BHUVAN_WMS as string | undefined) ||
  "https://bhuvan-vec1.nrsc.gov.in/bhuvan/wms";

export const JNPA_CENTER: [number, number] = [73.0, 18.86]; // [lon, lat] corridor mid
export const JNPA_ZOOM = 11.2;

// --------------------------------------------------------------------------
// Gate access-road bearings (OpenStreetMap ground truth)
// --------------------------------------------------------------------------
// Compass bearing (deg) of the terminal access road AT each gate marker,
// measured off the OSM highway centreline nearest the seeded gate coordinate
// (jnpa.gates / GATE_DEFS). Expressed in the SEAWARD sense (gate -> port),
// matching the UC2 reference convention (jnpa_poc_2 scene3d.ts applies
// QUAY_HEADING = 298 deg to its toll-naka gate symbol layers), so a gate model
// rotated by this value spans ACROSS its lane rather than standing broadside to
// it. Rotation only — no gate coordinate is moved.
export const GATE_ROAD_HEADING: Record<string, number> = {
  // OSM w1161066187 / w234196511 — the tertiary one-way couplet the marker sits
  // between (4.9 m and 7.3 m); 12 consecutive vertices all bear 114.4/294.4.
  "G-NSICT": 294,
  // OSM w151738469 "JNPT Terminal 3", primary, 3 lanes, one-way; 5.1 m from the
  // marker and the only highway within 78 m. Centreline bears 137.0/317.0.
  "G-JNPCT": 317,
  // OSM w383744551 — the port ring road, 13.1 m from the marker; the two
  // segments flanking the closest point both bear 304.6/124.6.
  "G-NSIGT": 305,
  // OSM w806817133 terminal service road, 2.0 m from the marker; segments
  // either side of the closest vertex bear 312.3 and 305.6 (mean 309.0).
  "G-BMCT": 309,
};

/** UC2 reference default (jnpa_poc_2 QUAY_HEADING) for an unmapped gate id. */
export const DEFAULT_GATE_HEADING = 298;

/** Access-road bearing (deg) to orient gate-side assets at `gateId`. */
export function gateRoadHeading(gateId: string): number {
  return GATE_ROAD_HEADING[gateId] ?? DEFAULT_GATE_HEADING;
}

export function activeBasemapProvider(): "mapbox" | "esri" | "carto" | "bhuvan" {
  if (MAPBOX_TOKEN) return "mapbox";
  if (BASEMAP === "carto") return "carto";
  if (BASEMAP === "bhuvan") return "bhuvan";
  return "esri"; // satellite — default
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
