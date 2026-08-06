// Unit tests for the sign-in input classifier (node:test — no runner dependency).
// Run with:  npm run test:vehicle-login
//
// These lock in the driver-facing identity rule: a driver signs in with the
// REGISTRATION NUMBER (resolved server-side to the internal TRK id); the id
// forms remain accepted for operations support but are never required.
import assert from "node:assert/strict";
import test from "node:test";
import { execFileSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

// Transpile vehicleLogin.ts with the esbuild that vite already ships.
const here = dirname(fileURLToPath(import.meta.url));
const out = join(mkdtempSync(join(tmpdir(), "jnpa-vlogin-")), "vehicleLogin.mjs");
execFileSync(
  join(here, "../../node_modules/.bin/esbuild"),
  [join(here, "vehicleLogin.ts"), "--format=esm", `--outfile=${out}`],
  { stdio: "pipe" },
);
const { classifyLoginInput, normalizePlate } = await import(pathToFileURL(out).href);

// --- registration numbers (the driver-facing path) --------------------------
test("a state-series registration classifies as a plate", () => {
  assert.deepEqual(classifyLoginInput("MH04LZ1507"), { kind: "plate", value: "MH04LZ1507" });
});

test("lowercase / spaced / hyphenated plates normalise to the canonical form", () => {
  for (const raw of ["mh04lz1507", "MH 04 LZ 1507", "MH-04-LZ-1507"]) {
    assert.deepEqual(classifyLoginInput(raw), { kind: "plate", value: "MH04LZ1507" }, raw);
  }
});

test("a Bharat-series registration classifies as a plate", () => {
  assert.equal(classifyLoginInput("22BH1234AB").kind, "plate");
});

test("single-letter series and 3-digit numbers still match", () => {
  assert.equal(classifyLoginInput("MH4A123").kind, "plate");
});

// --- internal id forms (accepted for ops, never advertised) -----------------
test("a TRK id is accepted verbatim for operational support", () => {
  assert.deepEqual(classifyLoginInput("trk-000011"), { kind: "device", value: "TRK-000011" });
});

test("a bare pairing code maps to the canonical device id", () => {
  assert.deepEqual(classifyLoginInput("11"), { kind: "device", value: "TRK-000011" });
});

// --- rejects ----------------------------------------------------------------
test("garbage, empty and lookalike inputs are invalid", () => {
  for (const raw of ["", "   ", "HELLO", "TRK-11", "MH04", "1234567", "MH04LZ15071234"]) {
    assert.equal(classifyLoginInput(raw).kind, "invalid", JSON.stringify(raw));
  }
});

test("normalizePlate strips separators only — it never invents characters", () => {
  assert.equal(normalizePlate(" mh-04 lz 1507 "), "MH04LZ1507");
});
