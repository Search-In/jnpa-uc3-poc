// alertFocus — a tiny cross-component channel for "focus this alert on the map".
//
// The notification drawer lives in the global header (Shell), while the map
// lives in the LiveOperations screen. When an operator clicks an alert in the
// drawer we publish it here; LiveOperations subscribes and pans/zooms + rings
// the incident. Same lightweight pub/sub pattern as the guided-tour store
// (class singleton + useSyncExternalStore) — no new dependency.

import { useSyncExternalStore } from "react";
import type { Alert } from "@/lib/types";

interface AlertFocusState {
  alert: Alert | null;
  /** Bumps on every focus()/clear() so repeat clicks on the same alert re-fire. */
  nonce: number;
}

type Listener = () => void;

class AlertFocusStore {
  private state: AlertFocusState = { alert: null, nonce: 0 };
  private listeners = new Set<Listener>();

  getState = (): AlertFocusState => this.state;

  subscribe = (fn: Listener): (() => void) => {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  };

  private set = (next: AlertFocusState): void => {
    this.state = next;
    this.listeners.forEach((l) => l());
  };

  focus = (alert: Alert): void => this.set({ alert, nonce: this.state.nonce + 1 });
  clear = (): void => this.set({ alert: null, nonce: this.state.nonce + 1 });
}

export const alertFocusStore = new AlertFocusStore();

export function useAlertFocus(): AlertFocusState {
  return useSyncExternalStore(
    alertFocusStore.subscribe,
    alertFocusStore.getState,
    alertFocusStore.getState,
  );
}
