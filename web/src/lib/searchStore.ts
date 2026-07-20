// Global-search hand-off store. The header omnibox writes the last query +
// detected entity here and navigates to the target screen; that screen reads it
// (useGlobalSearch) to pre-fill and auto-run its lookup. Keeps the omnibox
// decoupled from every destination while still resolving end-to-end.

import { useSyncExternalStore } from "react";

export type SearchEntity =
  | "vehicle"
  | "driver"
  | "container"
  | "shippingLine"
  | "fastag"
  | "alert"
  | "case";

export interface GlobalSearchState {
  query: string;
  entity: SearchEntity | null;
  nonce: number;
}

let state: GlobalSearchState = { query: "", entity: null, nonce: 0 };
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

export const searchStore = {
  set(query: string, entity: SearchEntity | null) {
    state = { query, entity, nonce: state.nonce + 1 };
    emit();
  },
  get(): GlobalSearchState {
    return state;
  },
  subscribe(l: () => void): () => void {
    listeners.add(l);
    return () => listeners.delete(l);
  },
};

export function useGlobalSearch(): GlobalSearchState {
  return useSyncExternalStore(searchStore.subscribe, searchStore.get, searchStore.get);
}

/** Detect the most likely entity type for a raw query string. */
export function detectEntity(raw: string): SearchEntity {
  const q = raw.trim().toUpperCase();
  if (/^[A-Z]{4}\d{7}$/.test(q)) return "container"; // ISO 6346 container no
  if (/^[A-Z]{2}\d{2}\s?\d{11}$/.test(q.replace(/\s/g, " "))) return "driver"; // DL
  if (/CASE|CHLN|CHALLAN/.test(q)) return "case";
  if (/^ALERT|^AL-/.test(q)) return "alert";
  if (/^[A-Z]{2}\d{1,2}[A-Z]{0,3}\d{3,4}$/.test(q.replace(/[\s-]/g, ""))) return "vehicle"; // plate
  return "vehicle";
}
