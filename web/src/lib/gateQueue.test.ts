// Driver Advisory — the gate-queue source states.
//
// Regression cover for "Driver Advisory shows No trucks currently in a gate
// queue". The screen read only `devices` off GET /api/trucks?state=AT_GATE_QUEUE
// and rendered `devices.length`, so the gateway's "I could not reach the queue
// source" answer (200, devices: [], degraded, state_filter_supported: false)
// was displayed as the operational statement "nobody is queueing", with 0 on
// every gate card.
//
// These assert the four states are distinguishable, and — the part that matters
// operationally — that a count is only ever shown when one was actually
// measured.

import { describe, expect, it } from "vitest";

import {
  deriveQueueState,
  gateDepth,
  isAnswered,
  queueDepthByGate,
  recordAnswer,
  withLastKnownGood,
} from "./gateQueue";
import type { TruckListEnvelope } from "./gateQueue";
import type { TruckDevice } from "./types";

function truck(id: string, gate: string): TruckDevice {
  return {
    device_id: id,
    plate: `MH04${id}`,
    gate_id: gate,
    state: "AT_GATE_QUEUE",
    position: { lat: 18.95, lon: 72.95 },
    speed_kmh: 0,
    heading: 0,
    remaining_km: 0,
    eta_s: 60,
  };
}

const LIVE_QUEUE: TruckListEnvelope = {
  devices: [truck("1", "G-NSICT"), truck("2", "G-NSICT"), truck("3", "G-BMCT")],
  count: 3,
  degraded: false,
  decision_path: "PRIMARY",
  source: "truck-sim",
  state_filter_supported: true,
};

describe("deriveQueueState", () => {
  it("renders a live queue with its rows and depth", () => {
    const s = deriveQueueState({ isLoading: false, isError: false, envelope: LIVE_QUEUE });
    expect(s.status).toBe("ok");
    expect(s.count).toBe(3);
    expect(s.devices).toHaveLength(3);
    expect(s.degraded).toBe(false);
    expect(gateDepth(s, "G-NSICT")).toBe(2);
    expect(gateDepth(s, "G-JNPCT")).toBe(0); // measured: genuinely nobody there
  });

  it("treats a live empty answer as a real, measured zero", () => {
    const s = deriveQueueState({
      isLoading: false,
      isError: false,
      envelope: { devices: [], count: 0, degraded: false, state_filter_supported: true },
    });
    expect(s.status).toBe("empty");
    expect(s.count).toBe(0);
    expect(s.degraded).toBe(false);
    expect(gateDepth(s, "G-NSICT")).toBe(0);
  });

  it("reports an unanswerable queue as UNAVAILABLE, never as an empty queue", () => {
    // The exact body the gateway returns when the truck-sim cannot be reached
    // and the memo has expired (gateway/routers/trucks.py).
    const s = deriveQueueState({
      isLoading: false,
      isError: false,
      envelope: {
        devices: [],
        count: 0,
        degraded: true,
        decision_path: null,
        source: null,
        state_filter_supported: false,
        hint: "TruckState is only known to the truck-sim; start it to filter by state.",
      },
    });
    expect(s.status).toBe("unavailable");
    // THE REGRESSION: no count is claimed, so no card can print 0.
    expect(s.count).toBeNull();
    expect(gateDepth(s, "G-NSICT")).toBeNull();
    expect(s.detail).toContain("truck-sim");
  });

  it("serves the cached queue as DEGRADED with its age", () => {
    const s = deriveQueueState({
      isLoading: false,
      isError: false,
      envelope: {
        ...LIVE_QUEUE,
        degraded: true,
        decision_path: "CACHED",
        source: "memo",
        cache_age_s: 42,
      },
    });
    expect(s.status).toBe("degraded");
    expect(s.count).toBe(3); // real rows, honestly aged
    expect(s.degraded).toBe(true);
    expect(s.detail).toContain("42 s");
  });

  it("renders minutes for an old memo", () => {
    const s = deriveQueueState({
      isLoading: false,
      isError: false,
      envelope: { ...LIVE_QUEUE, degraded: true, cache_age_s: 300 },
    });
    expect(s.detail).toContain("5 min");
  });

  it("separates loading from empty", () => {
    const s = deriveQueueState({ isLoading: true, isError: false, envelope: undefined });
    expect(s.status).toBe("loading");
    expect(s.count).toBeNull();
  });

  it("surfaces a request failure as an error, not as zero trucks", () => {
    const s = deriveQueueState({ isLoading: false, isError: true, envelope: undefined });
    expect(s.status).toBe("error");
    expect(s.count).toBeNull();
    expect(s.devices).toEqual([]);
  });

  it("never renders rows from a source that cannot honour the state filter", () => {
    // Belt-and-braces: even if a rung returned rows alongside
    // state_filter_supported:false, they are NOT the gate queue.
    const s = deriveQueueState({
      isLoading: false,
      isError: false,
      envelope: { ...LIVE_QUEUE, degraded: true, state_filter_supported: false },
    });
    expect(s.status).toBe("unavailable");
    expect(s.devices).toEqual([]);
    expect(s.count).toBeNull();
  });
});

describe("queueDepthByGate", () => {
  it("counts per gate and ignores devices with no gate", () => {
    const devices = [truck("1", "G-NSICT"), truck("2", "G-BMCT"), truck("3", "G-NSICT")];
    devices.push({ ...truck("4", "G-BMCT"), gate_id: null });
    const depth = queueDepthByGate(devices);
    expect(depth.get("G-NSICT")).toBe(2);
    expect(depth.get("G-BMCT")).toBe(1);
    expect(depth.size).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// INTERMITTENCY: the same page showing 3 trucks, then "Gate-queue feed
// unavailable", then 3 trucks again. A missed poll must never erase a queue
// that was correct seconds ago, and an out-of-order response must never win.
// ---------------------------------------------------------------------------
describe("last-known-good queue", () => {
  const answered = deriveQueueState({ isLoading: false, isError: false, envelope: LIVE_QUEUE });
  const failed = deriveQueueState({ isLoading: false, isError: true, envelope: undefined });
  const unavailable = deriveQueueState({
    isLoading: false,
    isError: false,
    envelope: { devices: [], count: 0, degraded: true, state_filter_supported: false },
  });

  it("(a) a valid response shows its trucks", () => {
    expect(answered.status).toBe("ok");
    expect(answered.devices).toHaveLength(3);
    const view = withLastKnownGood(answered, null, 1_000);
    expect(view.count).toBe(3);
  });

  it("(b) a valid EMPTY response is a genuine empty state, even with a baseline", () => {
    const good = recordAnswer(null, answered, 1_000);
    const empty = deriveQueueState({
      isLoading: false,
      isError: false,
      envelope: { devices: [], count: 0, degraded: false, state_filter_supported: true },
    });
    const view = withLastKnownGood(empty, good, 2_000);
    // The source ANSWERED zero — that answer wins over the older non-zero one.
    expect(view.status).toBe("empty");
    expect(view.count).toBe(0);
    expect(view.devices).toEqual([]);
  });

  it("(c) unavailable with NO baseline stays unavailable", () => {
    const view = withLastKnownGood(unavailable, null, 1_000);
    expect(view.status).toBe("unavailable");
    expect(view.count).toBeNull();
  });

  it("(d) a stale response cannot overwrite a newer one", () => {
    // B (newer, 3 trucks) commits; A (older, empty) arrives late and is ignored.
    const b = recordAnswer(null, answered, 2_000);
    const late = deriveQueueState({
      isLoading: false,
      isError: false,
      envelope: { devices: [], count: 0, degraded: false, state_filter_supported: true },
    });
    const after = recordAnswer(b, late, 1_000); // older timestamp
    expect(after?.at).toBe(2_000);
    expect(after?.state.count).toBe(3);
  });

  it("(e) a failed refresh keeps the last known queue, marked stale", () => {
    const good = recordAnswer(null, answered, 10_000);
    const view = withLastKnownGood(failed, good, 40_000);
    expect(view.status).toBe("stale");
    expect(view.devices).toHaveLength(3); // the table is NOT erased
    expect(view.count).toBe(3);
    expect(view.degraded).toBe(true);
    expect(view.detail).toContain("last refresh failed");
    expect(view.detail).toContain("30 s ago");
    // …and the failure never becomes the baseline itself.
    expect(recordAnswer(good, failed, 40_000)).toBe(good);
  });

  it("(e2) an unavailable refresh also keeps the last known queue", () => {
    const good = recordAnswer(null, answered, 10_000);
    const view = withLastKnownGood(unavailable, good, 130_000);
    expect(view.status).toBe("stale");
    expect(view.count).toBe(3);
    expect(view.detail).toContain("2 min ago");
    expect(recordAnswer(good, unavailable, 130_000)).toBe(good);
  });

  it("(f) a refresh in flight keeps the current table instead of blanking it", () => {
    const good = recordAnswer(null, answered, 10_000);
    const loading = deriveQueueState({ isLoading: true, isError: false, envelope: undefined });
    const view = withLastKnownGood(loading, good, 12_000);
    expect(view.status).toBe("ok");
    expect(view.devices).toHaveLength(3);
  });

  it("(g) recovery: the next successful poll replaces the stale view", () => {
    let good = recordAnswer(null, answered, 10_000);
    expect(withLastKnownGood(failed, good, 20_000).status).toBe("stale");
    const fresher = deriveQueueState({
      isLoading: false,
      isError: false,
      envelope: { ...LIVE_QUEUE, devices: [LIVE_QUEUE.devices[0]], count: 1 },
    });
    good = recordAnswer(good, fresher, 30_000);
    const view = withLastKnownGood(fresher, good, 31_000);
    expect(view.status).toBe("ok");
    expect(view.count).toBe(1);
    expect(view.detail).toBeNull();
  });

  it("(h) repeated identical answers classify identically", () => {
    const seen = new Set<string>();
    let good = null as ReturnType<typeof recordAnswer>;
    for (let i = 1; i <= 20; i++) {
      const s = deriveQueueState({ isLoading: false, isError: false, envelope: LIVE_QUEUE });
      good = recordAnswer(good, s, i * 1000);
      seen.add(withLastKnownGood(s, good, i * 1000 + 1).status);
    }
    expect([...seen]).toEqual(["ok"]);
    expect(good?.at).toBe(20_000);
  });

  it("a degraded (memo) answer is still an answer and updates the baseline", () => {
    const memo = deriveQueueState({
      isLoading: false,
      isError: false,
      envelope: { ...LIVE_QUEUE, degraded: true, decision_path: "CACHED", cache_age_s: 12 },
    });
    expect(isAnswered(memo)).toBe(true);
    expect(recordAnswer(null, memo, 5_000)?.at).toBe(5_000);
  });
});

// The registered-driver-device list rides on the SAME envelope as the gate
// queue. That is only safe if it cannot influence what the queue is understood
// to be: a signed-in driver is not a measurement that anyone is queueing.
describe("registered driver devices do not disturb the queue measurement", () => {
  const withRegistered: TruckListEnvelope = {
    ...LIVE_QUEUE,
    registered_devices: [
      {
        device_id: "TRK-000026",
        plate: "MH04QA9911",
        gate_id: null,
        state: null,
        position: null,
        speed_kmh: null,
        heading: null,
        remaining_km: null,
        eta_s: null,
        source: "pwa-registered",
      },
    ],
    registered_count: 1,
  };

  it("keeps the queue count to what the simulator measured", () => {
    const s = deriveQueueState({ isLoading: false, isError: false, envelope: withRegistered });
    expect(s.status).toBe("ok");
    expect(s.count).toBe(3); // NOT 4
    expect(s.devices.map((d) => d.device_id)).not.toContain("TRK-000026");
  });

  it("keeps the per-gate depth cards unchanged", () => {
    const s = deriveQueueState({ isLoading: false, isError: false, envelope: withRegistered });
    expect(gateDepth(s, "G-NSICT")).toBe(2);
    expect(gateDepth(s, "G-BMCT")).toBe(1);
    expect(gateDepth(s, "G-JNPCT")).toBe(0);
  });

  it("still reports a genuinely empty queue as empty when a driver is signed in", () => {
    // The distinction the whole module exists for: somebody being signed in
    // says nothing about whether anybody is queueing.
    const s = deriveQueueState({
      isLoading: false,
      isError: false,
      envelope: { ...withRegistered, devices: [], count: 0 },
    });
    expect(s.status).toBe("empty");
    expect(s.count).toBe(0);
  });

  it("still reports an unanswerable queue as unavailable, never as empty", () => {
    const s = deriveQueueState({
      isLoading: false,
      isError: false,
      envelope: {
        devices: [],
        count: 0,
        degraded: true,
        state_filter_supported: false,
        registered_devices: withRegistered.registered_devices,
        registered_count: 1,
      },
    });
    expect(s.status).toBe("unavailable");
    expect(s.count).toBeNull();
  });
});
