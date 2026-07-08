import { chromium } from "@playwright/test";
const BASE="http://localhost:3000", OUT=process.argv[2];
const routes = [
  ["command-center","/command-center","deploy-01-command-center"],
  ["live","/live","deploy-02-live"],
  ["advisory","/advisory","deploy-03-advisory"],
  ["parking","/parking","deploy-04-parking"],
  ["gate-customs","/gate-customs","deploy-05-customs"],
  ["alerts","/alerts","deploy-06-alerts"],
  ["intelligence","/intelligence","deploy-07-intelligence"],
  ["geofencing","/geofencing","deploy-08-geo"],
  ["fastag","/fastag","deploy-09-fastag"],
  ["reports","/reports","deploy-10-reports"],
  ["enrollments","/enrollments","deploy-11-enrolment"],
  ["health","/health","deploy-12-health"],
  ["what-if","/what-if","deploy-13-whatif"],
  ["demo","/demo","deploy-14-demo"],
  ["port-3d","/port-3d","deploy-15-port3d"],
];
const b = await chromium.launch();
const ctx = await b.newContext({ viewport: { width: 1512, height: 950 }, deviceScaleFactor: 2 });
for (const [name, path, shot] of routes) {
  const p = await ctx.newPage();
  const jsErr = [], netErr = [];
  p.on("pageerror", e => jsErr.push(e.message));
  p.on("console", m => { if (m.type()==="error") { const t=m.text(); if (/Failed to load resource|status of \d/.test(t)) netErr.push(t); else jsErr.push(t); } });
  try {
    await p.goto(BASE+path, { waitUntil: "domcontentloaded", timeout: 25000 });
    await p.waitForTimeout(4000);
    await p.screenshot({ path: `${OUT}/${shot}.png` });
    const title = await p.title();
    console.log(`${name.padEnd(16)} | http200 | JS_ERR=${jsErr.length} NET_ERR=${netErr.length} | ${jsErr.slice(0,2).map(e=>e.slice(0,80)).join(" ; ")}`);
  } catch(e) {
    console.log(`${name.padEnd(16)} | FAIL ${e.message.slice(0,60)}`);
  }
  await p.close();
}
await ctx.close(); await b.close();
