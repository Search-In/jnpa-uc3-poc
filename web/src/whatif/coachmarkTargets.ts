/**
 * coachmarkTargets — resolves a guided-tour step's DOM target to its on-screen
 * rectangle so the coach-mark can ring the EXACT business component on the active
 * page (alert card, table row, button, panel, …).
 *
 * Robust component discovery: after a view change the target element often
 * mounts LATE — the page is still mounting, and rows like the cross-twin badge
 * or a police-report row only appear once their scenario_step WebSocket frame
 * lands and renders. So we (1) watch the DOM with a MutationObserver to catch the
 * instant it appears, AND (2) run an always-on per-frame tracker that finds the
 * element, scrolls it into view once, and re-measures its rect EVERY frame. That
 * continuous re-measure is what keeps the spotlight glued to the target even when
 * the live timeline silently re-renders and reflows the layout (a plain
 * scroll/resize listener misses those reflows — no event fires). If the element
 * never appears within the discovery budget we log a warning and skip — never ring
 * the wrong element, never a stray box. No hard-coded coordinates — pure
 * data-guided-id lookup + getBoundingClientRect.
 */
import { useLayoutEffect, useState } from "react";

export interface Rect {
  top: number;
  left: number;
  width: number;
  height: number;
}

/** The DOM selector for a tagged component. */
export function guidedSelector(token: string): string {
  return `[data-guided-id="${token}"]`;
}

const valid = (r: DOMRect) => r.width > 8 && r.height > 8 && r.bottom > 0 && r.right > 0;

/** True when the element's box is (mostly) outside the viewport. */
function offscreen(r: DOMRect): boolean {
  const vh = window.innerHeight || document.documentElement.clientHeight;
  const vw = window.innerWidth || document.documentElement.clientWidth;
  return r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw;
}

/**
 * How long to keep waiting for a late-mounting target before giving up, expressed
 * in animation frames (~60 fps ⇒ ~8 s). Generous, because a row like the
 * cross-twin badge only mounts once its scenario_step WebSocket frame lands and
 * the page has navigated + painted.
 */
const DISCOVERY_FRAMES = 480;

/**
 * Measure the element tagged `data-guided-id="<token>"` on the active page,
 * waiting for it to appear (it may mount after navigation / when its WS row
 * renders). Scrolls it into view when `scroll` is set. `dep` (e.g. the step
 * index) re-arms discovery for the next step.
 */
export function useTargetRect(token: string | null, scroll: boolean, dep: unknown): Rect | null {
  const [rect, setRect] = useState<Rect | null>(null);
  useLayoutEffect(() => {
    setRect(null);
    if (!token) return;

    let cancelled = false;
    let found = false;
    let scrolled = false;
    // Last committed rect, so the per-frame tracker only re-renders on real change.
    let last: Rect | null = null;

    const find = () => document.querySelector<HTMLElement>(guidedSelector(token));

    // Commit a rect, but only when it actually moved — a tight comparison keeps the
    // always-on tracker from re-rendering every frame when nothing changed.
    const commit = (r: DOMRect): void => {
      if (
        last &&
        last.top === r.top &&
        last.left === r.left &&
        last.width === r.width &&
        last.height === r.height
      )
        return;
      last = { top: r.top, left: r.left, width: r.width, height: r.height };
      setRect(last);
    };

    // (2) Watch the DOM so a late-mounting target is caught the instant it appears.
    // The continuous tracker below also discovers it via rAF, but the observer
    // shaves the first-paint latency for a row that mounts off a WS frame.
    const observer = new MutationObserver(() => {
      if (cancelled || found) return;
      if (find()) found = true; // hand off to the tracker, which measures + scrolls
    });
    observer.observe(document.body, { childList: true, subtree: true });

    // Continuous tracker — the single source of truth for the ring. Every frame it
    // (a) finds the tagged element (covers a late mount after navigation / a WS row
    // that renders only once its scenario_step lands), (b) scrolls it into view ONCE
    // when off-screen, and (c) re-measures and re-commits its rect. Because it runs
    // every frame it keeps the spotlight glued to the target even when the live
    // timeline silently re-renders and shifts the layout (the cross-twin badge sits
    // at the bottom of a list that grows as steps arrive) — a plain scroll/resize
    // listener misses those reflows because no scroll/resize event fires.
    let raf = 0;
    let discoverFrames = 0;
    const tick = () => {
      if (cancelled) return;
      const el = find();
      if (el) {
        found = true;
        const r = el.getBoundingClientRect();
        if (valid(r)) {
          if (scroll && !scrolled && offscreen(r)) {
            scrolled = true;
            el.scrollIntoView({ behavior: "smooth", block: "center", inline: "center" });
            // Let the smooth scroll settle; the next frames re-measure it.
          }
          commit(r);
        }
      } else if (!found && discoverFrames++ > DISCOVERY_FRAMES) {
        // Never appeared within the discovery budget — stop, never ring a stray box.
        observer.disconnect();
        if (import.meta.env.DEV) {
          console.warn(
            `[GuidedTour] target component not found, skipping highlight: ${guidedSelector(token)}`,
          );
        }
        return;
      }
      // If it was found and then vanished (a re-render swapped the node), keep
      // polling so the ring re-attaches the instant the node returns.
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);

    return () => {
      cancelled = true;
      observer.disconnect();
      cancelAnimationFrame(raf);
    };
  }, [token, scroll, dep]);
  return rect;
}
