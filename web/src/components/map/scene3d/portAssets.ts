// portAssets — static, display-only port-context 3D assets (terminal quay aprons,
// container-yard stacks, STS gantry cranes, berthed vessels) that make the UC3
// SceneView visually match the UC2 port digital twin.
//
// The quay-aligned placement frame + per-asset builders are ported directly from
// the UC2 reference (jnpa_poc_2 apps/web/src/map/scene3d.ts) so ships berth on the
// water face, cranes stand on the waterline and yards seat inland. The ONLY
// adaptation for UC3 is the anchor source: UC3 has no `terminals`/`pendency`
// feed, so each of the four gate markers (which ARE the four JNPA terminals)
// supplies a { id, lng, lat } anchor, and yard fill is a deterministic per-
// terminal fraction instead of a live pendency value. These functions return
// Graphic[] with inline 3D symbols so Scene3D consumes them through its existing
// GraphicsLayer render pattern (removeAll + add) — no new layer architecture.
//
// Reused models (already in web/public/models): ship-cargo-a/b.glb, sts-crane.glb,
// yard-container-{red,green,blue}.glb.
import Graphic from "@arcgis/core/Graphic";
import Point from "@arcgis/core/geometry/Point";
import Polygon from "@arcgis/core/geometry/Polygon";

const MODELS = "/models";
const WGS84 = { wkid: 4326 } as const;

/** A fixed terminal anchor (berth-line centroid), as UC2 defines them. */
export interface PortTerminal {
  id: string;
  lng: number;
  lat: number;
}

// UC2 reference terminal anchors — the EXACT berth-line centroids from the UC2
// config/terminals.json (EPSG:4326 [lng, lat]). The port scene is anchored to
// these FIXED terminals exactly as UC2 does — NOT to live/gate coordinates — so
// ships berth on the water face, cranes stand on the quay and yards seat inland
// identically to UC2, independent of runtime gate data. Includes GTI (UC3 has no
// gate for it, but UC2 renders it).
export const PORT_TERMINALS: PortTerminal[] = [
  { id: "NSICT", lng: 72.9505, lat: 18.9527 },
  { id: "NSIGT", lng: 72.9525, lat: 18.955 },
  { id: "GTI", lng: 72.9444, lat: 18.9457 },
  { id: "BMCT", lng: 72.9383, lat: 18.9386 },
  { id: "JNPCT", lng: 72.9479, lat: 18.9497 },
];

// Quay lengths (m) per terminal — the UC2 config/terminals.json values, so crane
// counts, yard spans and berth lengths reproduce the UC2 layout exactly.
const QUAY_LENGTH_M: Record<string, number> = {
  NSICT: 600,
  NSIGT: 330,
  GTI: 712,
  BMCT: 2000,
  JNPCT: 680,
};
const DEFAULT_QUAY_M = 700;
const quayLen = (id: string): number => QUAY_LENGTH_M[id] ?? DEFAULT_QUAY_M;

// ---- quay-aligned local frame (ported verbatim from UC2 scene3d.ts) --------
const LAT = 18.945;
const M_PER_DEG_LAT = 110_574;
const M_PER_DEG_LON = 111_320 * Math.cos((LAT * Math.PI) / 180);
const dLat = (m: number) => m / M_PER_DEG_LAT;
const dLon = (m: number) => m / M_PER_DEG_LON;

/** Real JNPA wharf bearing (deg): NNE→SSW, JNPCT(N) → BMCT(S). */
const QUAY_BEARING_DEG = 208;
const BRG = (QUAY_BEARING_DEG * Math.PI) / 180;
const alongE = Math.sin(BRG);
const alongN = Math.cos(BRG);
// SEAWARD = along-axis rotated +90° (≈298°, mostly west toward Thane Creek).
const SEAWARD_BRG = ((QUAY_BEARING_DEG + 90) % 360) * (Math.PI / 180);
const seaE = Math.sin(SEAWARD_BRG);
const seaN = Math.cos(SEAWARD_BRG);
// The gate/berth centroids mark the channel-side berth line, so bias the whole
// frame inland to seat the waterline, ships and cranes on the real wharf.
const LANDWARD_BIAS_M = 150;

/** Place a point relative to a terminal centroid in the quay frame.
 *  alongM  = metres down-quay (+south toward BMCT).
 *  offsetM = metres perpendicular: NEGATIVE seaward (water/ships/cranes),
 *            POSITIVE landward (yards/roads). */
function place(lng: number, lat: number, alongM: number, offsetM: number): [number, number] {
  const off = offsetM + LANDWARD_BIAS_M;
  const e = alongM * alongE - off * seaE;
  const n = alongM * alongN - off * seaN;
  return [lng + dLon(e), lat + dLat(n)];
}

// Model heading that aligns a model's long axis parallel to the quay.
const MODEL_ROTATION_DEG = 90;
const QUAY_HEADING = (QUAY_BEARING_DEG + MODEL_ROTATION_DEG) % 360;

/** Deterministic 0..1 from a key (no Math.random → stable replays). */
function rand01(key: string, salt = ""): number {
  let h = 2166136261;
  const s = key + "|" + salt;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 10000) / 10000;
}

// Rectangle in the quay frame (flat quay apron deck).
function quayRect(
  lng: number,
  lat: number,
  alongLenM: number,
  offsetDepthM: number,
  offsetCenterM: number,
): number[][] {
  const a = alongLenM / 2;
  const o0 = offsetCenterM - offsetDepthM / 2;
  const o1 = offsetCenterM + offsetDepthM / 2;
  return [
    place(lng, lat, -a, o0),
    place(lng, lat, a, o0),
    place(lng, lat, a, o1),
    place(lng, lat, -a, o1),
    place(lng, lat, -a, o0),
  ];
}

// UC2 committed asset placements — the hand-tuned final positions from the UC2
// data/positions.json (vessels, cranes, yard blocks). The pkey scheme and per-
// terminal counts match UC2 exactly, so applying these via withOverride() (UC2's
// own mechanism) reproduces UC2's berthed ships, quay-edge crane rows and in-yard
// container stacks precisely, instead of only the pre-drag derived layout.
// [lng, lat] verbatim from UC2 data/positions.json.
const PLACEMENTS: Record<string, [number, number]> = {
  "vessel:NSICT": [72.945013, 18.959685],
  "vessel:NSIGT": [72.940072, 18.949435],
  "vessel:GTI": [72.93816, 18.946855],
  "vessel:JNPCT": [72.948485, 18.961732],
  "vessel:BMCT": [72.933589, 18.939489],
  "crane:NSICT:0": [72.951439, 18.964655],
  "crane:NSICT:1": [72.949707, 18.961306],
  "crane:NSICT:2": [72.948577, 18.958953],
  "crane:NSIGT:0": [72.947728, 18.95705],
  "crane:NSIGT:1": [72.946831, 18.95523],
  "crane:NSIGT:2": [72.946099, 18.953536],
  "crane:GTI:0": [72.94326, 18.950323],
  "crane:GTI:1": [72.941561, 18.948395],
  "crane:GTI:2": [72.940863, 18.947575],
  "crane:GTI:3": [72.940181, 18.946844],
  "crane:BMCT:0": [72.938557, 18.945129],
  "crane:BMCT:1": [72.935065, 18.942779],
  "crane:BMCT:2": [72.938764, 18.941331],
  "crane:BMCT:3": [72.938083, 18.940588],
  "crane:BMCT:4": [72.934942, 18.937226],
  "crane:BMCT:5": [72.934335, 18.936578],
  "crane:BMCT:6": [72.933883, 18.936077],
  "crane:BMCT:7": [72.931841, 18.933824],
  "crane:BMCT:8": [72.931494, 18.933493],
  "crane:JNPCT:0": [72.930072, 18.931891],
  "crane:JNPCT:1": [72.929634, 18.931472],
  "crane:JNPCT:2": [72.929037, 18.930613],
  "yard:NSICT:0": [72.952651, 18.958562],
  "yard:NSICT:1": [72.952859, 18.958969],
  "yard:NSICT:2": [72.953067, 18.959375],
  "yard:NSICT:3": [72.953275, 18.959781],
  "yard:NSICT:4": [72.953026, 18.958388],
  "yard:NSICT:5": [72.953235, 18.958794],
  "yard:NSICT:6": [72.953443, 18.959201],
  "yard:NSICT:7": [72.953651, 18.959607],
  "yard:NSICT:8": [72.953402, 18.958213],
  "yard:NSICT:9": [72.95361, 18.95862],
  "yard:NSICT:10": [72.953818, 18.959026],
  "yard:NSICT:11": [72.954026, 18.959433],
  "yard:NSIGT:0": [72.949835, 18.95377],
  "yard:NSIGT:1": [72.950028, 18.954184],
  "yard:NSIGT:2": [72.950221, 18.954597],
  "yard:NSIGT:3": [72.950414, 18.95501],
  "yard:NSIGT:4": [72.950216, 18.953609],
  "yard:NSIGT:5": [72.95041, 18.954022],
  "yard:NSIGT:6": [72.950603, 18.954435],
  "yard:NSIGT:7": [72.950796, 18.954848],
  "yard:NSIGT:8": [72.950598, 18.953447],
  "yard:NSIGT:9": [72.950791, 18.95386],
  "yard:NSIGT:10": [72.950985, 18.954273],
  "yard:NSIGT:11": [72.951178, 18.954686],
  "yard:GTI:0": [72.946133, 18.944539],
  "yard:GTI:1": [72.946451, 18.944899],
  "yard:GTI:2": [72.946768, 18.945259],
  "yard:GTI:3": [72.947086, 18.945619],
  "yard:GTI:4": [72.946468, 18.944271],
  "yard:GTI:5": [72.946786, 18.944631],
  "yard:GTI:6": [72.947103, 18.944992],
  "yard:GTI:7": [72.94742, 18.945352],
  "yard:GTI:8": [72.946803, 18.944004],
  "yard:GTI:9": [72.94712, 18.944364],
  "yard:GTI:10": [72.947438, 18.944724],
  "yard:GTI:11": [72.947755, 18.945084],
  "yard:BMCT:0": [72.940248, 18.938169],
  "yard:BMCT:1": [72.940493, 18.938557],
  "yard:BMCT:2": [72.940737, 18.938944],
  "yard:BMCT:3": [72.940982, 18.939332],
  "yard:BMCT:4": [72.940606, 18.937964],
  "yard:BMCT:5": [72.940851, 18.938352],
  "yard:BMCT:6": [72.941096, 18.93874],
  "yard:BMCT:7": [72.94134, 18.939127],
  "yard:BMCT:8": [72.940965, 18.937759],
  "yard:BMCT:9": [72.941209, 18.938147],
  "yard:BMCT:10": [72.941454, 18.938535],
  "yard:BMCT:11": [72.941698, 18.938922],
  "yard:JNPCT:0": [72.930222, 18.929586],
  "yard:JNPCT:1": [72.930652, 18.930126],
  "yard:JNPCT:2": [72.931083, 18.930667],
  "yard:JNPCT:3": [72.931513, 18.931207],
  "yard:JNPCT:4": [72.930752, 18.929203],
  "yard:JNPCT:5": [72.931182, 18.929744],
  "yard:JNPCT:6": [72.931613, 18.930284],
  "yard:JNPCT:7": [72.932043, 18.930825],
  "yard:JNPCT:8": [72.931282, 18.928821],
  "yard:JNPCT:9": [72.931712, 18.929361],
  "yard:JNPCT:10": [72.932142, 18.929902],
  "yard:JNPCT:11": [72.932573, 18.930442],
};

/** UC2 withOverride: use the committed placement for this pkey if present,
 *  otherwise fall back to the derived quay-frame position. */
function withOverride(pkey: string, derived: [number, number]): [number, number] {
  const o = PLACEMENTS[pkey];
  return o ? [o[0], o[1]] : derived;
}

// ---- terminal quay aprons --------------------------------------------------
// A thin 40 m quay-edge strip just landward of the waterline — a subtle deck to
// seat the cranes on (the satellite basemap already shows the real concrete).
export function terminalDeckGraphics(terminals: PortTerminal[]): Graphic[] {
  return terminals.map(
    (t) =>
      new Graphic({
        geometry: new Polygon({
          rings: [quayRect(t.lng, t.lat, quayLen(t.id), 40, 20)],
          spatialReference: WGS84,
        }),
        symbol: {
          type: "polygon-3d",
          symbolLayers: [
            {
              type: "fill",
              material: { color: [120, 128, 138, 0.28] },
              outline: { color: [150, 158, 168, 0.5], size: 0.5 },
            },
          ],
        } as never,
        attributes: { assetId: `deck:${t.id}`, terminalId: t.id },
      }),
  );
}

// ---- container-yard stacks -------------------------------------------------
const YARD_ROWS = 3;
const YARD_COLS = 4;
const CONTAINER_H_M = 5.8;
const YARD_MODELS = ["red", "green", "blue"] as const;

export function yardStackGraphics(terminals: PortTerminal[]): Graphic[] {
  const out: Graphic[] = [];
  for (const t of terminals) {
    const quay = quayLen(t.id);
    // No live pendency feed in UC3 → deterministic per-terminal fill fraction so
    // each terminal stacks to a stable, varied height (display only).
    const frac = 0.35 + rand01(t.id, "fill") * 0.55; // 0.35..0.90
    for (let r = 0; r < YARD_ROWS; r++) {
      for (let c = 0; c < YARD_COLS; c++) {
        const alongM = (c - (YARD_COLS - 1) / 2) * (quay / YARD_COLS);
        const offsetM = 230 + r * 70; // landward rows
        const i = r * YARD_COLS + c;
        const [bx, by] = withOverride(`yard:${t.id}:${i}`, place(t.lng, t.lat, alongM, offsetM));
        const jitter = 0.5 + rand01(t.id, `blk${i}`) * 1.0;
        const f = Math.max(0.05, Math.min(1, frac * jitter));
        const tiers = 1 + Math.round(f * 5); // 1..6 tiers
        const fillPct = Math.round(f * 100);
        for (let k = 0; k < tiers; k++) {
          const model =
            k === tiers - 1 && fillPct >= 66 ? "red" : YARD_MODELS[(i + k) % YARD_MODELS.length]!;
          out.push(
            new Graphic({
              geometry: new Point({
                longitude: bx,
                latitude: by,
                z: k * CONTAINER_H_M,
                spatialReference: WGS84,
              }),
              symbol: {
                type: "point-3d",
                symbolLayers: [
                  {
                    type: "object",
                    resource: { href: `${MODELS}/yard-container-${model}.glb` },
                    height: CONTAINER_H_M,
                    anchor: "bottom",
                    heading: QUAY_HEADING,
                  },
                ],
              } as never,
              attributes: { assetId: `yard:${t.id}:${i}:${k}`, terminalId: t.id, fillPct },
            }),
          );
        }
      }
    }
  }
  return out;
}

// ---- STS gantry cranes -----------------------------------------------------
// Real STS crane GLB on the waterline; count scales with quay length; heading
// runs the rail along the quay so the boom cantilevers seaward.
export function craneGraphics(terminals: PortTerminal[]): Graphic[] {
  const out: Graphic[] = [];
  for (const t of terminals) {
    const quay = quayLen(t.id);
    const n = Math.max(3, Math.min(9, Math.round(quay / 200)));
    for (let i = 0; i < n; i++) {
      const alongM = ((i + 0.5) / n - 0.5) * quay;
      const [cx, cy] = withOverride(`crane:${t.id}:${i}`, place(t.lng, t.lat, alongM, 30));
      out.push(
        new Graphic({
          geometry: new Point({ longitude: cx, latitude: cy, spatialReference: WGS84 }),
          symbol: {
            type: "point-3d",
            symbolLayers: [
              {
                type: "object",
                resource: { href: `${MODELS}/sts-crane.glb` },
                height: 68,
                anchor: "bottom",
                heading: QUAY_HEADING,
              },
            ],
          } as never,
          attributes: { assetId: `crane:${t.id}:${i}`, terminalId: t.id },
        }),
      );
    }
  }
  return out;
}

// ---- berthed vessels -------------------------------------------------------
// Real container-ship GLB berthed on the WATER side of each terminal, hull
// parallel to the quay (seaward offset clears the landward bias).
export function vesselGraphics(terminals: PortTerminal[]): Graphic[] {
  return terminals.map((t, idx) => {
    const quay = quayLen(t.id);
    const alongShift = (rand01(t.id, "berth") - 0.5) * quay * 0.25;
    const [bx, by] = withOverride(`vessel:${t.id}`, place(t.lng, t.lat, alongShift, -230));
    const loa = Math.min(quay * 0.9, 330);
    const hull = idx % 2 === 0 ? "a" : "b";
    return new Graphic({
      geometry: new Point({ longitude: bx, latitude: by, spatialReference: WGS84 }),
      symbol: {
        type: "point-3d",
        symbolLayers: [
          {
            type: "object",
            resource: { href: `${MODELS}/ship-cargo-${hull}.glb` },
            height: 40,
            anchor: "bottom",
            heading: (QUAY_HEADING + 90) % 360,
          },
        ],
      } as never,
      attributes: { assetId: `vessel:${t.id}`, terminalId: t.id, loaM: Math.round(loa) },
    });
  });
}
