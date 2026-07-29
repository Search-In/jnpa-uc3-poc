// Unit tests for the Bhuvan WMS layer helpers (map/bhuvan.ts). The repo has
// no DOM test environment, so — like lib/air_quality.test.ts — they exercise
// the pure config-parsing and toggle/loading/error state machine that drives
// the ArcgisMap layer control; the ArcGIS WMSLayer factory itself is a thin
// pass-through over this validated config.
import { describe, expect, it } from "vitest";

import {
  BHUVAN_SOURCE_LABEL,
  DEFAULT_BHUVAN_OPACITY,
  bhuvanReducer,
  clampOpacity,
  initialBhuvanState,
  parseBhuvanConfig,
  type BhuvanState,
} from "./bhuvan";

const GATEWAY_ANSWER = {
  provider: "BHUVAN",
  enabled: true,
  wms_url: "https://bhuvan-vec1.nrsc.gov.in/bhuvan/wms",
  default_layer: "india3",
  source: "LIVE",
  layers: [
    { name: "india3", title: "India Base Mosaic", type: "WMS" },
    { name: "lulc:MH_LULC50K_1112", title: "Maharashtra LULC 50K", type: "WMS" },
  ],
};

describe("parseBhuvanConfig (layer configuration)", () => {
  it("accepts the gateway /api/bhuvan/layers answer", () => {
    const cfg = parseBhuvanConfig(GATEWAY_ANSWER);
    expect(cfg).not.toBeNull();
    expect(cfg!.wms_url).toBe("https://bhuvan-vec1.nrsc.gov.in/bhuvan/wms");
    expect(cfg!.default_layer).toBe("india3");
    expect(cfg!.enabled).toBe(true);
    expect(cfg!.layers).toHaveLength(2);
    expect(cfg!.layers[0]).toEqual({ name: "india3", title: "India Base Mosaic", type: "WMS" });
  });

  it("accepts the degraded CONFIGURED fallback (provider down, single layer)", () => {
    const cfg = parseBhuvanConfig({
      provider: "BHUVAN",
      enabled: true,
      wms_url: "https://bhuvan-vec1.nrsc.gov.in/bhuvan/wms",
      default_layer: "india3",
      source: "CONFIGURED",
      layers: [{ name: "india3", title: "india3", type: "WMS" }],
    });
    expect(cfg).not.toBeNull();
    expect(cfg!.source).toBe("CONFIGURED");
  });

  it("falls back to the first layer when default_layer is missing", () => {
    const cfg = parseBhuvanConfig({ ...GATEWAY_ANSWER, default_layer: "" });
    expect(cfg!.default_layer).toBe("india3");
  });

  it("marks the disabled posture", () => {
    const cfg = parseBhuvanConfig({ ...GATEWAY_ANSWER, enabled: false });
    expect(cfg!.enabled).toBe(false);
  });

  it("rejects unusable answers instead of crashing", () => {
    expect(parseBhuvanConfig(null)).toBeNull();
    expect(parseBhuvanConfig("nope")).toBeNull();
    expect(parseBhuvanConfig({})).toBeNull();
    // no URL -> nothing to draw from
    expect(parseBhuvanConfig({ ...GATEWAY_ANSWER, wms_url: "" })).toBeNull();
    // no default layer AND no layer list -> nothing to request
    expect(parseBhuvanConfig({ wms_url: "https://x/wms", default_layer: "", layers: [] })).toBeNull();
    // malformed layer entries are dropped, not fatal
    const cfg = parseBhuvanConfig({
      ...GATEWAY_ANSWER,
      layers: [{ name: "india3" }, { title: "no name" }, 42],
    });
    expect(cfg!.layers).toEqual([{ name: "india3", title: "india3", type: "WMS" }]);
  });
});

describe("clampOpacity", () => {
  it("clamps to [0,1] and defaults junk", () => {
    expect(clampOpacity(0.5)).toBe(0.5);
    expect(clampOpacity(-2)).toBe(0);
    expect(clampOpacity(7)).toBe(1);
    expect(clampOpacity("0.25")).toBe(0.25);
    expect(clampOpacity("junk")).toBe(DEFAULT_BHUVAN_OPACITY);
    expect(clampOpacity(NaN)).toBe(DEFAULT_BHUVAN_OPACITY);
  });
});

describe("bhuvanReducer (layer toggle lifecycle)", () => {
  it("starts hidden and idle so the default map is unchanged", () => {
    expect(initialBhuvanState.visible).toBe(false);
    expect(initialBhuvanState.status).toBe("idle");
    expect(initialBhuvanState.opacity).toBe(DEFAULT_BHUVAN_OPACITY);
  });

  it("toggle → loadStart → loadSuccess reaches the enabled/ready state", () => {
    let s: BhuvanState = initialBhuvanState;
    s = bhuvanReducer(s, { type: "toggle" });
    expect(s.visible).toBe(true);
    s = bhuvanReducer(s, { type: "loadStart" });
    expect(s.status).toBe("loading");
    s = bhuvanReducer(s, { type: "loadSuccess" });
    expect(s).toMatchObject({ visible: true, status: "ready", error: null });
  });

  it("toggle off keeps the loaded layer state for an instant re-enable", () => {
    let s: BhuvanState = { visible: true, status: "ready", opacity: 0.8, error: null };
    s = bhuvanReducer(s, { type: "toggle" });
    expect(s.visible).toBe(false);
    expect(s.status).toBe("ready");
  });

  it("a load failure surfaces the error AND unchecks the box", () => {
    let s: BhuvanState = bhuvanReducer(initialBhuvanState, { type: "toggle" });
    s = bhuvanReducer(s, { type: "loadStart" });
    s = bhuvanReducer(s, { type: "loadError", error: "WMS unreachable" });
    expect(s).toMatchObject({ visible: false, status: "error", error: "WMS unreachable" });
  });

  it("re-toggling after an error resets to idle so the fetch is retried", () => {
    const failed: BhuvanState = { visible: false, status: "error", opacity: 0.8, error: "boom" };
    const s = bhuvanReducer(failed, { type: "toggle" });
    expect(s.visible).toBe(true);
    expect(s.status).toBe("idle");
    expect(s.error).toBeNull();
  });

  it("setOpacity clamps and never touches visibility/status", () => {
    const ready: BhuvanState = { visible: true, status: "ready", opacity: 0.8, error: null };
    expect(bhuvanReducer(ready, { type: "setOpacity", opacity: 0.3 }).opacity).toBe(0.3);
    expect(bhuvanReducer(ready, { type: "setOpacity", opacity: 9 }).opacity).toBe(1);
    const s = bhuvanReducer(ready, { type: "setOpacity", opacity: 0.1 });
    expect(s.visible).toBe(true);
    expect(s.status).toBe("ready");
  });
});

describe("attribution", () => {
  it("names ISRO Bhuvan WMS as the source (spec display requirement)", () => {
    expect(BHUVAN_SOURCE_LABEL).toBe("ISRO Bhuvan WMS");
  });
});
