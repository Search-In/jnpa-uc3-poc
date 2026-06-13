import { defineConfig, devices } from "@playwright/test";

// E2E target. Defaults to the nginx production image serving the PWA at
// :3000/pwa (the verification command `open http://localhost:3000/pwa`).
// Override with E2E_BASE_URL to run against the Vite dev server on :3001.
//   - prod image:  http://localhost:3000   (PWA at /pwa/)
//   - dev server:  E2E_BASE_URL=http://localhost:3001  PWA_BASE=/ (or /pwa/)
const BASE_URL = process.env.E2E_BASE_URL || "http://localhost:3000";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    // The PWA pairs via a ?device= param; a phone-sized viewport keeps the
    // mobile layout under test.
    ...devices["Pixel 5"],
  },
  projects: [{ name: "mobile-chrome", use: { ...devices["Pixel 5"] } }],
});
