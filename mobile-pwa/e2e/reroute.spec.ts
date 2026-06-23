import { test, expect, request as pwRequest } from "@playwright/test";

// E2E (spec): pair a device, trigger a TFC-1-style re-route, and assert the
// full-screen re-route banner appears within 5 s.
//
// The PWA is served at /pwa (nginx prod image) or at PWA_BASE on the dev server.
// We pair instantly via the web-variant ?device= param (no QR/code typing
// needed) and then push a re-route through the SAME gateway endpoint TFC-1 uses
// (POST /api/trucks/{id}/route) — the in-app banner is driven by the resulting
// WS `reroute` frame / polling fallback, exactly as in the live demo.

const GATEWAY = process.env.E2E_GATEWAY_URL || "http://localhost:8000";
const PWA_BASE = process.env.PWA_BASE || "/pwa/";
const DEVICE = process.env.E2E_DEVICE || "TRK-000001";

// Re-route target: any of the four JNPA gates other than the truck's current one.
const ALT_GATE = "G-JNPCT";

async function pushReroute(deviceId: string, gateId: string) {
  const ctx = await pwRequest.newContext({ baseURL: GATEWAY });
  const res = await ctx.post(`/api/trucks/${encodeURIComponent(deviceId)}/route`, {
    data: { gate_id: gateId, force_state: "EN_ROUTE_TO_PORT", reason: "TFC-1 gate closure — re-routing" },
    headers: { "content-type": "application/json" },
  });
  await ctx.dispose();
  return res;
}

test("pair, trigger a TFC-1 re-route, banner appears within 5 s", async ({ page }) => {
  // 1) Pair the device via the web-variant query param.
  await page.goto(`${PWA_BASE}?device=${DEVICE}`);

  // The Trip screen renders once paired (the slot widget label is always there).
  await expect(page.getByText(/Slot at Gate|Slot rescheduled/)).toBeVisible({ timeout: 20_000 });

  // 2) Fire the re-route and start the 5 s clock from that instant.
  const t0 = Date.now();
  const res = await pushReroute(DEVICE, ALT_GATE);
  expect(res.ok(), `reroute POST failed: ${res.status()} ${await res.text()}`).toBeTruthy();

  // 3) The full-screen re-route confirmation must appear within 5 s.
  await expect(page.getByTestId("reroute-screen")).toBeVisible({ timeout: 5_000 });
  const elapsed = Date.now() - t0;
  console.log(`re-route banner appeared in ${elapsed} ms`);
  expect(elapsed).toBeLessThan(5_000);

  // 4) Accept sends state=ACK back and returns to the Trip screen.
  await page.getByTestId("reroute-accept").click();
  await expect(page.getByTestId("reroute-screen")).toBeHidden({ timeout: 10_000 });
});

test("pairing screen renders QR + 6-digit code before pairing", async ({ page }) => {
  // Fresh context (no ?device=) so we land on the pairing screen.
  await page.goto(PWA_BASE);
  await expect(page.getByTestId("pair-qr")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("pair-digit-0")).toBeVisible();
  // The demo-device shortcut pairs and reveals the Trip screen.
  await page.getByTestId("pair-demo").click();
  await expect(page.getByText(/Slot at Gate|Slot rescheduled/)).toBeVisible({ timeout: 20_000 });
});
