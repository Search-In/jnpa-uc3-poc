import { test, expect, request as pwRequest } from "@playwright/test";

// Driver notification ISOLATION, in a real browser, across two real PWA sessions.
//
// tests/test_driver_notification_e2e.py proves the gateway ADDRESSES each
// transport correctly. It cannot prove the CLIENT respects that addressing —
// the original bug was two-sided: the gateway fanned out AND the PWA rendered
// any alert that carried no address. A server-side fix with a permissive client
// still leaks the moment anything broadcasts.
//
// This closes the loop with two independently-paired browser contexts:
//
//     Driver A = TRK-000026        Driver B = TRK-000028
//
// An advisory addressed to A must appear on A's screen and must NOT appear on
// B's, with both apps open and connected at the same time.
//
// Non-delivery is asserted WITHOUT a bare timeout: after A's banner appears we
// send a second advisory addressed to B and wait for it on B. Once B's own
// advisory has arrived, B has demonstrably been processing frames — so the
// continued absence of A's advisory on B is a real negative, not a slow socket.

const GATEWAY = process.env.E2E_GATEWAY_URL || "http://localhost:8000";
const PWA_BASE = process.env.PWA_BASE || "/pwa/";
const DRIVER_A = process.env.E2E_DEVICE_A || "TRK-000026";
const DRIVER_B = process.env.E2E_DEVICE_B || "TRK-000028";

const A_TEXT = "ISOLATION-CHECK-ALPHA";
const B_TEXT = "ISOLATION-CHECK-BRAVO";

/** Push a driver-addressed advisory through the same route the alert engine uses. */
async function pushAdvisory(deviceId: string, body: string) {
  const ctx = await pwRequest.newContext({ baseURL: GATEWAY });
  const res = await ctx.post("/api/ai/event", {
    data: {
      device_id: deviceId,
      kind: "WRONG_DIRECTION",
      title: "Advisory",
      body,
      category: "safety",
    },
    headers: { "content-type": "application/json" },
  });
  await ctx.dispose();
  return res;
}

/** Open a paired PWA session for `device` and return its page. */
async function openDriver(browser: import("@playwright/test").Browser, device: string) {
  // A separate context per driver: separate storage, separate service worker,
  // separate WebSocket — i.e. two genuinely different devices, not two tabs
  // sharing one session.
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto(`${PWA_BASE}?device=${device}`);
  await expect(page.getByRole("button", { name: /View Route/ })).toBeVisible({ timeout: 20_000 });
  return { context, page };
}

test("driver A's advisory reaches A and never reaches B", async ({ browser }) => {
  const a = await openDriver(browser, DRIVER_A);
  const b = await openDriver(browser, DRIVER_B);

  try {
    // 1. Advisory addressed to A only.
    const res = await pushAdvisory(DRIVER_A, A_TEXT);
    expect(res.ok(), `gateway rejected the advisory: ${res.status()}`).toBeTruthy();

    // 2. A must see it.
    await expect(a.page.getByText(A_TEXT)).toBeVisible({ timeout: 15_000 });

    // 3. Advisory addressed to B only — this is the synchronisation point that
    //    makes the negative assertion below meaningful.
    const res2 = await pushAdvisory(DRIVER_B, B_TEXT);
    expect(res2.ok()).toBeTruthy();
    await expect(b.page.getByText(B_TEXT)).toBeVisible({ timeout: 15_000 });

    // 4. B has now demonstrably processed a frame of its own, so A's advisory
    //    being absent from B is a real negative rather than a race.
    await expect(b.page.getByText(A_TEXT)).toHaveCount(0);

    // 5. Symmetry: B's advisory must not have leaked onto A either.
    await expect(a.page.getByText(B_TEXT)).toHaveCount(0);
  } finally {
    await a.context.close();
    await b.context.close();
  }
});

test("an unpaired session receives no driver advisories at all", async ({ browser }) => {
  // A browser that never paired has no device binding. It must fail CLOSED:
  // an unaddressed-frame-accepting client would light up here.
  const context = await browser.newContext();
  const page = await context.newPage();
  try {
    await page.goto(PWA_BASE);
    const paired = await openDriver(browser, DRIVER_A);
    try {
      const res = await pushAdvisory(DRIVER_A, A_TEXT);
      expect(res.ok()).toBeTruthy();
      // The paired device confirms the advisory really was delivered...
      await expect(paired.page.getByText(A_TEXT)).toBeVisible({ timeout: 15_000 });
      // ...so its absence on the unpaired session is meaningful.
      await expect(page.getByText(A_TEXT)).toHaveCount(0);
    } finally {
      await paired.context.close();
    }
  } finally {
    await context.close();
  }
});
