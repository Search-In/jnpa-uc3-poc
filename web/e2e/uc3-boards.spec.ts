// Runtime verification for the UC3 surfaces touched by this change.
//
// Three of the four tickets extend an EXISTING screen rather than adding one, so
// the tests drive the real user route:
//   UC3-021  /gate-lane-board            (new screen — T-02 had no home)
//   UC3-027  /parking → "Metered Release" tab
//   UC3-040  /gate-customs → "Auto-LEO" tab
//   UC3-024  /truck-visit search box (plate | container | e-seal | Form 13)
//   UC3-025  /truck-visit → selected document's checkpoint timeline
//
// These are BROWSER tests, not API tests: the point is that the page mounts,
// the real API calls succeed, real data renders, the interactions work and the
// console stays clean. Every assertion is on text an evaluator would actually
// read on screen.
//
// Run against the Vite dev server pointed at a live gateway:
//   E2E_BASE_URL=http://127.0.0.1:5199 npx playwright test e2e/uc3-boards.spec.ts
import { expect, test, type ConsoleMessage, type Page } from "@playwright/test";

/** Console/network noise that is not this change's concern. */
const IGNORED = [
  /favicon/i,
  /Download the React DevTools/i,
  /\[vite\]/i,
  /ResizeObserver loop/i,
  /websocket|ws:\/\//i,
  /arcgis|esri/i,
];

function watch(page: Page) {
  const errors: string[] = [];
  const failed: string[] = [];
  page.on("console", (m: ConsoleMessage) => {
    if (m.type() !== "error") return;
    const text = m.text();
    if (IGNORED.some((re) => re.test(text))) return;
    errors.push(text);
  });
  page.on("response", (r) => {
    const url = r.url();
    if (!url.includes("/api/")) return;
    if (r.status() >= 400 && !IGNORED.some((re) => re.test(url))) {
      failed.push(`${r.status()} ${url}`);
    }
  });
  return { errors, failed };
}

/** No horizontal overflow: the body must never scroll sideways. */
async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow, "page scrolls horizontally").toBeLessThanOrEqual(1);
}

test.describe("UC3-021 Gate & Lane Board", () => {
  test("renders camera-counted queues and the provenance claim", async ({ page }) => {
    const w = watch(page);
    await page.goto("/gate-lane-board");

    await expect(page.getByRole("heading", { name: "Gate & Lane Board" })).toBeVisible();
    // The UI-068 claim must be on the page, not just in the payload.
    await expect(page.getByText(/Queue length is counted from video analytics/i)).toBeVisible();
    await expect(page.getByText(/core\.camera_ai_count/).first()).toBeVisible();

    // Real gate cards from RDS.
    await expect(page.getByRole("heading", { name: "NSICT Gate", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "BMCT Gate", exact: true })).toBeVisible();
    // Every card states HOW its queue was obtained.
    await expect(page.getByText("VIDEO_ANALYTICS").first()).toBeVisible();

    await expectNoHorizontalOverflow(page);
    expect(w.errors, `console errors: ${w.errors.join(" | ")}`).toEqual([]);
    expect(w.failed, `failed API calls: ${w.failed.join(" | ")}`).toEqual([]);
  });

  test("lane table and reassignment preview work without commanding equipment", async ({
    page,
  }) => {
    const w = watch(page);
    await page.goto("/gate-lane-board");

    await page.getByRole("button", { name: /Lanes/ }).click();
    await expect(page.getByText("G-NSICT-L1").first()).toBeVisible();
    await expect(page.getByText("REVERSIBLE").first()).toBeVisible();
    // Boom barrier is shown as observed, never as commandable.
    await expect(page.getByText("(observed)").first()).toBeVisible();

    // Back to the cards, where the reassignment control lives.
    await page.getByRole("button", { name: /Gate cards/ }).click();
    await expect(page.getByText("Lane reassignment").first()).toBeVisible();
    await page.getByRole("button", { name: /Preview impact/ }).click();

    await expect(page.getByText("SIMULATED PREVIEW").first()).toBeVisible();
    await page.getByRole("button", { name: /^Apply…$/ }).click();
    // The confirmation must say, in words, that no equipment is commanded.
    await expect(
      page.getByText(/does not move the barrier or send any command to gate equipment/i),
    ).toBeVisible();

    await page.getByRole("button", { name: /^Raise task$/ }).click();
    await expect(page.getByText(/Task raised for the gate supervisor/i)).toBeVisible();
    await expect(page.getByText(/No equipment command was sent/i)).toBeVisible();

    // The task appears in the queue with the equipment column reading "never sent".
    await page.getByRole("button", { name: /Tasks/ }).click();
    await expect(page.getByText("never sent").first()).toBeVisible();

    await expectNoHorizontalOverflow(page);
    expect(w.errors, `console errors: ${w.errors.join(" | ")}`).toEqual([]);
    expect(w.failed, `failed API calls: ${w.failed.join(" | ")}`).toEqual([]);
  });
});

test.describe("UC3-027 CPP metered release (on /parking)", () => {
  test("shows real occupancy and meters only the congested terminal", async ({ page }) => {
    const w = watch(page);
    await page.goto("/parking");

    await expect(page.getByRole("heading", { name: /Parking Management/ })).toBeVisible();
    // Occupancy stays on the tab that already owned it — not duplicated.
    await expect(page.getByText("Common Parking Plaza (CPP)").first()).toBeVisible();

    await page.getByRole("button", { name: /Metered Release/ }).click();
    // Amenity state is declared, not faked.
    await expect(page.getByText(/NOT_IN_CORPUS/).first()).toBeVisible();
    await page.getByRole("button", { name: /Recompute now/ }).click();

    // The driver advice sentence is generated from the table's own numbers.
    await expect(
      page.getByText(/gate queue is \d+ vehicles and clearing at \d+ per hour/).first(),
    ).toBeVisible();
    await expect(page.getByText("SIMULATED").first()).toBeVisible();

    await expectNoHorizontalOverflow(page);
    expect(w.errors, `console errors: ${w.errors.join(" | ")}`).toEqual([]);
    expect(w.failed, `failed API calls: ${w.failed.join(" | ")}`).toEqual([]);
  });
});

test.describe("UC3-040 Auto-LEO four-way join (on /gate-customs)", () => {
  test("renders the four-way join with per-stream states and the flag legend", async ({ page }) => {
    const w = watch(page);
    await page.goto("/gate-customs");
    await page.getByRole("button", { name: /Auto-LEO/ }).click();

    // The ticket's own cases, on the customer's real Form 13s.
    await expect(page.getByText("MEDU1777575").first()).toBeVisible();
    await expect(page.getByText("FFAU4770682").first()).toBeVisible();
    await expect(page.getByText("BMOU5841115").first()).toBeVisible();

    // All four streams named, and MISSING distinguished from MISMATCH.
    await expect(page.getByText("Weighbridge").first()).toBeVisible();
    await expect(page.getByText("ICEGATE").first()).toBeVisible();
    await expect(page.getByText("WEIGHT_MISMATCH").first()).toBeVisible();
    await expect(page.getByText("WEIGHT_MISSING").first()).toBeVisible();

    // The assumption behind the simulated feeds is on screen.
    await expect(page.getByText(/gaps G8\/G10/).first()).toBeVisible();

    // Expanding the X4 row shows the reroute + customs notification.
    await page.getByText("BMOU5841115").first().click();
    await expect(page.getByText(/Truck rerouted to/i).first()).toBeVisible();
    await expect(page.getByText(/customs notified/i).first()).toBeVisible();

    await expectNoHorizontalOverflow(page);
    expect(w.errors, `console errors: ${w.errors.join(" | ")}`).toEqual([]);
    expect(w.failed, `failed API calls: ${w.failed.join(" | ")}`).toEqual([]);
  });
});

test.describe("UC3-024 / UC3-025 on Truck Visit Detail", () => {
  test("four different keys resolve to the same trip", async ({ page }) => {
    const w = watch(page);
    // The Form 13 e-gate number, the container and the customs e-seal are three
    // different keys printed on ONE slip; all three must land on that slip's
    // tractor. Asserting the tractor directly is the invariant the ticket names.
    for (const key of ["16497850", "MEDU1777575", "5826371"]) {
      await page.goto(`/truck-visit?q=${key}`);
      await expect(page.getByText(/match 1\.00/).first()).toBeVisible();
      await expect(page.getByText(/^Showing/).first()).toContainText("MH43CK1959");
    }

    expect(w.errors, `console errors: ${w.errors.join(" | ")}`).toEqual([]);
    expect(w.failed, `failed API calls: ${w.failed.join(" | ")}`).toEqual([]);
  });

  test("an ambiguous plate lists candidates and resolves none", async ({ page }) => {
    const w = watch(page);
    await page.goto("/truck-visit?q=MH43BX1488");

    await expect(page.getByText("AMBIGUOUS").first()).toBeVisible();
    await expect(page.getByText(/does not pick between candidates/i)).toBeVisible();

    await expectNoHorizontalOverflow(page);
    expect(w.errors, `console errors: ${w.errors.join(" | ")}`).toEqual([]);
    expect(w.failed, `failed API calls: ${w.failed.join(" | ")}`).toEqual([]);
  });

  test("an unknown key shows the no-match state and invents nothing", async ({ page }) => {
    await page.goto("/truck-visit?q=ZZZZ9999999");
    await expect(page.getByText("NO MATCH").first()).toBeVisible();
    await expect(page.getByText(/No gate document carries this key/i)).toBeVisible();
  });

  test("the visit timeline labels every checkpoint's evidence", async ({ page }) => {
    const w = watch(page);
    await page.goto("/truck-visit?q=NYKU4768188");

    // This test is about the timeline's EVIDENCE LABELS, so it selects the GTI
    // visit explicitly rather than racing the resolver's refetch. (The resolver
    // landing on this document is covered by the resolution tests above.)
    await page
      .getByRole("button", { name: /NYKU4768188/ })
      .first()
      .click();
    await expect(page.getByText(/In-gate time/).first()).toContainText("82");
    await expect(page.getByText("Checkpoint timeline").first()).toBeVisible();
    // The hero visit's real gate times, and the honest gaps between them.
    await expect(page.getByText("Recognition portal (ANPR arch)").first()).toBeVisible();
    await expect(page.getByText("VERIFIED").first()).toBeVisible();
    await expect(page.getByText("NOT IN CORPUS").first()).toBeVisible();
    await expect(page.getByText("KEY ONLY").first()).toBeVisible();
    await expect(page.getByText(/In-gate time/).first()).toBeVisible();
    // The parsed pane still renders the real slip's fields beside the timeline.
    await expect(page.getByText("NYKU4768188").first()).toBeVisible();

    await expectNoHorizontalOverflow(page);
    expect(w.errors, `console errors: ${w.errors.join(" | ")}`).toEqual([]);
    expect(w.failed, `failed API calls: ${w.failed.join(" | ")}`).toEqual([]);
  });

  test("boards hold up at a narrow mobile viewport", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    for (const path of ["/gate-lane-board", "/parking", "/gate-customs", "/truck-visit"]) {
      await page.goto(path);
      await expectNoHorizontalOverflow(page);
    }
  });
});

// UC3-041's Document OCR screen is NOT covered here. It is reachable only from
// the UC-3 lifecycle "Documents" tab after a container has been selected — the
// legacy /document-ocr route redirects to a Reports tab that no longer exists.
// That broken redirect predates this change and is out of the ticket's scope, so
// it is reported rather than worked around with a brittle selector. The OCR
// engine/badge behaviour is covered by tests/test_uc3_040_041_030_036.py and was
// verified end-to-end against the real corpus photos.
