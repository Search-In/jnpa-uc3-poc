// MockAdapter — the zero-credential demo data source. `npm run dev` defaults to
// mock mode (see ./index.ts + VITE_DATA_MODE) and renders the FULL dashboard
// from these deterministic fixtures, with no backend, no camera/Vahan/ULIP/AI
// calls, and no Math.random anywhere. Every number is derived from a seeded
// string hash so the demo is byte-stable across reloads and machines.
//
// Geometry, KPI keys and scenario steps mirror the Python ground truth:
//   - shared/jnpa_shared/corridor.py  (NH-348 waypoints / SEG-00..SEG-12 / zones)
//   - shared/jnpa_shared/kpi.py       (the 7 KPI keys + targets/baselines)
//   - scenarios/tfc1.py|tfc2.py|tfc3.py (the 5-step reactive chains)
// so the on-screen geometry/KPIs match what LiveAdapter would surface.

import type {
  Alert,
  AutoLeoResult,
  CameraHealth,
  CarbonRollup,
  CorridorGeometry,
  CorridorSegment,
  Decision,
  EmptyAllocation,
  FaultControlResult,
  FaultSeverity,
  FaultState,
  Gate,
  IdentityVerifyArg,
  IdentityVerifyResult,
  IdentityEnrolResult,
  KpiResult,
  OperatorBanner,
  ParkingFacility,
  ParkingSummary,
  PoliceIncident,
  Scenario,
  ScenarioStep,
  SourceHealth,
  TrafficSnapshot,
  TruckDevice,
  ViolationCatalogItem,
  ViolationCommitInput,
  ViolationDetectResult,
  ViolationEnforceResult,
  ViolationIncident,
  Zone,
} from "@/lib/types";
import type { TasSlot } from "@/lib/types";
import type { DriverEnrollment } from "@/lib/types";
import type { CongestionMetrics, DataAdapter, DataMode, OcrEval } from "./types";
import { buildKpiResult } from "@/kpi/compute";

// --------------------------------------------------------------------------
// Deterministic helpers (no Math.random — every value is a function of a seed).
// --------------------------------------------------------------------------

/** FNV-1a 32-bit string hash -> unsigned int. Stable across runs/engines. */
function fnv1a(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    // h *= 16777619, kept in 32-bit space via Math.imul.
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** Deterministic float in [0, 1) seeded by an arbitrary string. */
function rand01(seed: string): number {
  return fnv1a(seed) / 0xffffffff;
}

/** Deterministic float in [lo, hi] seeded by a string. */
function randRange(seed: string, lo: number, hi: number): number {
  return lo + rand01(seed) * (hi - lo);
}

/** Deterministic integer in [lo, hi] (inclusive) seeded by a string. */
function randInt(seed: string, lo: number, hi: number): number {
  return lo + (fnv1a(seed) % (hi - lo + 1));
}

/** Deterministically pick an element from `arr` seeded by a string. */
function pick<T>(seed: string, arr: readonly T[]): T {
  return arr[fnv1a(seed) % arr.length];
}

function round(n: number, dp = 2): number {
  const f = 10 ** dp;
  return Math.round(n * f) / f;
}

/** A stable "now" anchor for the demo so timestamps are deterministic. */
const NOW = Date.parse("2026-06-17T06:30:00.000Z");
function iso(offsetSec: number): string {
  return new Date(NOW + offsetSec * 1000).toISOString();
}

// --------------------------------------------------------------------------
// Corridor ground truth (mirrors shared/jnpa_shared/corridor.py WAYPOINTS).
// Stored as (lat, lon) like the Python; converted to [lon, lat] for the wire.
// --------------------------------------------------------------------------

const WAYPOINTS: [number, number][] = [
  [18.9489, 72.9492], // 00 JNPA Gate-1 (NSICT)
  [18.943, 72.954],
  [18.936, 72.9595],
  [18.929, 72.965],
  [18.9215, 72.9705], // 04 Y-junction toward NH-348
  [18.914, 72.976],
  [18.906, 72.9815],
  [18.898, 72.987],
  [18.8895, 72.9925],
  [18.881, 72.998],
  [18.8725, 73.0035],
  [18.864, 73.009],
  [18.856, 73.015], // 12 midway
  [18.848, 73.0215],
  [18.84, 73.0285],
  [18.8325, 73.036],
  [18.825, 73.0435],
  [18.818, 73.0515],
  [18.811, 73.0595],
  [18.804, 73.0675],
  [18.7975, 73.0735],
  [18.791, 73.0775],
  [18.785, 73.079],
  [18.78, 73.08], // 23 Karal Phata junction
];

// Resampled ~1.8 km segments SEG-00..SEG-12 (matches corridor.py _build_segments
// output exactly — see the verified Python dump). Stored (lat, lon).
const SEGMENTS_LATLON: {
  id: string;
  start: [number, number];
  end: [number, number];
  length_km: number;
}[] = [
  { id: "SEG-00", start: [18.9489, 72.9492], end: [18.936, 72.9595], length_km: 1.8 },
  { id: "SEG-01", start: [18.936, 72.9595], end: [18.9228, 72.9695], length_km: 1.8 },
  { id: "SEG-02", start: [18.9228, 72.9695], end: [18.9095, 72.9791], length_km: 1.8 },
  { id: "SEG-03", start: [18.9095, 72.9791], end: [18.8958, 72.9884], length_km: 1.8 },
  { id: "SEG-04", start: [18.8958, 72.9884], end: [18.882, 72.9973], length_km: 1.8 },
  { id: "SEG-05", start: [18.882, 72.9973], end: [18.8682, 73.0063], length_km: 1.8 },
  { id: "SEG-06", start: [18.8682, 73.0063], end: [18.8549, 73.0159], length_km: 1.8 },
  { id: "SEG-07", start: [18.8549, 73.0159], end: [18.8422, 73.0266], length_km: 1.8 },
  { id: "SEG-08", start: [18.8422, 73.0266], end: [18.8303, 73.0382], length_km: 1.8 },
  { id: "SEG-09", start: [18.8303, 73.0382], end: [18.819, 73.0504], length_km: 1.8 },
  { id: "SEG-10", start: [18.819, 73.0504], end: [18.808, 73.0629], length_km: 1.8 },
  { id: "SEG-11", start: [18.808, 73.0629], end: [18.7961, 73.0744], length_km: 1.8 },
  { id: "SEG-12", start: [18.7961, 73.0744], end: [18.78, 73.08], length_km: 1.91 },
];

const CORRIDOR_LENGTH_KM = round(
  SEGMENTS_LATLON.reduce((a, s) => a + s.length_km, 0),
  3,
);

/** [lon, lat] for a (lat, lon) point. */
function lonlat(p: [number, number]): [number, number] {
  return [p[1], p[0]];
}

/** Point at fractional distance `f` (0..1) along the waypoint polyline (lat,lon). */
function pointAlong(f: number): [number, number] {
  const clamped = Math.max(0, Math.min(1, f));
  const span = (WAYPOINTS.length - 1) * clamped;
  const i = Math.min(Math.floor(span), WAYPOINTS.length - 2);
  const t = span - i;
  const a = WAYPOINTS[i];
  const b = WAYPOINTS[i + 1];
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
}

// --------------------------------------------------------------------------
// Static fixture tables.
// --------------------------------------------------------------------------

const GATE_DEFS = [
  { id: "G-NSICT", name: "NSICT Gate-1", lat: 18.9489, lon: 72.9492, target_vph: 60 },
  { id: "G-JNPCT", name: "JNPCT Gate", lat: 18.9512, lon: 72.9505, target_vph: 55 },
  { id: "G-NSIGT", name: "NSIGT Gate", lat: 18.9468, lon: 72.9528, target_vph: 50 },
  { id: "G-BMCT", name: "BMCT (PSA) Gate", lat: 18.944, lon: 72.9555, target_vph: 70 },
] as const;

const TRUCK_STATES = [
  "EN_ROUTE_TO_PORT",
  "EN_ROUTE_TO_PORT",
  "EN_ROUTE_TO_PORT",
  "AT_GATE_QUEUE",
  "AT_GATE_QUEUE",
  "INSIDE_PORT",
  "INSIDE_PORT",
  "GATE_OUT",
  "EN_ROUTE_TO_ECD",
] as const;

const ALERT_KINDS = [
  "WRONG_WAY",
  "ILLEGAL_PARKING",
  "PROVISIONAL_VEHICLE",
  "ELEVATED_SCRUTINY",
  "CUSTOMS_FLAG",
] as const;

const SOURCE_NAMES = [
  "Vahan",
  "Sarathi",
  "FASTag",
  "Traffic",
  "RFID",
  "Trucking",
  "ULIP",
  "Anomaly",
] as const;

// Violation catalog — mirrors the gateway reports._CHALLAN fine schedule so the
// enforcement console shows identical fines in mock and live mode.
const VIOLATION_CATALOG: ViolationCatalogItem[] = [
  { kind: "WRONG_WAY", label: "Wrong-way driving", section: "MVA s.184 (dangerous driving)", fine_inr: 5000 },
  { kind: "ILLEGAL_PARKING", label: "Illegal parking", section: "MVA s.122/177 (obstruction)", fine_inr: 1000 },
  { kind: "OVERSPEEDING", label: "Over-speeding", section: "MVA s.183 (over-speeding)", fine_inr: 2000 },
  { kind: "ROUTE_DEVIATION", label: "Route deviation", section: "JNPA corridor SOP / MVA s.177", fine_inr: 500 },
];

const DEPOTS = ["ECD-DRONAGIRI", "ECD-PANVEL", "CFS-URAN", "CFS-JNPT"] as const;
const CARGO_TYPES = ["container", "oil_tanker", "break_bulk", "cement_bowser"] as const;
const CONTAINER_TYPES = ["20GP", "40HC", "40GP", "20RF"] as const;

const DRIVERS = [
  { driver_id: "DRV-1001", name: "Ramesh Patil", license_no: "MH04 20190012345" },
  { driver_id: "DRV-1002", name: "Suresh Yadav", license_no: "MH43 20171100221" },
  { driver_id: "DRV-1003", name: "Imran Shaikh", license_no: "MH05 20200456789" },
  { driver_id: "DRV-1004", name: "Anil Gaikwad", license_no: "MH46 20180078912" },
  { driver_id: "DRV-1005", name: "Vijay Kamble", license_no: "MH12 20160033445" },
  { driver_id: "DRV-1006", name: "Prakash More", license_no: "MH14 20210099887" },
] as const;

// A tiny placeholder "face" frame (data-URL) so the mock enrolment queue renders
// review thumbnails without bundling real images.
const MOCK_FACE =
  "data:image/svg+xml;base64," +
  btoa(
    '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160"><rect width="160" height="160" fill="#1f78c2"/><circle cx="80" cy="64" r="34" fill="#cfe3f5"/><rect x="36" y="104" width="88" height="56" rx="28" fill="#cfe3f5"/></svg>',
  );

// In-memory enrolment queue for mock mode — seeded with two PENDING requests so
// the admin Driver Enrolment screen has something to review with no backend.
const MOCK_ENROLLMENTS: DriverEnrollment[] = [
  {
    driver_id: "DRV-2001",
    name: "Santosh Jadhav",
    license_no: "MH04 20220011223",
    mobile: "+91 98200 11223",
    vehicle_no: "MH04 AB 1234",
    aadhaar_masked: "XXXX XXXX 4821",
    emergency_contact: "+91 99300 55667",
    status: "PENDING",
    consent: true,
    consent_at: new Date(NOW - 3600 * 1000).toISOString(),
    photo: MOCK_FACE,
    face_images: [MOCK_FACE, MOCK_FACE],
    documents: [],
    submitted_at: new Date(NOW - 3600 * 1000).toISOString(),
  },
  {
    driver_id: "DRV-2002",
    name: "Farhan Ansari",
    license_no: "MH43 20210099001",
    mobile: "+91 98191 22334",
    vehicle_no: "MH43 CD 5678",
    aadhaar_masked: "XXXX XXXX 1190",
    emergency_contact: "+91 90040 77881",
    status: "PENDING",
    consent: true,
    consent_at: new Date(NOW - 7200 * 1000).toISOString(),
    photo: MOCK_FACE,
    face_images: [MOCK_FACE, MOCK_FACE, MOCK_FACE],
    documents: [],
    submitted_at: new Date(NOW - 7200 * 1000).toISOString(),
  },
];

// --------------------------------------------------------------------------
// KPI strip — mirrors shared/jnpa_shared/kpi.py KPI_TARGETS exactly. Each value
// is set on/near target. deltaPct is the raw signed change vs baseline (so a
// lower-is-better improvement reads negative), matching kpi.py._delta_pct.
// --------------------------------------------------------------------------

// KPI arithmetic lives in web/src/kpi/compute.ts (unit-tested, mirrors kpi.py).
type KpiSpec = import("@/kpi/compute").KpiSpec;

const KPI_SPECS: KpiSpec[] = [
  {
    key: "gate_queue_wait",
    label: "Gate Queue Wait Time",
    unit: "min",
    direction: "lower_is_better",
    target: 8.0,
    baseline: 14.5,
    value: 7.4,
  },
  {
    key: "gate_txn_time",
    label: "Avg Gate Transaction Time",
    unit: "min",
    direction: "lower_is_better",
    target: 3.0,
    baseline: 5.2,
    value: 2.8,
  },
  {
    key: "trt_empty_ecd",
    label: "TRT empty from ECD",
    unit: "min",
    direction: "lower_is_better",
    target: 45.0,
    baseline: 72.0,
    value: 43.5,
  },
  {
    key: "tat_inside_port",
    label: "TAT inside port",
    unit: "min",
    direction: "lower_is_better",
    target: 90.0,
    baseline: 135.0,
    value: 88.0,
  },
  {
    key: "queue_length",
    label: "Queue Length",
    unit: "vehicles",
    direction: "lower_is_better",
    target: 25.0,
    baseline: 41.0,
    value: 23.0,
  },
  {
    key: "avg_dwell",
    label: "Avg Vehicle Dwell",
    unit: "min",
    direction: "lower_is_better",
    target: 12.0,
    baseline: 19.0,
    value: 11.6,
  },
  {
    key: "gate_throughput",
    label: "Gate Throughput",
    unit: "vph",
    direction: "higher_is_better",
    target: 60.0,
    baseline: 44.0,
    value: 61.5,
  },
];

function buildKpi(spec: KpiSpec): KpiResult {
  // 8-point sparkline easing from baseline toward the current value (oldest ->
  // newest), with a small deterministic wobble. The value/target/delta/onTarget
  // arithmetic is delegated to the unit-tested kpi/compute.ts (mirrors kpi.py).
  const trend: number[] = [];
  for (let i = 0; i < 8; i++) {
    const f = i / 7;
    const eased = spec.baseline + (spec.value - spec.baseline) * f;
    const wob =
      i === 7
        ? 0
        : (rand01(`${spec.key}-trend-${i}`) - 0.5) * (Math.abs(spec.baseline - spec.value) * 0.12);
    trend.push(round(i === 7 ? spec.value : eased + wob, 2));
  }
  return buildKpiResult(spec, trend);
}

// --------------------------------------------------------------------------
// Auto-LEO queue — a few rows are intentionally blocked with customs flags so
// customsFlags() (and the contract test) have something to surface.
// --------------------------------------------------------------------------

type LeoRow = AutoLeoResult & { _ts: string };

function buildLeoQueue(): LeoRow[] {
  const flagPool = ["ESEAL_TAMPER", "WEIGHT_MISMATCH", "LEO_MISSING"];
  const rows: LeoRow[] = [];
  for (let i = 0; i < 6; i++) {
    const seed = `leo-${i}`;
    const container_no = `MSCU${(7100000 + randInt(seed, 0, 899999)).toString().padStart(7, "0")}`;
    const plate = `MH04${pick(seed + "-l", ["AB", "CK", "GT", "QR"])}${randInt(seed + "-n", 1000, 9999)}`;
    // Rows 1, 3, 5 are blocked (leo_ready=false) with a customs flag.
    const blocked = i % 2 === 1;
    const flag = pick(seed + "-flag", flagPool);
    const checks: Record<string, any> = {
      eseal_ok: !(blocked && flag === "ESEAL_TAMPER"),
      weight_ok: !(blocked && flag === "WEIGHT_MISMATCH"),
      leo_present: !(blocked && flag === "LEO_MISSING"),
      rfid_match: true,
    };
    // Gate-out containers are spread ALONG the JNPA -> Karal Phata corridor
    // (~22 km) at distinct fractions, so clicking different queue/feed rows
    // visibly flies the map to clearly separated locations (not one cluster).
    // gate_id keeps the origin gate for the customs label.
    const gate = pick(seed + "-gate", GATE_DEFS);
    const [lat, lon] = pointAlong(0.08 + i * 0.16);
    rows.push({
      container_no,
      vehicle_plate: plate,
      leo_ready: !blocked,
      checks,
      customs_flags: blocked ? [flag] : [],
      gate_id: gate.id,
      lat,
      lon,
      _ts: iso(-i * 47),
    });
  }
  return rows;
}

// --------------------------------------------------------------------------
// Parking facilities inside the port. `available === capacity - occupied` and
// the summary totals are derived from the same table so they always agree.
// --------------------------------------------------------------------------

const PARKING_DEFS = [
  {
    facility_id: "PK-NSICT-A",
    name: "NSICT Pre-Gate Apron A",
    gate_id: "G-NSICT",
    lat: 18.9482,
    lon: 72.9498,
    capacity: 120,
  },
  {
    facility_id: "PK-JNPCT-B",
    name: "JNPCT Holding Yard B",
    gate_id: "G-JNPCT",
    lat: 18.9505,
    lon: 72.951,
    capacity: 90,
  },
  {
    facility_id: "PK-NSIGT-C",
    name: "NSIGT Buffer C",
    gate_id: "G-NSIGT",
    lat: 18.9462,
    lon: 72.9532,
    capacity: 60,
  },
  {
    facility_id: "PK-BMCT-D",
    name: "BMCT Truck Park D",
    gate_id: "G-BMCT",
    lat: 18.9435,
    lon: 72.956,
    capacity: 150,
  },
  {
    facility_id: "PK-CENTRAL",
    name: "Central Multimodal Yard",
    gate_id: null,
    lat: 18.945,
    lon: 72.953,
    capacity: 200,
  },
  {
    facility_id: "PK-DRONAGIRI",
    name: "Dronagiri Node Park",
    gate_id: null,
    lat: 18.94,
    lon: 72.96,
    capacity: 80,
  },
] as const;

function buildParking(minuteOfDay?: number): ParkingFacility[] {
  // Occupancy breathes deterministically with the time-of-day so the screen
  // changes as you drag a time slider, but is stable for any given minute.
  const min = minuteOfDay ?? 510; // default ~08:30
  return PARKING_DEFS.map((d, i) => {
    const phase = (min / 1440) * Math.PI * 2;
    const wave = (Math.sin(phase + i) + 1) / 2; // 0..1
    const base = randRange(`park-${d.facility_id}`, 0.45, 0.85);
    const util = Math.max(0.05, Math.min(1, base * (0.7 + 0.45 * wave)));
    const occupied = Math.min(d.capacity, Math.round(d.capacity * util));
    const available = d.capacity - occupied;
    const pct = round((occupied / d.capacity) * 100, 1);
    const status: ParkingFacility["status"] =
      available === 0 ? "FULL" : pct >= 80 ? "FILLING" : "AVAILABLE";
    return {
      facility_id: d.facility_id,
      name: d.name,
      gate_id: d.gate_id,
      lat: d.lat,
      lon: d.lon,
      capacity: d.capacity,
      occupied,
      available,
      utilisation_pct: pct,
      status,
    };
  });
}

// --------------------------------------------------------------------------
// Scenario timelines — mirror the 5-step reactive chains in scenarios/tfc*.py.
// --------------------------------------------------------------------------

function tfcFromHandle(handleId: string): "tfc1" | "tfc2" | "tfc3" {
  const h = handleId.toLowerCase();
  if (h.startsWith("tfc3")) return "tfc3";
  if (h.startsWith("tfc2")) return "tfc2";
  return "tfc1";
}

function timelineSteps(handleId: string): ScenarioStep[] {
  const tfc = tfcFromHandle(handleId);
  const base = (
    step_no: number,
    title: string,
    trigger: string,
    detail: Record<string, any>,
    status = "ok",
  ): ScenarioStep => ({
    handle_id: handleId,
    scenario: tfc,
    step_no,
    title,
    status,
    trigger,
    ts: iso(-(6 - step_no) * 12),
    detail,
    trace_id: `trace-${handleId}`,
  });

  if (tfc === "tfc2") {
    return [
      base(1, "Injected wrong-way track at C-KARAL-EXIT (6 pings)", "kafka:truck.telemetry", {
        device_id: `SYN-TFC2-${handleId}`,
        plate: "MH04WW1234",
        heading_deg: 315.0,
        camera_id: "C-KARAL-EXIT",
      }),
      base(2, "Anomaly service emitted WRONG_WAY alert", "anomaly:/alerts/recent", {
        alert_id: "alt-wrongway-01",
        evidence_url: "/evidence/C-KARAL-EXIT-last10s.mp4",
      }),
      base(
        3,
        "e-Challan issued (ECH-2026-0098) — plate resolved via Vahan CACHED",
        "gateway:/api/echallan/issue",
        {
          echallan_id: "ECH-2026-0098",
          vahan_decision_path: "CACHED",
          echallan_pdf_url: "/api/echallan/ECH-2026-0098.pdf",
        },
      ),
      base(4, "Alert payload updated with echallan_id + echallan_pdf_url", "scenario.tfc2", {
        echallan_id: "ECH-2026-0098",
        echallan_pdf_url: "/api/echallan/ECH-2026-0098.pdf",
      }),
      base(5, "Evidence clip (last 10 s) available for the alert drawer", "frame-bus", {
        evidence_mp4_url: "http://localhost:9000/evidence/C-KARAL-EXIT-last10s.mp4",
        camera_id: "C-KARAL-EXIT",
      }),
    ];
  }

  if (tfc === "tfc3") {
    return [
      base(1, "UC-II published cargo.dpd_release spike x2.5", "kafka:cargo.dpd_release", {
        cross_twin: "UC-II -> UC-III",
        event: { dpd_release_spike: 2.5, window_min: 40, source: "UC-II" },
      }),
      base(
        2,
        "uc2_bridge -> 600 trucks/h over 40 min; instantiated 300 on the corridor",
        "scenarios.uc2_bridge",
        {
          demand_profile: { trucks_per_h: 600, window_min: 40, total_trucks: 400 },
          injected: 300,
          capped_at: 300,
        },
      ),
      base(
        3,
        "Forecaster predicts build-up on NH-348 segments 8-14 (5 segments >= P0.6)",
        "congestion:/predict",
        {
          assert_threshold: 0.6,
          need: 5,
          met: true,
          crossed_segments: ["SEG-08", "SEG-09", "SEG-10", "SEG-11", "SEG-12"],
          probs: {
            "SEG-07": 0.58,
            "SEG-08": 0.71,
            "SEG-09": 0.74,
            "SEG-10": 0.69,
            "SEG-11": 0.66,
            "SEG-12": 0.63,
          },
        },
      ),
      base(
        4,
        "Driver-advisory reissued gate-slot windows for 42 trucks (PWA push queued)",
        "driver-advisory:/api/trucks/{id}/route",
        {
          push_count: 42,
          pushes: [{ device_id: "SYN-TFC3-001", pwa_push: "gate-slot-window-reissued" }],
        },
      ),
      // TFC-3 must include the cross-twin step.
      base(5, "Cross-twin link: UC-II DPD release -> UC-III corridor demand", "cross-twin", {
        arrow: { from: "UC-II DPD release", to: "UC-III demand" },
        multiplier: 2.5,
        cross_twin: true,
      }),
    ];
  }

  // tfc1 — gate closure
  return [
    base(1, "Gate G-NSICT marked CLOSED", "scenario.tfc1", {
      gate_id: "G-NSICT",
      duration_minutes: 120,
    }),
    base(2, "Injected 80 AT_GATE_QUEUE trucks at G-NSICT", "truck-sim:/devices/inject", {
      injected: 80,
      segments_nudged: ["SEG-00", "SEG-01", "SEG-02", "SEG-03"],
    }),
    base(
      3,
      "Congestion forecaster predicts spillover to G-JNPCT & G-NSIGT (P>=0.7)",
      "congestion:/predict",
      {
        assert_threshold: 0.7,
        met: true,
        crossed_segments: ["SEG-00", "SEG-01", "SEG-02"],
        spillover_gates: ["G-JNPCT", "G-NSIGT"],
        probs: { "SEG-00": 0.82, "SEG-01": 0.79, "SEG-02": 0.74, "SEG-03": 0.68 },
      },
    ),
    base(
      4,
      "Auto-re-routed 23 EN_ROUTE_TO_PORT trucks off G-NSICT",
      "driver-advisory:/api/routing/best_alt_gate",
      { rerouted_count: 23, trucks: [{ device_id: "TRK-0007", from: "G-NSICT", to: "G-BMCT" }] },
    ),
    base(5, "TAS marked 17 slots RESCHEDULED at G-NSICT", "tas-mock:/api/tas/reschedule", {
      rescheduled: 17,
      to_gate: "G-BMCT",
    }),
  ];
}

// --------------------------------------------------------------------------
// Fault-injection control surface — mirrors the gateway exactly so mock-mode
// demos behave identically to live. The rung order matches the backend; the
// first (natural LIVE/PRIMARY) rung is the un-forced default. Severity is a
// pure function of the forced rung (no Math.random — deterministic by rule).
// --------------------------------------------------------------------------

const FAULT_DOMAINS = ["camera", "vahan", "trucks"] as const;
type FaultDomain = (typeof FAULT_DOMAINS)[number];

const FAULT_RUNGS: Record<FaultDomain, string[]> = {
  camera: ["LIVE", "CACHED", "SYNTHETIC"],
  vahan: ["LIVE_PRIMARY", "LIVE_FALLBACK", "CACHED", "PROVISIONAL"],
  trucks: ["PRIMARY", "SECONDARY", "TERTIARY"],
};

/** Severity for a forced rung, mirroring the gateway's rules. */
function rungSeverity(domain: FaultDomain, rung: string): FaultSeverity {
  if (domain === "camera") {
    if (rung === "SYNTHETIC") return "RED";
    if (rung === "CACHED") return "AMBER";
    return "GREEN";
  }
  if (domain === "vahan") {
    if (rung === "PROVISIONAL") return "RED";
    if (rung === "CACHED") return "AMBER";
    return "GREEN";
  }
  // trucks
  if (rung === "TERTIARY") return "RED";
  if (rung === "SECONDARY") return "AMBER";
  return "GREEN";
}

/** Roll up the per-domain forced rungs into the operator-banner summary. */
function bannerFrom(forced: Partial<Record<FaultDomain, string>>): OperatorBanner {
  const domains = (Object.keys(forced) as FaultDomain[]).filter((d) => forced[d]);
  const severities = domains.map((d) => rungSeverity(d, forced[d]!));
  const severity: FaultSeverity | null = severities.includes("RED")
    ? "RED"
    : severities.includes("AMBER")
      ? "AMBER"
      : severities.length
        ? "GREEN"
        : null;
  return { active: domains.length > 0, domains, severity };
}

// --------------------------------------------------------------------------
// The adapter.
// --------------------------------------------------------------------------

// Sentinel proving MockAdapter is linked into a bundle. A production (live)
// build dead-code-eliminates MockAdapter, so this literal MUST NOT appear in the
// shipped JS — the deploy guard (web/Dockerfile, scripts/verify_web_live_build.sh)
// greps for it and fails the build if found. Referenced in the constructor so it
// shares MockAdapter's liveness (it is removed iff MockAdapter is removed).
const MOCK_ADAPTER_SENTINEL = "JNPA_MOCK_ADAPTER_PRESENT_DO_NOT_SHIP";

export class MockAdapter implements DataAdapter {
  readonly mode: DataMode = "mock";

  // Per-device gate overrides applied by reroute(). The fixture generator is
  // deterministic, so without this a re-routed truck would snap back to its
  // seeded gate on the next poll. Persisting the override here lets the UI
  // reflect an updated recommended gate immediately after a successful save.
  private gateOverrides = new Map<string, string>();

  constructor() {
    // Defence in depth: a live build never constructs this (the branch is
    // DCE'd), but if one ever did, fail loudly instead of silently serving
    // fixtures in production.
    if (__JNPA_DATA_MODE__ === "live") {
      throw new Error(`MockAdapter constructed in a live build (${MOCK_ADAPTER_SENTINEL})`);
    }
  }

  // In-memory forced rungs (per adapter instance). force/clear mutate this and
  // severity/banner are recomputed by rule, so the demo is fully deterministic.
  private forcedRungs: Partial<Record<FaultDomain, string>> = {};

  // ---- geometry ----------------------------------------------------------
  gates(): Promise<Gate[]> {
    const gates: Gate[] = GATE_DEFS.map((g) => {
      const throughput = randInt(
        `gate-tp-${g.id}`,
        Math.round(g.target_vph * 0.7),
        Math.round(g.target_vph * 1.1),
      );
      return {
        id: g.id,
        name: g.name,
        lat: g.lat,
        lon: g.lon,
        target_vph: g.target_vph,
        throughput_60min: throughput,
        utilisation: round(throughput / g.target_vph, 3),
      };
    });
    return Promise.resolve(gates);
  }

  corridor(): Promise<CorridorGeometry> {
    const segments: CorridorSegment[] = SEGMENTS_LATLON.map((s) => ({
      id: s.id,
      start: lonlat(s.start),
      end: lonlat(s.end),
      length_km: s.length_km,
    }));
    return Promise.resolve({
      name: "NH-348 JNPA -> Karal Phata",
      polyline: WAYPOINTS.map(lonlat),
      segments,
      length_km: CORRIDOR_LENGTH_KM,
      segment_count: segments.length,
    });
  }

  // ---- live state --------------------------------------------------------
  trafficSnapshots(): Promise<TrafficSnapshot[]> {
    const snaps: TrafficSnapshot[] = SEGMENTS_LATLON.map((s, i) => {
      // A handful of near-port + midway segments are deliberately congested.
      const congested = i <= 1 || i === 6 || i === 9;
      const speed = congested ? randRange(`spd-${s.id}`, 6, 16) : randRange(`spd-${s.id}`, 28, 52);
      const jam = congested
        ? randRange(`jam-${s.id}`, 6.5, 9.5)
        : randRange(`jam-${s.id}`, 0.5, 3.5);
      return {
        segment_id: s.id,
        ts: iso(-randInt(`ts-${s.id}`, 5, 90)),
        speed_kmh: round(speed, 1),
        jam_factor: round(jam, 2),
        source: congested ? "HERE" : pick(`src-${s.id}`, ["HERE", "TomTom", "FASTag-derived"]),
      };
    });
    return Promise.resolve(snaps);
  }

  trafficPredict(
    _horizon = 15,
  ): Promise<{ decision_path: string; predictions: Record<string, number> }> {
    const predictions: Record<string, number> = {};
    SEGMENTS_LATLON.forEach((s, i) => {
      const congested = i <= 1 || i === 6 || i === 9;
      predictions[s.id] = round(
        congested ? randRange(`pred-${s.id}`, 0.62, 0.9) : randRange(`pred-${s.id}`, 0.08, 0.45),
        3,
      );
    });
    return Promise.resolve({ decision_path: "SYNTHETIC", predictions });
  }

  trucks(state?: string, limit = 300): Promise<TruckDevice[]> {
    const out: TruckDevice[] = [];
    for (let i = 0; i < 40; i++) {
      const seed = `truck-${i}`;
      const st = pick(seed + "-state", TRUCK_STATES);
      // Position along the corridor depends on state (queue/inside near port).
      let f: number;
      if (st === "EN_ROUTE_TO_PORT") f = randRange(seed + "-f", 0.25, 0.95);
      else if (st === "EN_ROUTE_TO_ECD") f = randRange(seed + "-f", 0.3, 1.0);
      else f = randRange(seed + "-f", 0.0, 0.08); // queue / inside / gate-out hug the port
      const [lat, lon] = pointAlong(f);
      const deviceId = `TRK-${(1000 + i).toString()}`;
      const gate = this.gateOverrides.get(deviceId) ?? pick(seed + "-gate", GATE_DEFS).id;
      const remaining_km = round(f * CORRIDOR_LENGTH_KM, 2);
      const moving = st === "EN_ROUTE_TO_PORT" || st === "EN_ROUTE_TO_ECD";
      const speed = moving ? randRange(seed + "-spd", 22, 48) : randRange(seed + "-spd", 0, 4);
      const eta_s = st === "EN_ROUTE_TO_PORT" ? randInt(seed + "-eta", 240, 2400) : null;
      const nearest =
        SEGMENTS_LATLON[
          Math.min(Math.floor(f * SEGMENTS_LATLON.length), SEGMENTS_LATLON.length - 1)
        ].id;
      out.push({
        device_id: deviceId,
        plate: `MH${randInt(seed + "-rto", 1, 48)
          .toString()
          .padStart(
            2,
            "0",
          )}${pick(seed + "-ser", ["AB", "CK", "GT", "QR", "ZX"])}${randInt(seed + "-num", 1000, 9999)}`,
        gate_id: gate,
        state: st,
        position: { lat: round(lat, 6), lon: round(lon, 6) },
        speed_kmh: round(speed, 1),
        heading: randInt(seed + "-hdg", 100, 170),
        remaining_km,
        eta_s,
        segment_id: nearest,
      });
    }
    const filtered = state ? out.filter((t) => t.state === state) : out;
    return Promise.resolve(filtered.slice(0, limit));
  }

  reroute(
    deviceId: string,
    body: { gate_id?: string; lat?: number; lon?: number; force_state?: string },
  ): Promise<{ rerouted: boolean }> {
    // Persist the new gate so the next trucks() poll returns it (see
    // gateOverrides above). Mirrors the live POST /api/trucks/{id}/route.
    if (body.gate_id) this.gateOverrides.set(deviceId, body.gate_id);
    return Promise.resolve({ rerouted: true });
  }

  // ---- alerts ------------------------------------------------------------
  alerts(params?: { since?: string; kind?: string; limit?: number }): Promise<Alert[]> {
    const all: Alert[] = [];
    for (let i = 0; i < 14; i++) {
      const seed = `alert-${i}`;
      const kind = ALERT_KINDS[i % ALERT_KINDS.length];
      const sev: Alert["severity"] =
        kind === "WRONG_WAY"
          ? "REPORT_TO_POLICE"
          : pick(seed + "-sev", ["info", "warning", "critical"]);
      const gate = pick(seed + "-gate", GATE_DEFS).id;
      const plate = `MH${randInt(seed + "-rto", 1, 48)
        .toString()
        .padStart(
          2,
          "0",
        )}${pick(seed + "-ser", ["AB", "CK", "WW", "QR"])}${randInt(seed + "-num", 1000, 9999)}`;
      all.push({
        id: `alt-${i.toString().padStart(3, "0")}`,
        ts: iso(-i * 137),
        kind,
        severity: sev,
        gate_id: kind === "ILLEGAL_PARKING" || kind === "WRONG_WAY" ? null : gate,
        plate,
        payload: {
          camera_id: pick(seed + "-cam", [
            "C-KARAL-EXIT",
            "C-NSICT-1",
            "C-YJUNCTION",
            "C-WEIGHBRIDGE",
          ]),
          zone_id: kind === "ILLEGAL_PARKING" ? "NPZ-YJUNCTION" : undefined,
          confidence: round(randRange(seed + "-conf", 0.72, 0.98), 2),
        },
        ack: false,
      });
    }
    let out = all;
    if (params?.kind) out = out.filter((a) => a.kind === params.kind);
    if (params?.limit) out = out.slice(0, params.limit);
    return Promise.resolve(out);
  }

  // ---- kpi / health ------------------------------------------------------
  kpiStrip(): Promise<KpiResult[]> {
    return Promise.resolve(KPI_SPECS.map(buildKpi));
  }

  sources(): Promise<SourceHealth[]> {
    const out: SourceHealth[] = SOURCE_NAMES.map((name, i) => {
      // FASTag + ULIP are DEGRADED; everything else LIVE.
      const degraded = name === "FASTag" || name === "ULIP";
      return {
        source: name,
        state: degraded ? "DEGRADED" : "LIVE",
        last_ok: iso(-randInt(`src-ok-${name}`, 2, degraded ? 240 : 30)),
        latency_p95_ms: degraded
          ? randInt(`src-lat-${name}`, 900, 2400)
          : randInt(`src-lat-${name}`, 40, 280),
        last_decision_path: degraded
          ? "CACHED"
          : i % 3 === 0
            ? "LIVE"
            : pick(`src-dp-${name}`, ["LIVE", "LIVE", "CACHED"]),
      };
    });
    return Promise.resolve(out);
  }

  cameras(): Promise<CameraHealth[]> {
    const ids = [
      "C-NSICT-1",
      "C-JNPCT-1",
      "C-YJUNCTION",
      "C-FLYOVER-RAMP",
      "C-WEIGHBRIDGE",
      "C-KARAL-EXIT",
    ];
    const out: CameraHealth[] = ids.map((id, i) => {
      const dp = i === 3 ? "CACHED" : i === 5 ? "SYNTHETIC" : "LIVE";
      return {
        camera_id: id,
        decision_path: dp,
        frame_age_s: dp === "SYNTHETIC" ? null : randInt(`cam-${id}`, 0, dp === "CACHED" ? 45 : 3),
      };
    });
    return Promise.resolve(out);
  }

  decisions(apiName?: string, limit = 200): Promise<Decision[]> {
    const apis = [
      "congestion.predict",
      "anomaly.classify",
      "vahan.lookup",
      "identity.verify",
      "routing.best_alt_gate",
    ];
    const out: Decision[] = [];
    for (let i = 0; i < 12; i++) {
      const seed = `dec-${i}`;
      const api = apis[i % apis.length];
      out.push({
        api,
        key: pick(seed + "-key", ["SEG-02", "MH04WW1234", "DRV-1003", "G-NSICT"]),
        decision_path: pick(seed + "-dp", ["LIVE", "LIVE", "CACHED", "SYNTHETIC"]),
        latency_ms: randInt(seed + "-lat", 12, 320),
        ts: iso(-i * 53),
        detail: { ok: true, model: api.split(".")[0] },
      });
    }
    const filtered = apiName ? out.filter((d) => d.api === apiName) : out;
    return Promise.resolve(filtered.slice(0, limit));
  }

  // ---- zones -------------------------------------------------------------
  zones(): Promise<Zone[]> {
    // Built from corridor.py NO_PARK_ZONES centroids; rings are [lon,lat] boxes.
    const defs = [
      {
        id: "NPZ-GATE-NSICT",
        name: "NSICT Gate-1 apron",
        kind: "no_parking" as const,
        c: [18.9489, 72.9492] as [number, number],
      },
      {
        id: "NPZ-YJUNCTION",
        name: "NH-348 Y-junction",
        kind: "no_parking" as const,
        c: [18.9215, 72.9705] as [number, number],
      },
      {
        id: "NPZ-WEIGHBRIDGE",
        name: "KM-12 weighbridge approach",
        kind: "restricted" as const,
        c: [18.84, 73.03] as [number, number],
      },
      {
        id: "NPZ-KARAL-JUNCTION",
        name: "Karal Phata junction",
        kind: "no_parking" as const,
        c: [18.78, 73.08] as [number, number],
      },
    ];
    const hLat = 0.0005;
    const hLon = 0.0005 / Math.cos((18.9 * Math.PI) / 180);
    const out: Zone[] = defs.map((d, i) => {
      const [lat, lon] = d.c;
      const ring: [number, number][] = [
        [lon - hLon, lat - hLat],
        [lon + hLon, lat - hLat],
        [lon + hLon, lat + hLat],
        [lon - hLon, lat + hLat],
        [lon - hLon, lat - hLat],
      ];
      return {
        id: d.id,
        name: d.name,
        kind: d.kind,
        polygon: ring,
        escalation: { warn_min: 2, notice_min: 5, challan_min: 10 },
        enabled: i !== 2, // the weighbridge zone is disabled to show the toggle
        updated_at: iso(-3600 * (i + 1)),
      };
    });
    return Promise.resolve(out);
  }

  putZones(zones: Zone[]): Promise<{ saved: boolean; count: number }> {
    return Promise.resolve({ saved: true, count: zones.length });
  }

  // ---- police reports ----------------------------------------------------
  policeReport(_params?: Record<string, string | undefined>): Promise<PoliceIncident[]> {
    const out: PoliceIncident[] = [];
    for (let i = 0; i < 3; i++) {
      const seed = `pol-${i}`;
      const plate = `MH04WW${randInt(seed + "-num", 1000, 9999)}`;
      out.push({
        id: `pol-${i.toString().padStart(3, "0")}`,
        ts: iso(-i * 600 - 120),
        kind: "WRONG_WAY",
        severity: "REPORT_TO_POLICE",
        gate_id: null,
        plate,
        payload: { camera_id: "C-KARAL-EXIT", heading_deg: 315 },
        ack: false,
        rc: {
          owner_name: pick(seed + "-own", [
            "Konkan Logistics",
            "Sahyadri Carriers",
            "Mumbai Freight Co",
          ]),
          maker_model: pick(seed + "-mk", [
            "TATA LPT 3118",
            "Ashok Leyland 2820",
            "Eicher Pro 6028",
          ]),
          reg_state: "Maharashtra",
        },
        challan: {
          echallan_id: `ECH-2026-${(90 + i).toString().padStart(4, "0")}`,
          amount_inr: 5000,
          section: "Rule 119 MVA — wrong-way driving",
        },
        evidence_url: `/evidence/C-KARAL-EXIT-${i}-last10s.mp4`,
      });
    }
    return Promise.resolve(out);
  }

  policePdfUrl(_params?: Record<string, string | undefined>): string {
    // Mock can't render a real PDF; return the harmless gateway path so the
    // button is a no-op link rather than a broken blob.
    return "/api/reports/police?format=pdf";
  }

  async downloadPolicePdf(_params?: Record<string, string | undefined>): Promise<void> {
    // No backend in mock mode — open the (no-op) path in a new tab so the button
    // does something visible without throwing. The live adapter streams the real
    // PDF with the bearer token attached.
    window.open(this.policePdfUrl(_params), "_blank", "noreferrer");
  }

  // ---- vehicle violation detection --------------------------------------
  // Deterministic, backend-free. detect() returns a seeded plate that exists in
  // the vehicle_master/driver fixtures; commit() echoes a filed incident. A
  // per-instance counter keeps case ids unique without Math.random.
  private violationSeq = 0;

  violationCatalog(): Promise<ViolationCatalogItem[]> {
    return Promise.resolve(VIOLATION_CATALOG.map((v) => ({ ...v })));
  }

  violationDetect(_image: Blob, gateId?: string): Promise<ViolationDetectResult> {
    const case_id = `case-mock-${++this.violationSeq}`;
    // Mock mode has no real ANPR service, so this is a SYNTHETIC fallback read
    // and is flagged as such. It NEVER substitutes a synthetic/mock vehicle:
    // vehicle + driver are null so the UI shows "Vehicle Not Found". Live mode
    // runs the real /api/anpr/infer pipeline.
    return Promise.resolve({
      case_id,
      plate: null,
      confidence: null,
      anpr_decision_path: "SYNTHETIC",
      anpr_real: false,
      bbox: null,
      degraded: true,
      vehicle: null,
      vehicle_class: null,
      driver: null,
      evidence_url: null,
      evidence_sha256: `sha256:mock-${this.violationSeq}`,
      gate_id: gateId ?? null,
      available_violations: VIOLATION_CATALOG.map((v) => ({ ...v })),
    });
  }

  violationCommit(input: ViolationCommitInput): Promise<ViolationIncident> {
    const chosen = VIOLATION_CATALOG.filter((v) => input.violations.includes(v.kind));
    const fine_total = chosen.reduce((a, v) => a + (v.fine_inr ?? 0), 0);
    const case_id = input.case_id ?? `case-mock-${++this.violationSeq}`;
    const issue = input.issue_challan !== false;
    return Promise.resolve({
      case_id,
      challan_id: issue ? `chl-${case_id}` : null,
      challan_no: issue ? `ECH-2026-${String(1000 + this.violationSeq).padStart(6, "0")}` : null,
      status: issue ? "CHALLAN_ISSUED" : "CONFIRMED",
      vehicle_number: input.plate ?? null,
      driver_id: input.driver_id ?? null,
      violations: chosen.map((v) => ({ ...v })),
      confidence: input.confidence ?? null,
      fine_total,
      total_fine: fine_total,
      evidence_url: input.evidence_url ?? null,
      evidence_sha256: input.evidence_sha256 ?? null,
      timestamp: new Date(NOW).toISOString(),
      gate_id: input.gate_id ?? null,
      alert_ids: chosen.map((_v, i) => `alt-${case_id}-${i}`),
      skipped: [],
    });
  }

  violationEnforce(
    _image: Blob,
    opts?: { gateId?: string; zoneId?: string; violations?: string },
  ): Promise<ViolationEnforceResult> {
    const case_id = `case-mock-${++this.violationSeq}`;
    // Deterministic auto-classification (mirrors the gateway's hash-derived pick).
    const seed = `enforce-${this.violationSeq}`;
    const primary = VIOLATION_CATALOG[fnv1a(seed) % VIOLATION_CATALOG.length];
    const maybe = fnv1a(`${seed}-2`) % 3 === 0
      ? VIOLATION_CATALOG[fnv1a(`${seed}-k`) % VIOLATION_CATALOG.length]
      : null;
    const chosen =
      maybe && maybe.kind !== primary.kind ? [primary, maybe] : [primary];
    const total = chosen.reduce((a, v) => a + (v.fine_inr ?? 0), 0);
    return Promise.resolve({
      case_id,
      plate: null,
      confidence: null,
      anpr_decision_path: "SYNTHETIC",
      anpr_real: false,
      bbox: null,
      degraded: true,
      // No synthetic/mock vehicle substitution — real enrichment only happens in
      // live mode from a real OCR plate.
      vehicle: null,
      vehicle_class: null,
      driver: null,
      violations: chosen.map((v) => ({ ...v })),
      total_fine: total,
      fine_total: total,
      challan_id: `chl-${case_id}`,
      challan_no: `ECH-2026-${String(1000 + this.violationSeq).padStart(6, "0")}`,
      status: "CHALLAN_ISSUED",
      evidence_url: null,
      evidence_sha256: `sha256:mock-${this.violationSeq}`,
      alert_ids: chosen.map((_v, i) => `alt-${case_id}-${i}`),
      skipped: [],
      notification_sent: true,
      gate_id: opts?.gateId ?? null,
    } as ViolationEnforceResult);
  }

  // ---- scenarios ---------------------------------------------------------
  scenarios(): Promise<Scenario[]> {
    const out: Scenario[] = [
      {
        id: "tfc1",
        name: "TFC-1 — Gate Closure",
        started_at: null,
        ended_at: null,
        params: { gate_id: "G-NSICT", duration_minutes: 120 },
      },
      {
        id: "tfc2",
        name: "TFC-2 — Wrong-Way Detection",
        started_at: null,
        ended_at: null,
        params: { camera_id: "C-KARAL-EXIT" },
      },
      {
        id: "tfc3",
        name: "TFC-3 — Cargo Surge Cross-Twin",
        started_at: null,
        ended_at: null,
        params: { dpd_release_spike: 2.5, window_min: 40 },
      },
    ];
    return Promise.resolve(out);
  }

  runScenario(
    name: string,
    _params: Record<string, any>,
  ): Promise<{ handle_id: string; name: string; status: string; trace_id?: string }> {
    const handle_id = `${name}-mock`;
    return Promise.resolve({ handle_id, name, status: "DONE", trace_id: `trace-${handle_id}` });
  }

  resetScenario(_name: string, _handleId?: string): Promise<{ ok: boolean }> {
    return Promise.resolve({ ok: true });
  }

  scenarioTimeline(handleId: string): Promise<{ handle_id: string; steps: ScenarioStep[] }> {
    return Promise.resolve({ handle_id: handleId, steps: timelineSteps(handleId) });
  }

  // ---- Appendix-C capabilities ------------------------------------------
  emptyAllocations(): Promise<EmptyAllocation[]> {
    const out: EmptyAllocation[] = [];
    for (let i = 0; i < 8; i++) {
      const seed = `empty-${i}`;
      const depot = DEPOTS[i % DEPOTS.length];
      const cargo = CARGO_TYPES[i % CARGO_TYPES.length];
      const distance = round(randRange(seed + "-dist", 6, 38), 1);
      // est TRT loosely tracks distance, on/around the 45-min target.
      const trt = round(28 + distance * randRange(seed + "-trt", 0.8, 1.3), 1);
      out.push({
        demand_id: `DEM-${(2200 + i).toString()}`,
        supply_depot: depot,
        container_type: CONTAINER_TYPES[i % CONTAINER_TYPES.length],
        cargo_type: cargo,
        distance_km: distance,
        est_trt_min: trt,
        confidence: round(randRange(seed + "-conf", 0.78, 0.97), 2),
      });
    }
    return Promise.resolve(out);
  }

  emptyTrtKpi(): Promise<KpiResult> {
    const spec = KPI_SPECS.find((s) => s.key === "trt_empty_ecd")!;
    return Promise.resolve(buildKpi(spec));
  }

  carbonRollup(): Promise<CarbonRollup> {
    const by_class: Record<string, number> = {
      HGV: 4820,
      REEFER: 1360,
      MGV: 980,
      LGV: 340,
    };
    const total_kg = Object.values(by_class).reduce((a, b) => a + b, 0);
    // Split total into moving vs idle so the two always sum back to total_kg.
    const moving = Math.round(total_kg * 0.62);
    const idle = total_kg - moving;
    return Promise.resolve({
      total_kg,
      vehicle_count: 218,
      by_class,
      by_source: { moving, idle },
    });
  }

  leoQueue(): Promise<AutoLeoResult[]> {
    return Promise.resolve(
      buildLeoQueue().map(({ _ts, ...row }) => {
        void _ts;
        return row;
      }),
    );
  }

  customsFlags(): Promise<Alert[]> {
    // Derived from the blocked leoQueue rows — same source of truth.
    const blocked = buildLeoQueue().filter((r) => !r.leo_ready);
    const out: Alert[] = blocked.map((r, i) => ({
      id: `cf-${i.toString().padStart(3, "0")}`,
      ts: r._ts,
      kind: "CUSTOMS_FLAG",
      severity: "warning",
      gate_id: r.gate_id ?? null,
      plate: r.vehicle_plate ?? null,
      payload: {
        container_no: r.container_no,
        customs_flags: r.customs_flags,
        checks: r.checks,
        lat: r.lat,
        lon: r.lon,
      },
      ack: false,
    }));
    return Promise.resolve(out);
  }

  identityGallery(): Promise<{ driver_id: string; name: string; license_no: string }[]> {
    return Promise.resolve(DRIVERS.map((d) => ({ ...d })));
  }

  identityVerify(
    driverId: string,
    arg?: "genuine" | "impostor" | "unknown" | IdentityVerifyArg,
  ): Promise<IdentityVerifyResult> {
    // A captured camera frame (image present) is treated as a genuine live match
    // in mock mode; otherwise honour the legacy simulate selector.
    const simulate =
      typeof arg === "string" ? arg : (arg?.simulate ?? (arg?.image ? "genuine" : "genuine"));
    const provider = typeof arg === "object" && arg?.image ? "onnx" : "synthetic";
    if (simulate === "genuine") {
      return Promise.resolve({
        driver_id: driverId,
        matched: true,
        score: 0.96,
        decision: "VERIFIED",
        reason: "Face match above threshold (0.85); Sarathi DL valid.",
        provider,
      });
    }
    if (simulate === "impostor") {
      return Promise.resolve({
        driver_id: driverId,
        matched: false,
        score: 0.1,
        decision: "REJECTED",
        reason: "Face match below threshold; gallery mismatch.",
        provider,
      });
    }
    // unknown -> provisional with a 24h cure window.
    const provisional_until = new Date(NOW + 24 * 3600 * 1000).toISOString();
    return Promise.resolve({
      driver_id: driverId,
      matched: false,
      score: 0.54,
      decision: "PROVISIONAL",
      provisional_until,
      cure_window_h: 24,
      reason: "No gallery enrolment; provisional entry granted pending KYC.",
      provider,
    });
  }

  identityEnrol(driverId: string, _image: string): Promise<IdentityEnrolResult> {
    // Mock enrolment — the real reference template is stored server-side; here we
    // just acknowledge so the camera flow works end-to-end in mock mode.
    return Promise.resolve({ enrolled: true, driver_id: driverId, provider: "onnx" });
  }

  // --- Driver enrolment approval workflow (mock store) ---
  enrollments(status?: string): Promise<DriverEnrollment[]> {
    const want = status?.toUpperCase();
    const out = MOCK_ENROLLMENTS.filter((e) => !want || e.status === want)
      // newest first, mirroring the gateway list ordering
      .slice()
      .sort((a, b) => (b.submitted_at ?? "").localeCompare(a.submitted_at ?? ""))
      .map((e) => ({ ...e, face_images: undefined }));
    return Promise.resolve(out);
  }

  enrollmentDetail(driverId: string): Promise<DriverEnrollment> {
    const rec = MOCK_ENROLLMENTS.find((e) => e.driver_id === driverId);
    if (!rec) return Promise.reject(new Error("enrolment not found"));
    return Promise.resolve({ ...rec });
  }

  approveEnrollment(driverId: string): Promise<{ approved: boolean }> {
    const rec = MOCK_ENROLLMENTS.find((e) => e.driver_id === driverId);
    if (rec) {
      rec.status = "ACTIVE";
      rec.reviewed_at = new Date(NOW).toISOString();
      rec.reviewed_by = "admin:mock";
      rec.template_dim = 128;
      rec.provider = "onnx";
      rec.photo_url = rec.photo ?? MOCK_FACE;
      rec.face_images = [];
    }
    return Promise.resolve({ approved: true });
  }

  rejectEnrollment(driverId: string, reason: string): Promise<{ rejected: boolean }> {
    const rec = MOCK_ENROLLMENTS.find((e) => e.driver_id === driverId);
    if (rec) {
      rec.status = "REJECTED";
      rec.rejection_reason = reason;
      rec.reviewed_at = new Date(NOW).toISOString();
      rec.reviewed_by = "admin:mock";
    }
    return Promise.resolve({ rejected: true });
  }

  reenrollEnrollment(driverId: string, reason?: string): Promise<{ reenroll: boolean }> {
    const rec = MOCK_ENROLLMENTS.find((e) => e.driver_id === driverId);
    if (rec) {
      rec.status = "REENROLL";
      rec.rejection_reason = reason ?? "re-enrolment requested";
      rec.reviewed_at = new Date(NOW).toISOString();
      rec.reviewed_by = "admin:mock";
    }
    return Promise.resolve({ reenroll: true });
  }

  parkingAvailability(minuteOfDay?: number): Promise<ParkingFacility[]> {
    return Promise.resolve(buildParking(minuteOfDay));
  }

  // Deterministic TAS slot book for the TAS widget. 12 slots at a 15-min cadence;
  // the first 5 are RESCHEDULED (what TFC-1 step 5 does) so the widget shows the
  // post-reschedule state in mock mode too.
  tasSlots(gateId = "G-NSICT"): Promise<TasSlot[]> {
    const baseMs = Date.UTC(2026, 5, 25, 9, 0, 0);
    const slots: TasSlot[] = Array.from({ length: 12 }, (_, i) => ({
      slot_id: `TAS-${gateId}-${String(i).padStart(2, "0")}`,
      gate_id: gateId,
      start: new Date(baseMs + i * 15 * 60_000).toISOString(),
      status: i < 5 ? "RESCHEDULED" : "BOOKED",
      rescheduled_to: i < 5 ? "G-JNPCT" : null,
    }));
    return Promise.resolve(slots);
  }

  parkingSummary(minuteOfDay?: number): Promise<ParkingSummary> {
    const facilities = buildParking(minuteOfDay);
    const total_capacity = facilities.reduce((a, f) => a + f.capacity, 0);
    const total_occupied = facilities.reduce((a, f) => a + f.occupied, 0);
    const total_available = facilities.reduce((a, f) => a + f.available, 0);
    const full_count = facilities.filter((f) => f.status === "FULL").length;
    return Promise.resolve({
      total_capacity,
      total_occupied,
      total_available,
      facilities: facilities.length,
      full_count,
    });
  }

  // ---- Fault-injection control surface ----------------------------------
  getFaults(): Promise<FaultState> {
    const domainState = (d: FaultDomain) => {
      const forced_rung = this.forcedRungs[d] ?? null;
      return {
        forced_rung,
        severity: forced_rung ? rungSeverity(d, forced_rung) : null,
      };
    };
    return Promise.resolve({
      domains: {
        camera: domainState("camera"),
        vahan: domainState("vahan"),
        trucks: domainState("trucks"),
      },
      rungs: {
        camera: [...FAULT_RUNGS.camera],
        vahan: [...FAULT_RUNGS.vahan],
        trucks: [...FAULT_RUNGS.trucks],
      },
    });
  }

  forceFault(domain: string, rung: string): Promise<FaultControlResult> {
    if (!FAULT_DOMAINS.includes(domain as FaultDomain)) {
      return Promise.reject(new Error(`404 unknown domain (${domain})`));
    }
    const d = domain as FaultDomain;
    if (!FAULT_RUNGS[d].includes(rung)) {
      return Promise.reject(new Error(`422 invalid rung (${rung})`));
    }
    this.forcedRungs[d] = rung;
    return Promise.resolve({
      forced: { [d]: rung },
      banner: bannerFrom(this.forcedRungs),
    });
  }

  clearFault(domain?: string): Promise<FaultControlResult> {
    if (domain == null) {
      this.forcedRungs = {};
      return Promise.resolve({ cleared: "all", banner: bannerFrom(this.forcedRungs) });
    }
    if (!FAULT_DOMAINS.includes(domain as FaultDomain)) {
      return Promise.reject(new Error(`404 unknown domain (${domain})`));
    }
    delete this.forcedRungs[domain as FaultDomain];
    return Promise.resolve({ cleared: domain, banner: bannerFrom(this.forcedRungs) });
  }

  // ---- Realism probes ---------------------------------------------------
  // Mirror the REAL committed artifacts (ai/anpr/eval/metrics.json,
  // ai/congestion/artifacts/metrics.json) so mock mode never shows a green
  // number the code cannot back. On a CPU-only PoC host ANPR runs the fallback
  // OCR (degraded, target NOT met) and congestion F1 is below 0.85. These honest
  // values drive the "DEGRADED MODEL" notice in the Demo Console.
  ocrEval(): Promise<OcrEval | null> {
    // combined_weighted_accuracy_pct: 10.63 → 0.1063; OCR_TARGET_MET: false.
    return Promise.resolve({
      clear_accuracy: 0.1063,
      target: 0.95,
      target_met: false,
      degraded: true,
    });
  }

  congestionMetrics(): Promise<CongestionMetrics | null> {
    // congestion_onset_f1: 0.8411 vs target_f1: 0.85 → below target.
    return Promise.resolve({ f1: 0.8411, target: 0.85, target_met: false });
  }
}
