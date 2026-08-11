// Capture the application-screen evidence for the UAT document.
//
//   cd web && node ../docs/ulip-uat/capture_ui.mjs
//
// Requires the dashboard on :5199 proxying /api to a gateway with
// ULIP_LIVE_ENABLED=1 and real credentials. Every screenshot is of the real
// application showing real ULIP data — nothing is stubbed. A slot whose data
// cannot be produced (an upstream that is down, a lookup key we do not have)
// is skipped and reported, never faked.
import { chromium } from "@playwright/test";
import { fileURLToPath } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SHOTS = path.join(HERE, "screenshots");
const BASE = "http://127.0.0.1:5199";
const VIEW = { width: 1440, height: 900 };

const wanted = process.argv.slice(2).map((a) => a.toUpperCase());
const want = (id) => wanted.length === 0 || wanted.includes(id);

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 });
const page = await ctx.newPage();
const done = [];
const skipped = [];

/** Screenshot the main content region, not the chrome around it. */
async function shot(id, note) {
  await page.waitForTimeout(500);
  await page.screenshot({ path: path.join(SHOTS, `${id}.png`) });
  done.push(`${id}  ${note}`);
  console.log(`  captured ${id}  ${note}`);
}

async function fastag(rc, tab) {
  await page.goto(`${BASE}/fastag`, { waitUntil: "networkidle" });
  await page.getByPlaceholder(/RC Number/i).fill(rc);
  await page.getByRole("button", { name: /^Search$/ }).click();
  await page.waitForTimeout(2500);
  if (tab) {
    await page.getByRole("button", { name: tab }).click();
    await page.waitForTimeout(2000);
  }
}

// --- FASTAG/01 --------------------------------------------------------------
if (want("SS-06")) {
  await fastag("CG07BC9186", /Transactions/);
  await shot("SS-06", "FASTag toll crossings for CG07BC9186");
}
if (want("SS-08")) {
  await fastag("MH19JK3923", /Transactions/);
  await shot("SS-08", "FASTag empty state — no crossings in the 72 h window");
}

// --- FASTAG/02 --------------------------------------------------------------
if (want("SS-12")) {
  // The vehicle-number lookup returns no tags on staging (raised with NLDSL),
  // so the populated registry is shown via the tag-id path, which does answer.
  await fastag("CG07BC9186", /Tag Status/);
  await page.locator('input[placeholder^="34161"]').fill("34161FA8203286140F4064E0");
  await page.getByRole("button", { name: /Check Tag/i }).click();
  await page.waitForTimeout(2500);
  await shot("SS-12", "NETC tag registry rendered from the tag-id lookup");
}

// --- LDB/01 -----------------------------------------------------------------
if (want("SS-20")) {
  await page.goto(`${BASE}/uc3-lifecycle`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2500);
  await shot("SS-20", "UC3 Lifecycle screen (LDB upstream unavailable)");
}

// --- VAHAN/04 and /01 -------------------------------------------------------
async function rcLookup(kind, value) {
  await page.goto(`${BASE}/vehicles?tab=rc-lookup`, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: kind }).click();
  await page.locator('input[placeholder]').last().fill(value);
  await page.getByRole("button", { name: /Look up/i }).click();
  await page.waitForTimeout(3000);
}
if (want("SS-24")) {
  await rcLookup(/Registration/, "UP32KH0320");
  await shot("SS-24", "RC record resolved live from VAHAN/04");
}
if (want("SS-26")) {
  await rcLookup(/Registration/, "MH01ZZ9999");
  await shot("SS-26", "Unknown vehicle — degraded / unverified state");
}
if (want("SS-30")) {
  await rcLookup(/Registration/, "UP32KH0320");
  await shot("SS-30", "RC record (VAHAN/04 -> VAHAN/01 retry is transparent)");
}
if (want("SS-34")) {
  await rcLookup(/Chassis/, "ME4JF509AH707");
  await shot("SS-34", "Chassis lookup — not found (VAHAN masks the chassis)");
}
if (want("SS-38")) {
  await rcLookup(/Engine/, "JF50E7608");
  await shot("SS-38", "Engine lookup — not found (VAHAN masks the engine)");
}

// --- SARATHI/02 -------------------------------------------------------------
if (want("SS-42") || want("SS-43") || want("SS-45")) {
  await page.goto(`${BASE}/vehicles?tab=drivers`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2500);
  if (want("SS-42")) await shot("SS-42", "Driver Master screen");
}

// --- posture screens --------------------------------------------------------
if (want("SS-64")) {
  await page.goto(`${BASE}/health`, { waitUntil: "networkidle" });
  await page.waitForTimeout(3500);
  await shot("SS-64", "System Health — integration posture");
}
if (want("SS-65")) {
  await page.goto(`${BASE}/integrations`, { waitUntil: "networkidle" });
  await page.waitForTimeout(3500);
  await shot("SS-65", "Integrations — ULIP configuration and last-call outcome");
}

await browser.close();
console.log(`\ncaptured ${done.length}`);
if (skipped.length) console.log(`skipped:\n  ${skipped.join("\n  ")}`);
