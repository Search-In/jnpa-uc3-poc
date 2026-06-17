/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_MAPBOX_TOKEN?: string;
  readonly VITE_BASEMAP?: string;
  readonly VITE_BHUVAN_WMS?: string;
  readonly VITE_GATEWAY_URL?: string;
  readonly VITE_DATA_MODE?: string;
  /** Optional ArcGIS API key. Basemaps work without one in dev. */
  readonly VITE_ARCGIS_API_KEY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
