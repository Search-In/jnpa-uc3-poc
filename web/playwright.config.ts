import { defineConfig, devices } from "@playwright/test";

// E2E target. Defaults to the nginx production image on :3000 (the verification
// command `open http://localhost:3000/live`); override with E2E_BASE_URL to run
// against the Vite dev server on :5173.
const BASE_URL = process.env.E2E_BASE_URL || "http://localhost:3000";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 35_000 },
  fullyParallel: false,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
