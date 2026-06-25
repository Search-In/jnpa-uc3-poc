// mapSettings — operator-selectable map preferences surfaced in the header
// "More options → Map settings" menu and consumed by the LiveOperations map.
// Pure UI state (no backend / GIS-service changes); same pub/sub pattern as the
// guided-tour store so it works across the global header ↔ screen boundary.

import { useSyncExternalStore } from "react";

/** Basemaps offered in the Map-settings menu (ArcGIS basemap ids + label key). */
export const BASEMAP_OPTIONS = [
  { id: "satellite", labelKey: "header.basemap.satellite" },
  { id: "hybrid", labelKey: "header.basemap.hybrid" },
  { id: "streets-navigation-vector", labelKey: "header.basemap.streets" },
  { id: "topo-vector", labelKey: "header.basemap.topographic" },
] as const;

interface MapSettingsState {
  basemap: string;
}

type Listener = () => void;

class MapSettingsStore {
  // Satellite matches the previous hard-coded default, so behaviour is unchanged
  // until the operator picks another basemap.
  private state: MapSettingsState = { basemap: "satellite" };
  private listeners = new Set<Listener>();

  getState = (): MapSettingsState => this.state;

  subscribe = (fn: Listener): (() => void) => {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  };

  setBasemap = (basemap: string): void => {
    if (basemap === this.state.basemap) return;
    this.state = { basemap };
    this.listeners.forEach((l) => l());
  };
}

export const mapSettingsStore = new MapSettingsStore();

export function useMapSettings(): MapSettingsState {
  return useSyncExternalStore(
    mapSettingsStore.subscribe,
    mapSettingsStore.getState,
    mapSettingsStore.getState,
  );
}
