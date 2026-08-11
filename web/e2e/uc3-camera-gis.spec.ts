// UC3-023 camera degraded mode + UC3-020 corridor heatmap, in a real browser.
// Both extend existing surfaces: the Gate & Lane Board and GeoAnalytics.
import { expect, test, type ConsoleMessage, type Page } from "@playwright/test";

const IGNORED = [
  /favicon/i,
  /Download the React DevTools/i,
  /\[vite\]/i,
  /ResizeObserver loop/i,
  /websocket|ws:\/\//i,
  /arcgis|esri/i,
];

/** The fault-drill buttons, scoped to their own card. The page header also has
 *  a LIVE/DEMO data-source toggle, so a bare getByRole("button", {name:"LIVE"})
 *  is ambiguous. */
function drill(page: Page, rung: string) {
  return page
    .locator("div")
    .filter({ hasText: /^Fault drill:/ })
    .last()
    .getByRole("button", { name: rung, exact: true });
}

/** DataTable renders desktop + mobile variants; only one is visible. */
function vis(page: Page, text: string) {
  return page.getByText(text).filter({ visible: true }).first();
}

function watch(page: Page) {
  const errors: string[] = [];
  const failed: string[] = [];
  page.on("console", (m: ConsoleMessage) => {
    if (m.type() !== "error") return;
    if (IGNORED.some((re) => re.test(m.text()))) return;
    errors.push(m.text());
  });
  page.on("response", (r) => {
    if (!r.url().includes("/api/")) return;
    if (r.status() >= 400) failed.push(`${r.status()} ${r.url()}`);
  });
  return { errors, failed };
}

async function noOverflow(page: Page) {
  const o = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(o, "page scrolls horizontally").toBeLessThanOrEqual(1);
}

const VIEWPORTS = [
  { name: "mobile", width: 390, height: 844 },
  { name: "tablet", width: 1024, height: 800 },
  { name: "desktop", width: 1600, height: 900 },
];

test.describe("UC3-023 camera degraded mode (Gate & Lane Board)", () => {
  test.afterEach(async ({ request }) => {
    // Leave the fault console clean for the next test / the demo.
    await request.delete("/api/control/fault/camera").catch(() => {});
  });

  test("drives the full LIVE -> DEGRADED -> DOWN -> restore ladder", async ({ page }) => {
    const w = watch(page);
    await page.goto("/gate-lane-board");
    await page.getByRole("button", { name: /Camera Health/ }).click();
    await expect(page.getByText("Camera feed health").first()).toBeVisible();

    // --- LIVE ---
    await drill(page, "LIVE").click();
    await expect(vis(page, "ANPR + RFID")).toBeVisible();
    await expect(vis(page, "0.97")).toBeVisible();

    // --- DEGRADED: cached frames must read REPLAY, never LIVE ---
    await drill(page, "CACHED").click();
    await expect(vis(page, "REPLAY")).toBeVisible();
    await expect(vis(page, "0.82")).toBeVisible();
    await expect(page.getByText(/badged REPLAY, never LIVE/i)).toBeVisible();

    // --- DOWN: RFID-only + manual verify + confidence drop ---
    await drill(page, "SYNTHETIC").click();
    await expect(vis(page, "NO FEED")).toBeVisible();
    await expect(vis(page, "RFID only")).toBeVisible();
    await expect(vis(page, "MANUAL VERIFY")).toBeVisible();
    await expect(vis(page, "0.60")).toBeVisible();
    await expect(page.getByText(/service rate is cut/i)).toBeVisible();

    // --- restore: reconciliation is persisted, not just announced ---
    await page.getByRole("button", { name: /Restore/ }).click();
    await expect(page.getByText(/reconciliation written to the decision log/i)).toBeVisible();

    await noOverflow(page);
    expect(w.errors, `console errors: ${w.errors.join(" | ")}`).toEqual([]);
    expect(w.failed, `failed API calls: ${w.failed.join(" | ")}`).toEqual([]);
  });

  test("never reports LIVE when no frame bus is available", async ({ page, request }) => {
    // With the fault cleared the panel must show the REAL state. Locally there
    // is no Redis frame bus, so the honest answer is NO FEED — not a false LIVE.
    await request.delete("/api/control/fault/camera").catch(() => {});
    await page.goto("/gate-lane-board");
    await page.getByRole("button", { name: /Camera Health/ }).click();
    await expect(vis(page, "NO FEED")).toBeVisible();
    await expect(vis(page, "real cascade")).toBeVisible();
  });

  for (const vp of VIEWPORTS) {
    test(`camera health has no horizontal overflow at ${vp.name} ${vp.width}px`, async ({
      page,
    }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.goto("/gate-lane-board");
      await page.getByRole("button", { name: /Camera Health/ }).click();
      await expect(page.getByText("Camera feed health").first()).toBeVisible();
      await noOverflow(page);
    });
  }
});

test.describe("UC3-020 corridor congestion heatmap (GeoAnalytics)", () => {
  test("renders 13 segments, flips DATA_MODE at now and explains the reroute", async ({ page }) => {
    const w = watch(page);
    await page.goto("/geofencing?tab=corridor");
    await page.getByRole("button", { name: /Corridor Congestion/ }).click();

    await expect(page.getByText(/Corridor congestion — NH-348/).first()).toBeVisible();

    // Past + now are OBSERVED counts.
    await expect(vis(page, "OBSERVED")).toBeVisible();
    await expect(page.getByText("Observed counts").first()).toBeVisible();

    // All 13 NH-348 segments are listed.
    await expect(page.getByText(/Segments \(\d+ of 13 measured\)/).first()).toBeVisible();
    await expect(vis(page, "SEG-00")).toBeVisible();
    await expect(vis(page, "SEG-12")).toBeVisible();

    // Legend.
    await expect(page.getByText("Legend — congestion index").first()).toBeVisible();
    await expect(page.getByText("Free flowing").first()).toBeVisible();
    await expect(page.getByText("Severe").first()).toBeVisible();

    // The permanent resolution disclaimer (assumption A4).
    await expect(page.getByText(/not survey-grade/i).first()).toBeVisible();

    // --- the slider drives the API, and the banner flips at now ---
    const slider = page.getByLabel("Corridor heatmap time slider");
    await slider.fill("-360");
    await expect(vis(page, "OBSERVED")).toBeVisible();
    await slider.fill("120");
    await expect(vis(page, "DERIVED")).toBeVisible();
    await expect(page.getByText(/Forecast — confidence/).first()).toBeVisible();
    // A forecast bucket is labelled EXTRAPOLATED, never counted.
    await expect(vis(page, "EXTRAPOLATED")).toBeVisible();

    await slider.fill("0");
    await expect(vis(page, "OBSERVED")).toBeVisible();

    await noOverflow(page);
    expect(w.errors, `console errors: ${w.errors.join(" | ")}`).toEqual([]);
    expect(w.failed, `failed API calls: ${w.failed.join(" | ")}`).toEqual([]);
  });

  test("shows the reroute recommendation with its threshold and reason", async ({ page }) => {
    await page.goto("/geofencing?tab=corridor");
    await page.getByRole("button", { name: /Corridor Congestion/ }).click();
    await expect(page.getByText(/Corridor congestion — NH-348/).first()).toBeVisible();

    // The corridor currently carries scenario jam signals, so the trigger fires;
    // when it does it must name the threshold and the segments, not just alert.
    const banner = page.getByText(/Pre-emptive reroute recommended/);
    if (await banner.count()) {
      await expect(banner.first()).toBeVisible();
      await expect(page.getByText(/reached the 0\.7 threshold/).first()).toBeVisible();
      await expect(vis(page, "REROUTE")).toBeVisible();
    } else {
      // Free-flowing is a valid state — it must simply not claim a reroute.
      await expect(page.getByText(/Pre-emptive reroute/)).toHaveCount(0);
    }
  });

  for (const vp of VIEWPORTS) {
    test(`corridor heatmap has no horizontal overflow at ${vp.name} ${vp.width}px`, async ({
      page,
    }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.goto("/geofencing?tab=corridor");
      await page.getByRole("button", { name: /Corridor Congestion/ }).click();
      await expect(page.getByText(/Corridor congestion — NH-348/).first()).toBeVisible();
      await noOverflow(page);
    });
  }
});

// --- SecureVision integration (additive) -------------------------------------
// The assertions here are about the integration's PROMISES, not its data: the
// workbench must load, it must describe itself as clip analysis rather than live
// CCTV, and the existing Camera AI tabs must keep working beside the new one.

test("Video Analytics workbench loads and does not claim live CCTV", async ({ page }) => {
  await page.goto("/video-analytics");
  await expect(page.getByRole("heading", { name: "Video Analytics" })).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByText(/uploaded camera clips/i)).toBeVisible();
  // The integration must never advertise a capability the vendor API lacks.
  await expect(page.getByText(/live cctv/i)).toHaveCount(0);
});

test("Camera AI keeps its existing tabs and gains a SecureVision tab", async ({ page }) => {
  await page.goto("/gate-customs");
  // NOTE: Customs & Gate does not read ?tab= from the URL (pre-existing — the
  // /camera-ai redirect in App.tsx lands on Gate Captures), so the host tab is
  // clicked rather than deep-linked.
  await page.getByRole("button", { name: "Camera AI" }).click();
  for (const tab of ["Counting", "Trailers", "Containers", "SecureVision AI"]) {
    await expect(page.getByRole("button", { name: tab })).toBeVisible({ timeout: 30_000 });
  }
});
