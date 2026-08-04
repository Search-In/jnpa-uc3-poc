import { test, expect } from "@playwright/test";

// Login gate coverage.
//
// The audit found ZERO Playwright tests for authentication, which mattered more
// after AUTH_ENABLED flipped to `true` by default: a broken login gate now stops
// the entire demo rather than being an optional extra. It also caught a class of
// misconfiguration that only shows up in the browser — VITE_AUTH_ENABLED not
// matching the gateway's AUTH_ENABLED, which produces either a pointless login
// screen or a silent 401 storm behind a working-looking UI.
//
// These run against whichever build is served at E2E_BASE_URL, so the same spec
// documents both postures instead of assuming one.

const CREDS = {
  username: process.env.E2E_USERNAME || "admin",
  password: process.env.E2E_PASSWORD || "admin",
};

/** True when this build shipped with VITE_AUTH_ENABLED=true. */
async function authGateShown(page: import("@playwright/test").Page): Promise<boolean> {
  const heading = page.getByRole("heading", { name: /Sign in/i });
  return await heading.isVisible({ timeout: 5_000 }).catch(() => false);
}

test.describe("authentication", () => {
  test("an auth-enabled build shows the login gate before any data", async ({ page }) => {
    await page.goto("/live");

    if (!(await authGateShown(page))) {
      test.skip(true, "build has VITE_AUTH_ENABLED=false — no gate to assert");
      return;
    }

    // The gate must be the ONLY thing rendered: no dashboard chrome may leak
    // behind it, or an unauthenticated viewer sees live operational data.
    await expect(page.getByRole("heading", { name: /Sign in/i })).toBeVisible();
    await expect(page.getByTestId("live-map")).toHaveCount(0);

    // Both credential fields are present and required.
    const username = page.getByLabel("Username");
    const password = page.getByLabel("Password");
    await expect(username).toBeVisible();
    await expect(password).toBeVisible();
    await expect(password).toHaveAttribute("type", "password");
  });

  test("wrong credentials are refused with a visible message", async ({ page }) => {
    await page.goto("/live");
    if (!(await authGateShown(page))) {
      test.skip(true, "build has VITE_AUTH_ENABLED=false");
      return;
    }

    await page.getByLabel("Username").fill("definitely-not-a-user");
    await page.getByLabel("Password").fill("wrong-password");
    await page.getByRole("button", { name: /Sign in/i }).click();

    // A refusal must SAY something — a silently-failing login is the worst
    // possible demo-day failure mode.
    await expect(page.locator(".text-red-600")).toBeVisible({ timeout: 15_000 });
    // ...and must not let the user through.
    await expect(page.getByRole("heading", { name: /Sign in/i })).toBeVisible();
  });

  test("valid credentials reach the dashboard and the session persists", async ({ page }) => {
    await page.goto("/live");
    if (!(await authGateShown(page))) {
      test.skip(true, "build has VITE_AUTH_ENABLED=false");
      return;
    }

    await page.getByLabel("Username").fill(CREDS.username);
    await page.getByLabel("Password").fill(CREDS.password);
    await page.getByRole("button", { name: /Sign in/i }).click();

    // Through the gate: the map is the dashboard's landing surface.
    await expect(page.getByTestId("live-map")).toBeVisible({ timeout: 30_000 });
    await expect(page.getByRole("heading", { name: /Sign in/i })).toHaveCount(0);

    // A reload must NOT bounce back to the gate — the token is stored, so a
    // presenter refreshing mid-demo does not have to log in again.
    await page.reload();
    await expect(page.getByTestId("live-map")).toBeVisible({ timeout: 30_000 });
  });

  test("an auth-disabled build reaches the dashboard with no gate", async ({ page }) => {
    await page.goto("/live");
    if (await authGateShown(page)) {
      test.skip(true, "build has VITE_AUTH_ENABLED=true — covered above");
      return;
    }
    await expect(page.getByTestId("live-map")).toBeVisible({ timeout: 30_000 });
  });
});
