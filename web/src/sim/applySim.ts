// applySim — pure merge helpers that overlay the sim overrides on top of the
// adapter's base data. The dashboard (through the SimAdapter wrap) calls these
// so every screen and the map show the live (simulated) values without touching
// the real adapter. All helpers are non-mutating and return new arrays/objects,
// so React change-detection works.
//
// Ported from jnpa_poc_2 apps/web/src/sim/applySim.ts, adapted to UC-III wire
// types (Gate, TrafficSnapshot, TruckDevice, Alert, KpiResult, ParkingFacility).

import type {
  Alert,
  Gate,
  KpiResult,
  ParkingFacility,
  PoliceIncident,
  TasSlot,
  TrafficSnapshot,
  TruckDevice,
} from "@/lib/types";
import { deltaPct as kpiDeltaPct, isOnTarget } from "@/kpi/compute";
import type { SimState } from "./simStore";

const round1 = (x: number) => Math.round(x * 10) / 10;
const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0);
const clamp01 = (x: number) => Math.max(0, Math.min(1, x));

/** True when any sim lever is engaged (so we skip work when idle). */
export function simEngaged(sim: SimState): boolean {
  return (
    Object.keys(sim.gates).length > 0 ||
    Object.keys(sim.segments).length > 0 ||
    sim.flowRate !== 1 ||
    sim.vehicleInjection > 0 ||
    sim.scanQueue != null ||
    sim.parkingDelta !== 0 ||
    sim.incidents.length > 0
  );
}

// ---- Gates ----------------------------------------------------------------

/**
 * Overlay per-gate utilisation / throughput and scale throughput by the global
 * flow rate. Queue length itself isn't a Gate field in UC-III (the dashboard
 * derives it from AT_GATE_QUEUE trucks) — see applyTrucks for that.
 */
export function applyGates(base: Gate[], sim: SimState): Gate[] {
  const hasGateOverrides = Object.keys(sim.gates).length > 0;
  if (!hasGateOverrides && sim.flowRate === 1) return base;
  return base.map((g) => {
    const o = sim.gates[g.id];
    let throughput = g.throughput_60min;
    if (sim.flowRate !== 1) throughput = Math.round(throughput * sim.flowRate);
    if (o?.throughput60min != null) throughput = o.throughput60min;
    const utilisation = o?.utilisation ?? g.utilisation;
    if (throughput === g.throughput_60min && utilisation === g.utilisation) return g;
    return { ...g, throughput_60min: throughput, utilisation };
  });
}

// ---- Traffic snapshots (congestion) ---------------------------------------

/** Overlay per-segment jam factor / speed so the corridor heatmap responds. */
export function applySnapshots(base: TrafficSnapshot[], sim: SimState): TrafficSnapshot[] {
  if (Object.keys(sim.segments).length === 0) return base;
  return base.map((snap) => {
    const o = sim.segments[snap.segment_id];
    if (!o) return snap;
    return {
      ...snap,
      jam_factor: o.jamFactor ?? snap.jam_factor,
      speed_kmh: o.speedKmh ?? snap.speed_kmh,
      source: "sim",
    };
  });
}

// ---- Trucks (gate queues + injection) -------------------------------------

/** A JNPA-area fallback point so injected trucks without a gate anchor still
 *  land in the right region rather than at (0,0). */
const FALLBACK_POINT = { lat: 18.95, lon: 72.95 };

function jitter(seed: number, scale = 0.01): number {
  // Deterministic pseudo-jitter in [-scale, +scale] from an integer seed.
  const x = Math.sin(seed * 12.9898) * 43758.5453;
  return (x - Math.floor(x) - 0.5) * 2 * scale;
}

/**
 * Overlay the truck feed. For each gate with a queueLength override we ensure
 * that many AT_GATE_QUEUE trucks carry that gate_id (the dashboard counts these
 * per gate); plus the global vehicleInjection adds EN_ROUTE_TO_PORT trucks.
 * Synthetic trucks are appended; base trucks are preserved.
 *
 * `state` mirrors the adapter's trucks(state?) filter so an AT_GATE_QUEUE query
 * gets the queue trucks and an unfiltered query gets everything.
 */
export function applyTrucks(base: TruckDevice[], sim: SimState, state?: string): TruckDevice[] {
  const extra: TruckDevice[] = [];

  // Per-gate queue trucks (only relevant to AT_GATE_QUEUE / unfiltered reads).
  if (!state || state === "AT_GATE_QUEUE") {
    let seed = 1;
    for (const [gateId, o] of Object.entries(sim.gates)) {
      const want = o.queueLength ?? 0;
      const have = base.filter((t) => t.gate_id === gateId && t.state === "AT_GATE_QUEUE").length;
      const add = Math.max(0, want - have);
      const lat = o.lat ?? FALLBACK_POINT.lat;
      const lon = o.lon ?? FALLBACK_POINT.lon;
      for (let i = 0; i < add; i++) {
        const s = seed++;
        extra.push({
          device_id: `SIM-Q-${gateId}-${i}`,
          plate: null,
          gate_id: gateId,
          state: "AT_GATE_QUEUE",
          position: { lat: lat + jitter(s), lon: lon + jitter(s + 7) },
          speed_kmh: 0,
          heading: 0,
          remaining_km: 0,
          eta_s: 0,
          segment_id: null,
        });
      }
    }
  }

  // Global vehicle injection (EN_ROUTE_TO_PORT) — corridor inflow.
  if (sim.vehicleInjection > 0 && (!state || state === "EN_ROUTE_TO_PORT")) {
    for (let i = 0; i < sim.vehicleInjection; i++) {
      extra.push({
        device_id: `SIM-INJ-${i}`,
        plate: null,
        gate_id: null,
        state: "EN_ROUTE_TO_PORT",
        position: {
          lat: FALLBACK_POINT.lat + jitter(i, 0.05),
          lon: FALLBACK_POINT.lon + jitter(i + 3, 0.05),
        },
        speed_kmh: 24 + Math.round(jitter(i, 8)),
        heading: 135,
        remaining_km: 6,
        eta_s: 900,
        segment_id: null,
      });
    }
  }

  return extra.length ? [...base, ...extra] : base;
}

// ---- Alerts (incident injection) ------------------------------------------

/** Prepend OPEN injected incidents to the active alert feed as Alert rows.
 *  Cleared (RESOLVED) incidents drop out of the active feed but remain in the
 *  police report (see applyPoliceReport), so the two stay consistent. */
export function applyAlerts(base: Alert[], sim: SimState): Alert[] {
  const open = sim.incidents.filter((i) => i.status === "OPEN");
  if (open.length === 0) return base;
  const injected: Alert[] = open.map((i) => ({
    id: i.id,
    ts: i.ts,
    kind: i.kind,
    severity: i.severity,
    gate_id: i.gate_id,
    plate: null,
    payload: { source: "simulator", segment_id: i.segment_id, scenario: i.scenario },
    ack: false,
  }));
  return [...injected, ...base];
}

// ---- Traffic-Police Reports -----------------------------------------------

/** Human-readable location for an injected incident. */
function incidentLocation(gate_id: string | null, segment_id: string | null): string {
  if (gate_id) return gate_id.replace("G-", "") + " gate";
  if (segment_id) return `NH-348 ${segment_id}`;
  return "NH-348 corridor";
}

/**
 * Surface injected incidents in the Traffic-Police Reports module as report
 * rows, honouring the screen's kind/severity/gate filters. OPEN incidents read
 * as live reports; cleared incidents stay as RESOLVED records (status carried in
 * the payload + `ack`) so the report transitions rather than losing the row.
 */
export function applyPoliceReport(
  base: PoliceIncident[],
  sim: SimState,
  params?: Record<string, string | undefined>,
): PoliceIncident[] {
  if (sim.incidents.length === 0) return base;
  const kindF = params?.kind;
  const sevF = params?.severity;
  const gateF = params?.gate ?? params?.gate_id;
  const rows: PoliceIncident[] = sim.incidents
    .filter(
      (i) =>
        (!kindF || i.kind === kindF) &&
        (!sevF || i.severity === sevF) &&
        (!gateF || i.gate_id === gateF),
    )
    .map((i) => ({
      id: i.id,
      ts: i.ts,
      kind: i.kind,
      severity: i.severity,
      gate_id: i.gate_id,
      plate: null,
      ack: i.status === "RESOLVED",
      payload: {
        source: "simulator",
        status: i.status, // OPEN | RESOLVED — the report's lifecycle state
        location: incidentLocation(i.gate_id, i.segment_id),
        segment_id: i.segment_id,
        scenario: i.scenario,
      },
      evidence_url: null,
    }));
  return [...rows, ...base];
}

// ---- TAS (Terminal Appointment System) ------------------------------------

/** Queue level at/below which appointments are left on schedule. */
const TAS_CONGESTION_THRESHOLD = 20;

/**
 * Reflect simulator gate congestion in the appointment schedule: when a gate's
 * queue override crosses the congestion threshold, its booked slots are pushed
 * back (status → RESCHEDULED, `rescheduled_to` later by a queue-scaled delay),
 * which the TAS widget reads as reduced availability + later turnaround. Below
 * the threshold the schedule is untouched. Deterministic (no clocks/RNG).
 */
export function applyTas(base: TasSlot[], sim: SimState): TasSlot[] {
  if (Object.keys(sim.gates).length === 0) return base;
  return base.map((slot) => {
    const q = sim.gates[slot.gate_id]?.queueLength ?? 0;
    if (q <= TAS_CONGESTION_THRESHOLD || slot.status === "CANCELLED") return slot;
    // Delay grows with how far the queue exceeds the threshold: +2 min per truck.
    const delayMin = Math.round((q - TAS_CONGESTION_THRESHOLD) * 2);
    const to = new Date(new Date(slot.start).getTime() + delayMin * 60000).toISOString();
    return { ...slot, status: "RESCHEDULED", rescheduled_to: to };
  });
}

// ---- Traffic prediction ---------------------------------------------------

/** Raise predicted congestion for segments the sim is congesting. */
export function applyPredict(
  base: { decision_path: string; predictions: Record<string, number> },
  sim: SimState,
): { decision_path: string; predictions: Record<string, number> } {
  if (Object.keys(sim.segments).length === 0) return base;
  const predictions = { ...base.predictions };
  for (const [id, o] of Object.entries(sim.segments)) {
    if (o.jamFactor != null) {
      predictions[id] = Math.max(predictions[id] ?? 0, clamp01(o.jamFactor / 10));
    }
  }
  return { decision_path: base.decision_path, predictions };
}

// ---- Parking / empty pool -------------------------------------------------

/**
 * Spread the parking availability delta across facilities (weighted by current
 * availability) so every facility shifts, making the parking board respond.
 */
export function applyParking(base: ParkingFacility[], sim: SimState): ParkingFacility[] {
  const delta = sim.parkingDelta;
  if (delta === 0 || base.length === 0) return base;
  const total = base.reduce((n, p) => n + Math.max(1, p.available), 0);
  let applied = 0;
  return base.map((p, i) => {
    const share =
      i === base.length - 1
        ? delta - applied
        : Math.round((delta * Math.max(1, p.available)) / total);
    applied += share;
    const available = Math.max(0, Math.min(p.capacity, p.available + share));
    const occupied = Math.max(0, p.capacity - available);
    const utilisation_pct = p.capacity > 0 ? Math.round((occupied / p.capacity) * 100) : 0;
    const status =
      utilisation_pct >= 100 ? "FULL" : utilisation_pct >= 80 ? "FILLING" : "AVAILABLE";
    return { ...p, available, occupied, utilisation_pct, status };
  });
}

// ---- KPIs -----------------------------------------------------------------

/**
 * Per-KPI multiplicative factor the current sim state applies. 1 = unchanged.
 * The factors model how each lever pushes a metric: gate load and corridor
 * congestion worsen waits/dwell/TATs and queue length; higher flow improves
 * throughput but, when congested, hurts it. Factors are intentionally gentle so
 * the board moves believably rather than swinging wildly.
 *
 * Keys match the UC-III KPI set in data/mock.ts: gate_queue_wait, gate_txn_time,
 * trt_empty_ecd, tat_inside_port, queue_length, avg_dwell, gate_throughput.
 */
function kpiFactors(sim: SimState): Partial<Record<string, number>> {
  const gateQs = Object.values(sim.gates).map((g) => g.queueLength ?? 0);
  const gateLoad = clamp01(mean(gateQs) / 30); // 0 (clear) .. 1 (jammed)

  const jamVals = Object.values(sim.segments).map((s) => s.jamFactor ?? 0);
  const congLoad = clamp01(mean(jamVals) / 8); // 0 .. 1

  const injLoad = clamp01(sim.vehicleInjection / 150);

  const flow = sim.flowRate - 1; // -1 .. +2
  const load = clamp01(gateLoad * 0.6 + congLoad * 0.4); // combined corridor stress

  return {
    // lower-is-better: wait/dwell/TATs rise with load
    gate_queue_wait: 1 + load * 0.8 + injLoad * 0.2,
    gate_txn_time: 1 + gateLoad * 0.6,
    avg_dwell: 1 + load * 0.5,
    tat_inside_port: 1 + load * 0.4 + injLoad * 0.2,
    trt_empty_ecd: 1 + congLoad * 0.5,
    // lower-is-better: queue length tracks gate load + injection directly
    queue_length: 1 + gateLoad * 0.9 + injLoad * 0.4,
    // higher-is-better: throughput rises with flow, falls under congestion
    gate_throughput: 1 + clamp01(flow) * 0.3 - load * 0.35,
  };
}

/**
 * Overlay the simulator's effect on the computed KPIs so the headline metrics
 * (KPI strip) move in lock-step with the tiles and map. Each KPI value is
 * scaled by its lever factor and its deltaPct / onTarget recomputed against the
 * unchanged baseline/target (reusing the unit-tested kpi/compute helpers); the
 * trend's last point is nudged to the new value so sparklines track too.
 */
export function applyKpis(base: KpiResult[], sim: SimState): KpiResult[] {
  if (!simEngaged(sim)) return base;
  const factors = kpiFactors(sim);
  return base.map((k) => {
    const f = factors[k.key];
    if (f == null || f === 1) return k;
    const value = round1(Math.max(0, k.value * f));
    const trend = k.trend.length > 0 ? [...k.trend.slice(0, -1), value] : k.trend;
    return {
      ...k,
      value,
      deltaPct: kpiDeltaPct(value, k.baseline),
      onTarget: isOnTarget(value, k.target, k.direction),
      trend,
    };
  });
}
