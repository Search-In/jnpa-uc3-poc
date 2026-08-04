// Unit tests for the notification addressing rule (node:test — no test runner
// dependency). Run with:  npm run test:addressing
//
// These lock in the client half of the isolation fix: driver B must not accept
// driver A's advisory, and an advisory with no address must not be accepted by
// anyone (that permissiveness is what produced the leak).
import assert from "node:assert/strict";
import test from "node:test";
import { execFileSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

// Transpile addressing.ts with the esbuild that vite already ships.
const here = dirname(fileURLToPath(import.meta.url));
const out = join(mkdtempSync(join(tmpdir(), "jnpa-addr-")), "addressing.mjs");
execFileSync(
  join(here, "../../node_modules/.bin/esbuild"),
  [join(here, "addressing.ts"), "--format=esm", `--outfile=${out}`],
  { stdio: "pipe" },
);
const { isForThisDriver, isForOtherDevice, isBroadcast } = await import(pathToFileURL(out).href);

const A = "TRK-000001";
const B = "TRK-000002";
const PLATE_A = "MH04AB1234";
const PLATE_B = "MH04CD5678";

test("driver A accepts an advisory addressed to A", () => {
  assert.equal(isForThisDriver({ audience: "driver", device_id: A }, A, PLATE_A), true);
});

test("driver B REJECTS an advisory addressed to A", () => {
  assert.equal(isForThisDriver({ audience: "driver", device_id: A }, B, PLATE_B), false);
});

test("an advisory with NO address is rejected (the original leak)", () => {
  // accidents / blacklist / AI events dispatched without a top-level plate used
  // to pass this check on every device.
  assert.equal(isForThisDriver({ title: "Accident", body: "..." }, A, PLATE_A), false);
  assert.equal(isForThisDriver({ title: "Accident", body: "..." }, B, null), false);
});

test("a driver that does not know its plate yet still rejects others' alerts", () => {
  // Freshly-paired device: plate is null until DriverSession assembles it.
  assert.equal(isForThisDriver({ plate: PLATE_A }, B, null), false);
});

test("plate addressing works when device_id is absent", () => {
  assert.equal(isForThisDriver({ plate: PLATE_A }, A, PLATE_A), true);
  assert.equal(isForThisDriver({ plate: PLATE_A }, B, PLATE_B), false);
});

test("an explicit broadcast reaches every driver", () => {
  assert.equal(isForThisDriver({ audience: "broadcast", kind: "TRAFFIC_CONGESTION" }, A, PLATE_A), true);
  assert.equal(isForThisDriver({ audience: "broadcast", kind: "TRAFFIC_CONGESTION" }, B, null), true);
  assert.equal(isBroadcast({ audience: "broadcast" }), true);
});

test("worker drops frames addressed to another device", () => {
  assert.equal(isForOtherDevice({ device_id: A }, B), true);
  assert.equal(isForOtherDevice({ device_id: A }, A), false);
  assert.equal(isForOtherDevice({ audience: "broadcast", device_id: A }, B), false);
});

test("worker passes unaddressed frames through to the page", () => {
  // traffic / decision / operator_banner carry no device_id and subscribers
  // (MapView, health chips) still need them.
  assert.equal(isForOtherDevice({ segments: [] }, A), false);
  assert.equal(isForOtherDevice(null, A), false);
});

test("malformed payloads are never accepted as ours", () => {
  assert.equal(isForThisDriver(null, A, PLATE_A), false);
  assert.equal(isForThisDriver("nope", A, PLATE_A), false);
});
