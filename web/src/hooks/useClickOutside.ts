import { useEffect } from "react";

/**
 * Invoke `onOutside` when a pointer/touch press lands outside `ref`'s element.
 * No-op while `active` is false, so a closed floating panel costs nothing.
 * Powers the ArcGIS-style floating widgets (Layers / Legend / Alerts) that
 * close on an outside click.
 */
export function useClickOutside<T extends HTMLElement>(
  ref: React.RefObject<T | null>,
  onOutside: () => void,
  active: boolean,
): void {
  useEffect(() => {
    if (!active) return;
    function handler(e: MouseEvent | TouchEvent) {
      const el = ref.current;
      if (el && e.target instanceof Node && !el.contains(e.target)) onOutside();
    }
    document.addEventListener("mousedown", handler);
    document.addEventListener("touchstart", handler);
    return () => {
      document.removeEventListener("mousedown", handler);
      document.removeEventListener("touchstart", handler);
    };
  }, [ref, onOutside, active]);
}
