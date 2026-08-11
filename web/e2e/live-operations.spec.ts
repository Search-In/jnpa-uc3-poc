// Regression guard for the /live blank-page incident.
//
// A device with `state: null` made humanizeState() throw inside VehicleRail's
// map. With no error boundary above it React unmounted the whole tree, so /live
// painted the shell and nothing else. The gateway returns null state for every
// live device, so this was not an edge case — it was the normal path.
//
// These tests fail if /live ever renders empty again, at any of the widths the
// incident was reported at.
import { expect, test, type ConsoleMessage, type Page } from "@playwright/test";

const IGNORED = [
  /favicon/i,
  /Download the React DevTools/i,
  /\[vite\]/i,
  /ResizeObserver loop/i,
  /websocket|ws:\/\//i,
  /arcgis|esri/i,
  /Failed to load resource/i,
];

const SIZES = [
  { name: "desktop", width: 1600, height: 900 },
  { name: "reported", width: 1190, height: 877 },
  { name: "tablet", width: 1024, height: 800 },
  { name: "mobile", width: 390, height: 844 },
];

function watch(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push("PAGEERROR: " + e.message));
  page.on("console", (m: ConsoleMessage) => {
    if (m.type() !== "error") return;
    if (IGNORED.some((re) => re.test(m.text()))) return;
    errors.push(m.text());
  });
  return errors;
}

test.describe("/live renders", () => {
  for (const s of SIZES) {
    test(`Live Operations is not blank at ${s.name} ${s.width}x${s.height}`, async ({ page }) => {
      const errors = watch(page);
      await page.setViewportSize({ width: s.width, height: s.height });
      await page.goto("/live");

      // The incident signature: shell present, content empty. Assert on the
      // CONTENT, not just that the page responded.
      await expect(
        page.getByText(/Corridor KPIs|Turn Around Time Inside Port/).first(),
      ).toBeVisible();
      // The incident rendered a body of length 0. A healthy page is ~1,100
      // chars at mobile (collapsed sidebar) and ~12,400 at desktop, so this
      // threshold separates "blank" from "rendered" without depending on how
      // much nav chrome the current width happens to show.
      const body = (await page.locator("body").innerText()).trim();
      expect(body.length, "the page rendered no content").toBeGreaterThan(500);

      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(overflow, "page scrolls horizontally").toBeLessThanOrEqual(1);
      expect(errors, `runtime errors: ${errors.join(" | ")}`).toEqual([]);
    });
  }

  test("a device with no classified state renders as Unknown, not a crash", async ({ page }) => {
    const errors = watch(page);
    await page.goto("/live");
    // The gateway returns state: null for unclassified devices; the rail must
    // say so rather than throwing or inventing a movement state.
    await expect(page.getByText("Unknown").first()).toBeVisible();
    expect(errors, `runtime errors: ${errors.join(" | ")}`).toEqual([]);
  });

  test("the KPI panels render inside their own boundaries", async ({ page }) => {
    await page.goto("/live");
    await expect(page.getByText("Turn Around Time Inside Port").first()).toBeVisible();
    await expect(page.getByText("KPI distribution").first()).toBeVisible();
    // No panel may be showing the boundary's failure state on a healthy load.
    await expect(page.getByText(/could not be displayed/)).toHaveCount(0);
  });
});
