// Keeps the port-wide focus and the browser URL in step, in both directions.
//
// The URL is the DURABLE layer of the focus design: it survives a reload, it can
// be pasted into a chat or a demo script, and — critically — it is the only layer
// that still works when the gateway is unreachable. So the deep link is read on
// mount, and every later focus change is written back.
//
// Mount this once, high in the tree (the Shell). Mounting it twice would be
// harmless but pointless; the store is a singleton.

import { useEffect, useRef } from "react";
import {
  FOCUS_PARAM_NAMES,
  focusFromParams,
  focusStore,
  focusToParams,
  usePortFocus,
} from "@/lib/focusStore";

export function useFocusUrlSync(): void {
  const focus = usePortFocus();
  const booted = useRef(false);

  // URL -> store, once, before anything can overwrite it.
  useEffect(() => {
    if (booted.current) return;
    booted.current = true;
    const keys = focusFromParams(new URLSearchParams(window.location.search));
    if (Object.keys(keys).length > 0) focusStore.set(keys, "UC-3");
  }, []);

  // store -> URL. replaceState rather than push: focusing an entity is a filter,
  // not a navigation, so it must not stack up entries the back button has to
  // walk through. Non-focus params (?q=, ?tab=) are preserved.
  useEffect(() => {
    if (!booted.current) return;
    const url = new URL(window.location.href);
    const desired = focusToParams(focus);
    const preserved = new URLSearchParams(url.search);
    // Derived from the store's own PARAM map rather than repeated here: a
    // hand-maintained copy silently stops clearing whichever key was added last,
    // leaving a stale filter in the URL that outlives the focus that set it.
    for (const k of FOCUS_PARAM_NAMES) preserved.delete(k);
    for (const [k, v] of desired.entries()) preserved.set(k, v);
    const next = preserved.toString();
    const target = `${url.pathname}${next ? `?${next}` : ""}${url.hash}`;
    if (target !== `${url.pathname}${url.search}${url.hash}`) {
      window.history.replaceState(window.history.state, "", target);
    }
  }, [focus]);
}
