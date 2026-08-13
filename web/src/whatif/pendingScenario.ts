/**
 * The `?scenario=` deep link, captured before anything can lose it.
 *
 * Read ONCE at module load — before React renders, before the sign-in gate decides
 * anything — and parked in sessionStorage. Two things would otherwise eat it:
 *
 *   • the SIGN-IN GATE. App renders LoginGate instead of the dashboard, so any component
 *     that reads the URL does not mount until after sign-in.
 *   • REDIRECTS. `<Navigate replace>` carries only what it is given, so a route hop drops
 *     the query string from the address bar.
 *
 * Consumed on take, so a reload after the scenario has finished does not silently restart
 * it — the link fires once, for the visit it was opened in. sessionStorage rather than
 * localStorage: a deep link belongs to the tab it was opened in.
 */

const KEY = "jnpa.uc3.pendingScenario";

/** Captured at import time. Module side effect ON PURPOSE — see above. */
(function capture() {
  try {
    const id = new URLSearchParams(window.location.search).get("scenario");
    if (id) sessionStorage.setItem(KEY, id);
  } catch {
    /* no URL / no storage — the deep link is simply unavailable, not an error */
  }
})();

export function takePendingScenario(): string | null {
  try {
    const id = sessionStorage.getItem(KEY);
    if (id) sessionStorage.removeItem(KEY);
    return id;
  } catch {
    return null;
  }
}
