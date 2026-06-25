/**
 * useTourStore — React binding for the guided-tour store. Components re-render
 * whenever the tour state changes (start / step / pause / stop). Port of the
 * reference project's useSimStore hook, scoped to tour state only.
 */
import { useSyncExternalStore } from "react";
import { tourStore, type TourState } from "./tourStore";

export function useTourStore(): TourState {
  return useSyncExternalStore(tourStore.subscribe, tourStore.getState, tourStore.getState);
}
