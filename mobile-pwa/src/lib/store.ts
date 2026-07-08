// Offline advisory cache. The spec requires the last 24 h of advisories cached
// via IndexedDB so the Inbox renders with no network (e.g. a truck in a dead
// zone). We use idb-keyval (a tiny IndexedDB wrapper) with one store keyed by
// the PWA, holding a capped, de-duplicated, time-pruned list of Advisory rows.

import { createStore, get, set } from "idb-keyval";
import type { Advisory } from "./types";

const store = createStore("jnpa-pwa", "advisories");
const KEY = "inbox";
const TTL_MS = 24 * 60 * 60 * 1000; // 24 h
const MAX = 500;

function dedupeSortPrune(rows: Advisory[]): Advisory[] {
  const cutoff = Date.now() - TTL_MS;
  const byId = new Map<string, Advisory>();
  for (const r of rows) {
    const t = Date.parse(r.ts);
    if (Number.isFinite(t) && t < cutoff) continue; // older than 24 h
    byId.set(r.id, { ...byId.get(r.id), ...r });
  }
  return [...byId.values()].sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts)).slice(0, MAX);
}

export async function loadAdvisories(): Promise<Advisory[]> {
  try {
    const rows = (await get<Advisory[]>(KEY, store)) ?? [];
    return dedupeSortPrune(rows);
  } catch {
    return [];
  }
}

export async function saveAdvisories(rows: Advisory[]): Promise<Advisory[]> {
  const pruned = dedupeSortPrune(rows);
  try {
    await set(KEY, pruned, store);
  } catch {
    /* storage may be unavailable (private mode); inbox still works in-memory */
  }
  return pruned;
}

// Merge new advisories into the cache and return the fresh, pruned list.
export async function appendAdvisories(incoming: Advisory[]): Promise<Advisory[]> {
  const existing = await loadAdvisories();
  return saveAdvisories([...existing, ...incoming]);
}

// --- Generic offline cache (Phase 3) — profile / vehicle / route / parking /
// alerts survive a cold, network-less open; refreshed opportunistically when
// online, read from cache when fetch fails. One idb-keyval store, key-namespaced.
const cacheStore = createStore("jnpa-pwa", "cache");

export async function cacheSet<T>(key: string, value: T): Promise<void> {
  try {
    await set(key, { v: value, at: Date.now() }, cacheStore);
  } catch {
    /* storage unavailable — best-effort */
  }
}

export async function cacheGet<T>(key: string): Promise<T | null> {
  try {
    const rec = await get<{ v: T; at: number }>(key, cacheStore);
    return rec ? rec.v : null;
  } catch {
    return null;
  }
}

// Fetch-through-cache: try the network; on success cache + return; on failure
// fall back to the last cached value (offline mode). Never throws.
export async function cached<T>(key: string, fetcher: () => Promise<T>): Promise<T | null> {
  try {
    const fresh = await fetcher();
    await cacheSet(key, fresh);
    return fresh;
  } catch {
    return cacheGet<T>(key);
  }
}
