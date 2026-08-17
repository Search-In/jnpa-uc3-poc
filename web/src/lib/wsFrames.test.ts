/**
 * GAP-WS-02 — the WsFrame union must match what the gateway actually emits.
 *
 * Seven frame types were emitted and silently discarded by a fall-through, and
 * the union did not admit they existed, so "we chose not to handle this" and
 * "we forgot this" were indistinguishable from the code.
 *
 * This test reads the gateway source and the frontend source and compares them,
 * rather than asserting against a list someone typed out — a hand-written list
 * is exactly what went stale in the first place. If a new `ws.broadcast("x")`
 * lands in the gateway, this fails until `x` is either handled or explicitly
 * ignored.
 */
import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

const REPO = resolve(__dirname, "../../..");

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    if (name === "__pycache__" || name === ".venv" || name === "node_modules") continue;
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) walk(p, out);
    else if (name.endsWith(".py")) out.push(p);
  }
  return out;
}

/** Every frame type the gateway puts on a socket. */
function emittedFrameTypes(): Set<string> {
  const types = new Set<string>();
  for (const dir of ["gateway", "services"]) {
    for (const file of walk(join(REPO, dir))) {
      const src = readFileSync(file, "utf8");
      for (const m of src.matchAll(/\.broadcast\(\s*["']([a-z_]+)["']/g)) types.add(m[1]);
    }
  }
  return types;
}

const typesTs = readFileSync(join(REPO, "web/src/lib/types.ts"), "utf8");
const socketTsx = readFileSync(join(REPO, "web/src/hooks/SocketContext.tsx"), "utf8");

/** Members of the WsFrame union, read from its declaration. */
function unionMembers(): Set<string> {
  const start = typesTs.indexOf("export type WsFrame =");
  expect(start).toBeGreaterThan(-1);
  const body = typesTs.slice(start, typesTs.indexOf("\n\n", start));
  return new Set([...body.matchAll(/type:\s*"([a-z_]+)"/g)].map((m) => m[1]));
}

/** Types the provider names — handled inline or listed as ignored. */
function accountedFor(): Set<string> {
  const handled = [...socketTsx.matchAll(/frame\.type === "([a-z_]+)"/g)].map((m) => m[1]);
  const ignoredBlock = socketTsx.slice(socketTsx.indexOf("IGNORED_FRAME_TYPES: ReadonlySet"));
  const ignored = [...ignoredBlock.slice(0, ignoredBlock.indexOf("]);"))
    .matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
  return new Set([...handled, ...ignored]);
}

describe("WsFrame union vs the gateway", () => {
  it("finds the broadcast sites at all (guards the test itself)", () => {
    // If the scan silently matched nothing, every assertion below would pass
    // vacuously — which is the failure mode this whole ticket is about.
    expect(emittedFrameTypes().size).toBeGreaterThan(8);
  });

  it("models every frame the gateway broadcasts", () => {
    const missing = [...emittedFrameTypes()].filter((t) => !unionMembers().has(t));
    expect(missing, `unmodelled frame types: ${missing.join(", ")}`).toEqual([]);
  });

  it("accounts for every modelled frame — handled or explicitly ignored", () => {
    const acc = accountedFor();
    const unaccounted = [...unionMembers()].filter((t) => !acc.has(t));
    expect(
      unaccounted,
      `frames that would fall through silently: ${unaccounted.join(", ")}`,
    ).toEqual([]);
  });

  it("does not model anpr, which is never broadcast", () => {
    // gateway/main.py builds anpr_pump with broadcast=False: ANPR reads are
    // persisted to core.anpr_read only. Modelling it would describe a frame
    // that cannot arrive.
    expect(emittedFrameTypes().has("anpr")).toBe(false);
    expect(unionMembers().has("anpr")).toBe(false);
  });

  it("warns on an unrecognised frame instead of dropping it", () => {
    expect(socketTsx).toMatch(/unhandled frame type/);
  });
});
