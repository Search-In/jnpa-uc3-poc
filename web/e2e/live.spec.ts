import { test, expect } from "@playwright/test";

// Spec: load /live, expect a map canvas, and expect at least one alert chip to
// appear within 30 s on a freshly booted stack. Runs against the nginx image on
// :3000 by default (override with E2E_BASE_URL=http://localhost:5173 for dev).
test("live operations renders the map and a live alert chip", async ({ page }) => {
  await page.goto("/live");

  // The MapLibre container mounts, then it draws a <canvas>.
  await expect(page.getByTestId("live-map")).toBeVisible({ timeout: 15_000 });
  await expect(page.locator('[data-testid="live-map"] canvas')).toBeVisible({ timeout: 15_000 });

  // Alerts now live in a header notification drawer — open it via the bell, then
  // the drawer header is shown.
  await page.getByRole("button", { name: "Active alerts" }).first().click();
  await expect(page.getByRole("heading", { name: "Active alerts" })).toBeVisible();

  // At least one alert row appears within 30 s — seeded from REST and/or pushed
  // over the WebSocket on a freshly booted stack.
  const alertButton = page.locator('[data-testid="alerts-panel"] button').first();
  await expect(alertButton).toBeVisible({ timeout: 30_000 });
});

test("primary navigation reaches every screen", async ({ page }) => {
  await page.goto("/live");
  // Labels are the accessible-name contract for the grouped sidebar (navConfig).
  for (const name of [
    "Driver Advisory",
    "Geo-fencing Manager",
    "Reports & Enforcement",
    "System Health",
    "What-If Console",
    "Live Operations",
  ]) {
    await page.getByRole("link", { name }).click();
    await expect(page).not.toHaveURL(/error/);
  }
});
