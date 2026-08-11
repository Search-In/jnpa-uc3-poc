// Runtime verification for the second UC3 batch. Every ticket extends an
// EXISTING surface, so each test drives the real operator route.
import { expect, test, type ConsoleMessage, type Page } from "@playwright/test";

const IGNORED = [
  /favicon/i,
  /Download the React DevTools/i,
  /\[vite\]/i,
  /ResizeObserver loop/i,
  /websocket|ws:\/\//i,
  /arcgis|esri/i,
];

/** The visible copy of a value. DataTable renders desktop + mobile variants. */
function visibleText(page: Page, text: string) {
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

test.describe("UC3-028 violation queue + UC3-029 hash-chained audit", () => {
  test("queue lists real cases and the audit chain verifies", async ({ page }) => {
    const w = watch(page);
    await page.goto("/reports");
    await page.getByRole("button", { name: /^Violations/ }).click();

    // Queue rendered from real filed cases.
    await expect(page.getByText("Violation queue").first()).toBeVisible();
    await expect(visibleText(page, "MH43CQ2814")).toBeVisible();
    await expect(visibleText(page, "ABANDONED_VEHICLE")).toBeVisible();
    // UI-113: evidence is hash-referenced, and the rule is stated.
    await expect(page.getByText(/referenced by its SHA-256/i).first()).toBeVisible();

    // Open a case -> audit trail + verify chain (UC3-029).
    await visibleText(page, "MH43CQ2814").click();
    await expect(page.getByText(/Audit trail/).first()).toBeVisible();
    await expect(page.getByText(/^sha256:/).first()).toBeVisible();

    await page.getByRole("button", { name: /Verify chain/ }).click();
    await expect(page.getByText(/Chain intact/i)).toBeVisible();

    // UC3-028 escalation ladder: fire it and read the per-channel delivery log.
    await expect(page.getByText(/Escalation ladder/).first()).toBeVisible();
    await page.getByRole("button", { name: /Evaluate ladder/ }).click();
    await expect(page.getByText(/F-08 budget/).first()).toBeVisible();
    // Recipients come from the REAL transporter master, and unsent channels say so.
    await expect(page.getByText("UNAVAILABLE").first()).toBeVisible();
    await expect(page.getByText(/no provider is configured/i).first()).toBeVisible();

    // The challan is always badged SIMULATED (UC3-030 holds here too).
    await expect(page.getByText(/not a legally issued challan/i).first()).toBeVisible();

    await noOverflow(page);
    expect(w.errors, `console errors: ${w.errors.join(" | ")}`).toEqual([]);
    expect(w.failed, `failed API calls: ${w.failed.join(" | ")}`).toEqual([]);
  });

  test("filtering by violation type narrows the queue server-side", async ({ page }) => {
    await page.goto("/reports");
    await page.getByRole("button", { name: /^Violations/ }).click();
    await expect(visibleText(page, "MH43CQ2814")).toBeVisible();

    // By label: /reports has other comboboxes (auto-refresh), so nth() is fragile.
    await page.getByLabel("Filter by violation type").selectOption("WRONG_WAY");
    await expect(visibleText(page, "MH43BX1488")).toBeVisible();
    await expect(page.getByText("MH43CQ2814").filter({ visible: true })).toHaveCount(0);
  });

  test("queue holds up at mobile width", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/reports");
    await page.getByRole("button", { name: /^Violations/ }).click();
    await expect(page.getByText("Violation queue").first()).toBeVisible();
    await noOverflow(page);
  });
});

test.describe("UC3-035 dual turnaround definitions", () => {
  test("both TAT definitions render together with real ground-truth markers", async ({ page }) => {
    const w = watch(page);
    await page.goto("/live");

    await expect(page.getByText("Turn Around Time Inside Port").first()).toBeVisible();
    // UI-122: neither definition may appear alone. Both must be on screen.
    await expect(page.getByText("gate-in to gate-out").first()).toBeVisible();
    await expect(page.getByText("plaza entry to highway exit").first()).toBeVisible();
    await expect(page.getByText(/UI-122/).first()).toBeVisible();

    // The only REAL measured turnarounds in the corpus, from the ticket.
    await expect(page.getByText("165 min").first()).toBeVisible();
    await expect(page.getByText("82 min").first()).toBeVisible();
    await expect(page.getByText("eir4_gateway_one").first()).toBeVisible();

    // UC3-035 distribution: daily average, median, P90 and peak-hour ratio.
    await expect(page.getByText("KPI distribution").first()).toBeVisible();
    await expect(page.getByText("Daily average").first()).toBeVisible();
    await expect(page.getByText("P90").first()).toBeVisible();
    await expect(page.getByText("Peak-hour ratio").first()).toBeVisible();
    // Tender-exact wording (S4: a wrong unit is a free mark lost).
    await expect(page.getByText("Turn Around Time Inside Port").first()).toBeVisible();
    // Provenance is explicit per KPI, never blended.
    await expect(page.getByText(/LIVE · \d+ trips|BASELINE/).first()).toBeVisible();

    await noOverflow(page);
    expect(w.errors, `console errors: ${w.errors.join(" | ")}`).toEqual([]);
  });
});
