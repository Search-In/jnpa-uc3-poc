// widgetCollapse — per-widget collapsed/expanded state for the dashboard.
//
// Same lightweight pub/sub pattern as alertFocus / the guided-tour store (class
// singleton + useSyncExternalStore — no new dependency). Each dashboard widget
// owns an independent boolean keyed by a stable id. State is a module singleton
// so it survives in-app route changes, and is mirrored to localStorage so an
// operator's collapsed layout also persists across reloads.

import { useSyncExternalStore } from "react";

const STORAGE_KEY = "dashboard.collapsed.v1";

type Listener = () => void;

function load(): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    return parsed && typeof parsed === "object" ? (parsed as Record<string, boolean>) : {};
  } catch {
    return {};
  }
}

class WidgetCollapseStore {
  // Only collapsed ids are stored (true). A missing id means expanded — the
  // default — so widgets render open until the operator collapses them.
  private state: Record<string, boolean> = load();
  private listeners = new Set<Listener>();

  getSnapshot = (): Record<string, boolean> => this.state;

  subscribe = (fn: Listener): (() => void) => {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  };

  isCollapsed = (id: string): boolean => this.state[id] === true;

  toggle = (id: string): void => {
    const next = { ...this.state };
    if (next[id]) delete next[id];
    else next[id] = true;
    this.set(next);
  };

  private set = (next: Record<string, boolean>): void => {
    this.state = next;
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    } catch {
      /* storage unavailable (private mode / quota) — state still lives in memory */
    }
    this.listeners.forEach((l) => l());
  };
}

export const widgetCollapseStore = new WidgetCollapseStore();

/** Subscribe to a single widget's collapsed state. Re-renders only that widget. */
export function useWidgetCollapsed(id: string): boolean {
  return useSyncExternalStore(
    widgetCollapseStore.subscribe,
    () => widgetCollapseStore.isCollapsed(id),
    () => widgetCollapseStore.isCollapsed(id),
  );
}
