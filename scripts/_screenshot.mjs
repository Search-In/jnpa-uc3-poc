// Playwright screenshot helper for scripts/demo_drive.py (Prompt 12, Deliverable 2).
//
// Loads a URL in headless Chromium, stamps a timestamp + caption overlay in the
// top-left, and writes a PNG. Invoked once per demo step by demo_drive.py:
//
//   node scripts/_screenshot.mjs <url> <out.png> "<caption>" "<iso-timestamp>"
//
// Uses the Playwright bundled in web/node_modules (the dashboard's dev dep), so
// no extra install is needed once `make web-build` / `npm install` has run there.
// Exits 0 on success; non-zero (with a message on stderr) on any failure so the
// caller can degrade to "screenshot skipped" without aborting the demo.

import { chromium } from 'playwright';

const [url, outPath, caption = '', stamp = ''] = process.argv.slice(2);

if (!url || !outPath) {
  console.error('usage: node _screenshot.mjs <url> <out.png> [caption] [stamp]');
  process.exit(2);
}

const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  // Best-effort load. networkidle can hang on a live WS dashboard, so cap it and
  // fall back to a fixed settle delay — we want a representative frame, not a
  // pixel-perfect one.
  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 20000 });
  } catch (e) {
    console.error(`warn: goto(${url}) -> ${e.message}; capturing whatever rendered`);
  }
  await page.waitForTimeout(3500); // let the map / live data paint

  // Inject a timestamp + caption overlay so the screenshot is self-describing
  // in the evidence pack (the bid annexure needs to know what/when each is).
  await page.evaluate(
    ({ caption, stamp }) => {
      const bar = document.createElement('div');
      bar.style.cssText = [
        'position:fixed', 'top:0', 'left:0', 'z-index:2147483647',
        'background:rgba(10,14,20,0.82)', 'color:#e6f0ff',
        'font:600 13px/1.4 -apple-system,Segoe UI,Roboto,sans-serif',
        'padding:8px 12px', 'border-bottom-right-radius:8px',
        'box-shadow:0 2px 8px rgba(0,0,0,0.4)', 'max-width:70vw',
      ].join(';');
      const line1 = document.createElement('div');
      line1.textContent = caption || 'JNPA UC-III PoC';
      const line2 = document.createElement('div');
      line2.style.cssText = 'opacity:0.7;font-weight:400;font-size:11px';
      line2.textContent = stamp || new Date().toISOString();
      bar.appendChild(line1);
      bar.appendChild(line2);
      document.body.appendChild(bar);
    },
    { caption, stamp },
  );

  await page.screenshot({ path: outPath, fullPage: false });
  console.log(`screenshot -> ${outPath}`);
  process.exit(0);
} catch (e) {
  console.error(`error: ${e.message}`);
  process.exit(1);
} finally {
  await browser.close();
}
