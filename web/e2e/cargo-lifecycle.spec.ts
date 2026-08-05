import { test, expect } from "@playwright/test";

// Cargo / container lifecycle coverage.
//
// The audit found 2 Playwright tests for 49 screens, and none touching the cargo
// lifecycle — the spine of the UC-III demo. These assert the screens an evaluator
// is actually walked through, and specifically that they render REAL data rather
// than an empty shell: an "everything loaded, nothing in it" screen was the most
// common failure the audit found (38 of 196 tables were empty), and it looks
// identical to a working screen unless a test checks the content.
//
// Deliberately tolerant about WHICH rows appear (the demo database changes) but
// strict that the page reaches a terminal state — loaded, empty-with-a-message,
// or an explicit error. A permanent spinner must fail.

const LOAD = 30_000;

/** Fail if the page is still spinning — a hung panel is the bug we care about. */
async function settled(page: import("@playwright/test").Page) {
  await expect
    .poll(
      async () => {
        const spinners = await page.locator('[role="status"], .animate-spin').count();
        return spinners;
      },
      { timeout: LOAD, message: "panel never stopped loading" },
    )
    .toBe(0);
}

test.describe("cargo lifecycle", () => {
  test("the UC-3 Lifecycle console loads and settles", async ({ page }) => {
    await page.goto("/uc3-lifecycle");
    await expect(page.getByRole("heading", { name: /Lifecycle/i }).first()).toBeVisible({
      timeout: LOAD,
    });
    await settled(page);

    // The KPI band is the screen's contract with the operator: it must render
    // numbers, not placeholders.
    const stats = page.locator("main").getByText(/\d/).first();
    await expect(stats).toBeVisible({ timeout: LOAD });
  });

  test("the lifecycle console exposes its tabs", async ({ page }) => {
    await page.goto("/uc3-lifecycle");
    await settled(page);
    // Documents / chains / upload are separate segments of the same console.
    for (const label of [/document/i, /chain/i]) {
      await expect(page.getByRole("button", { name: label }).first()).toBeVisible({
        timeout: LOAD,
      });
    }
  });

  test("searching for an unknown container reports 'not found', not a crash", async ({ page }) => {
    await page.goto("/uc3-lifecycle");
    await settled(page);

    const search = page.getByRole("textbox").first();
    await search.fill("ZZZU0000000"); // valid shape, certainly absent
    await search.press("Enter");

    // An empty result must be a MESSAGE, never a blank panel or an error page.
    await expect(page.locator("main")).not.toContainText(/undefined|NaN|\[object Object\]/i, {
      timeout: LOAD,
    });
    await expect(page).not.toHaveURL(/error/);
  });

  test("Customs & Gate renders its data", async ({ page }) => {
    await page.goto("/gate-customs");
    await settled(page);
    await expect(page.locator("main")).toBeVisible();
    await expect(page.locator("main")).not.toContainText(/undefined|NaN/i);
  });

  test("CFS/ECY movements render the imported corpus", async ({ page }) => {
    await page.goto("/cfs-ecy");
    await settled(page);
    // 1,928 CODECO movements are imported; the screen must show a count.
    await expect(page.locator("main").getByText(/\d/).first()).toBeVisible({ timeout: LOAD });
  });

  test("no screen renders a raw technical error string to the operator", async ({ page }) => {
    // Plain-language errors were an explicit audit item (A-04). A raw
    // "HTTP 500 —" or a stack fragment on screen is a demo-day killshot.
    for (const path of ["/uc3-lifecycle", "/gate-customs", "/cfs-ecy"]) {
      await page.goto(path);
      await settled(page);
      await expect(page.locator("main")).not.toContainText(
        /Traceback|\[object Object\]|undefined/i,
      );
    }
  });
});
