// roadSnap — client-side road-network alignment for the corridor + vehicles.
//
// The GIS data source (adapter / gateway) is intentionally left untouched: it
// still ships the authored straight-line corridor waypoints. This module is a
// pure RENDER-TIME enhancement that aligns those waypoints to the real road
// centerline and snaps vehicle points onto it.
//
// Centerline accuracy: we prefer OSRM's MAP-MATCHING service (/match) over plain
// routing (/route). Map matching is purpose-built to fit a noisy polyline to the
// road graph and — unlike /route — never inserts U-turn "loops" at the
// intermediate waypoints (the artifact that previously showed up inside the port
// area). If matching is unavailable we fall back to /route with U-turns
// suppressed, and finally to the unsnapped straight line so the map never breaks.

export type LngLat = [number, number]; // [lon, lat]

// OSRM public demo server — driving profile, no API key required.
const OSRM_BASE = "https://router.project-osrm.org";
const OSRM_TIMEOUT_MS = 6000;
// Per-point search radius (m) for map matching. Generous enough to catch
// waypoints offset from the centerline, tight enough to avoid parallel roads.
const MATCH_RADIUS_M = 50;

// Equirectangular longitude scale at the corridor's latitude (~18.86°N). Scaling
// lon by cos(lat) turns raw degrees into a locally-isotropic metric so nearest-
// point projection and slicing are distance-accurate (≈5% bias removed) — this
// is what centers the vehicle markers precisely on the line.
const CORRIDOR_LAT = 18.86;
const LON_SCALE = Math.cos((CORRIDOR_LAT * Math.PI) / 180);

/**
 * Align `points` ([lon,lat]) to the road centerline and return the polyline.
 * Tries map matching, then routing, then null (caller falls back to straight
 * segments). The fetch is abortable + time-boxed.
 */
export async function snapPathToRoads(
  points: LngLat[],
  signal?: AbortSignal,
): Promise<LngLat[] | null> {
  if (!Array.isArray(points) || points.length < 2) return null;
  const coords = points.map((p) => `${p[0]},${p[1]}`).join(";");

  // 1) Map matching — best centerline fit, no via-waypoint loops.
  const radiuses = points.map(() => MATCH_RADIUS_M).join(";");
  const matchUrl =
    `${OSRM_BASE}/match/v1/driving/${coords}` +
    `?geometries=geojson&overview=full&tidy=true&gaps=ignore&radiuses=${radiuses}`;
  const matched = await fetchGeometry(matchUrl, "matchings", signal);
  if (matched) return matched;

  // 2) Plain routing with U-turns suppressed at via points (continue_straight).
  const routeUrl =
    `${OSRM_BASE}/route/v1/driving/${coords}` +
    `?geometries=geojson&overview=full&continue_straight=true`;
  const routed = await fetchGeometry(routeUrl, "routes", signal);
  if (routed) return routed;

  // 3) No snapping available.
  return null;
}

/** Fetch an OSRM endpoint and concatenate the geometry of its result legs. */
async function fetchGeometry(
  url: string,
  key: "matchings" | "routes",
  signal?: AbortSignal,
): Promise<LngLat[] | null> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), OSRM_TIMEOUT_MS);
  const onAbort = () => ctrl.abort();
  signal?.addEventListener("abort", onAbort);
  try {
    const res = await fetch(url, { signal: ctrl.signal });
    if (!res.ok) return null;
    const json = (await res.json()) as {
      code?: string;
      matchings?: { geometry?: { coordinates?: number[][] } }[];
      routes?: { geometry?: { coordinates?: number[][] } }[];
    };
    if (json.code && json.code !== "Ok") return null;
    const legs = json[key];
    if (!Array.isArray(legs) || legs.length === 0) return null;
    // Concatenate legs in order, dropping a leg's first point when it duplicates
    // the previous leg's last point (matchings can be split into ordered traces).
    const out: LngLat[] = [];
    for (const leg of legs) {
      const coords = leg.geometry?.coordinates;
      if (!Array.isArray(coords)) continue;
      for (const c of coords) {
        const p: LngLat = [c[0], c[1]];
        const last = out[out.length - 1];
        if (last && last[0] === p[0] && last[1] === p[1]) continue;
        out.push(p);
      }
    }
    return out.length >= 2 ? out : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onAbort);
  }
}

// ---- metric geometry helpers -------------------------------------------------
// All math runs in a local equirectangular metric (x = lon·cos(lat), y = lat) so
// distances are isotropic; inputs/outputs stay in raw [lon,lat].

function mdist2(a: LngLat, b: LngLat): number {
  const dx = (a[0] - b[0]) * LON_SCALE;
  const dy = a[1] - b[1];
  return dx * dx + dy * dy;
}

function mlen(a: LngLat, b: LngLat): number {
  return Math.sqrt(mdist2(a, b));
}

/** Closest point on segment a→b to p, with the interpolation factor t∈[0,1]. */
function projectToSegment(a: LngLat, b: LngLat, p: LngLat): { point: LngLat; t: number } {
  const dx = (b[0] - a[0]) * LON_SCALE;
  const dy = b[1] - a[1];
  const len2 = dx * dx + dy * dy;
  if (len2 === 0) return { point: a, t: 0 };
  const px = (p[0] - a[0]) * LON_SCALE;
  const py = p[1] - a[1];
  let t = (px * dx + py * dy) / len2;
  t = Math.max(0, Math.min(1, t));
  return { point: [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t], t };
}

// ---------------------------------------------------------------------------
// Corridor road reference — the drawn line, and ONLY the drawn line.
//
// UC2 renders its roads from stored road-true polylines (`truckroute:*` paths
// in its placement store) and every renderer slices that stored geometry. UC3
// has no such store: it renders the corridor straight from the API waypoints,
// which are an authored diagonal drifting up to 15 km off NH-348. Snapping
// them at request time does not help — OSRM refuses /match for this trace
// (radius/'too many coordinates') and the /route fallback detours to touch
// every off-road waypoint, returning 68 km of road for a 23.5 km corridor.
// That detour is what drew routes through forest and open ground.
//
// So this is UC2's pattern: the road-true centreline, stored once. It is the
// MAIN TRAFFIC ROAD ONLY — JNPA Gate-1 south-east to the corridor's far end at
// 73.080, 18.780 — 28.8 km of carriageway, every sampled vertex verified within
// 1 m of a mapped road (OSRM /nearest).
//
// It deliberately does NOT route through the gates, and that is the UC2
// architecture, not a shortcut. UC2's traffic overlay is built in roadSegments()
// purely from `truckroute:*` paths; that function contains no gate reference at
// all. Gates are an independent layer resolved from `gate3d:*` placements, and
// UC2 associates them with traffic by DISTANCE — "a share of its queue length is
// added to nearby segments with a linear falloff over TRAFFIC_GATE_REACH_M".
// That reach radius only exists because gates sit BESIDE the roads.
//
// An earlier revision here threaded the reference through all four gate
// checkpoints. Because the gates are terminal entrances on dead-end spur roads,
// that forced the route to drive in and back out of each one: 3.37 km of the
// 43.8 km (8%) retraced itself, twice on the opposite carriageway 9-25 m away,
// which rendered as duplicated and parallel orange ribbons. Re-ordering the via
// points made it worse (up to 102 overlapping edges), so the loops were inherent
// to threading, not to the ordering. This reference has ZERO self-overlap.
//
// One property still matters: LENGTH. projectOnPath below maps an authored
// point's FRACTION along the corridor onto this line, so a reference shorter
// than the corridor squeezes the whole thing into it — an 8.35 km reference once
// drew all 13 segments compressed into a third of their extent, reading as a
// single short line.
//
// Rendering only: no API value, segment id, jam factor, threshold, gate position
// or backend behaviour is derived from or altered by it. Gate placement is
// entirely separate (gateAccessPosition in scene3d/portAssets.ts) and untouched.
export const CORRIDOR_ROAD: LngLat[] = [
  [72.94936, 18.94922], [72.95032, 18.94879], [72.95075, 18.94933], [72.95123, 18.94995],
  [72.95207, 18.95102], [72.95284, 18.95081], [72.95359, 18.95049], [72.95431, 18.95018],
  [72.95525, 18.94978], [72.95659, 18.94922], [72.95711, 18.94988], [72.95771, 18.95070],
  [72.95848, 18.95167], [72.95869, 18.95190], [72.95887, 18.95205], [72.95923, 18.95234],
  [72.95949, 18.95247], [72.96020, 18.95247], [72.96049, 18.95247], [72.96075, 18.95248],
  [72.96202, 18.95248], [72.96228, 18.95248], [72.96274, 18.95241], [72.96298, 18.95234],
  [72.96363, 18.95206], [72.96425, 18.95175], [72.96486, 18.95139], [72.96541, 18.95101],
  [72.96575, 18.95073], [72.96593, 18.95056], [72.96624, 18.95016], [72.96639, 18.94977],
  [72.96648, 18.94934], [72.96657, 18.94891], [72.96662, 18.94846], [72.96660, 18.94800],
  [72.96635, 18.94581], [72.96639, 18.94548], [72.96517, 18.93233], [72.96527, 18.93161],
  [72.96543, 18.93096], [72.96574, 18.93023], [72.96619, 18.92955], [72.96671, 18.92895],
  [72.97109, 18.92451], [72.97175, 18.92395], [72.97194, 18.92379], [72.97342, 18.92228],
  [72.97539, 18.92031], [72.97619, 18.91945], [72.97630, 18.91918], [72.97662, 18.91888],
  [72.97764, 18.91785], [72.97813, 18.91737], [72.98056, 18.91493], [72.98225, 18.91325],
  [72.98269, 18.91280], [72.98292, 18.91255], [72.98331, 18.91212], [72.98374, 18.91169],
  [72.98405, 18.91136], [72.98440, 18.91094], [72.98461, 18.91068], [72.98479, 18.91046],
  [72.98583, 18.90885], [72.98871, 18.90420], [72.98907, 18.90357], [72.98922, 18.90336],
  [72.98971, 18.90283], [72.98990, 18.90264], [72.99011, 18.90251], [72.99093, 18.90190],
  [72.99127, 18.90165], [72.99698, 18.89762], [72.99751, 18.89726], [72.99821, 18.89677],
  [72.99884, 18.89650], [73.00047, 18.89537], [73.00079, 18.89513], [73.00104, 18.89498],
  [73.00130, 18.89486], [73.00156, 18.89481], [73.00186, 18.89483], [73.00213, 18.89492],
  [73.00253, 18.89454], [73.00223, 18.89374], [73.00048, 18.88976], [73.00035, 18.88941],
  [73.00027, 18.88905], [73.00019, 18.88843], [73.00013, 18.88763], [72.99998, 18.88579],
  [72.99993, 18.88517], [72.99974, 18.88268], [72.99956, 18.88177], [72.99921, 18.88083],
  [72.99871, 18.88002], [72.99809, 18.87931], [72.99504, 18.87680], [72.99548, 18.87627],
  [72.99575, 18.87609], [72.99617, 18.87594], [72.99661, 18.87578], [72.99700, 18.87557],
  [72.99896, 18.87413], [72.99980, 18.87338], [73.00084, 18.87245], [73.00247, 18.87118],
  [73.00272, 18.87098], [73.00293, 18.87081], [73.00319, 18.87054], [73.00380, 18.86968],
  [73.00427, 18.86904], [73.00496, 18.86797], [73.00560, 18.86700], [73.00577, 18.86674],
  [73.00634, 18.86587], [73.00697, 18.86494], [73.00717, 18.86465], [73.00732, 18.86426],
  [73.00763, 18.86324], [73.00787, 18.86266], [73.00834, 18.86234], [73.00895, 18.86196],
  [73.00920, 18.86184], [73.01319, 18.86067], [73.01374, 18.86055], [73.01512, 18.86044],
  [73.01506, 18.85990], [73.01493, 18.85912], [73.01500, 18.85882], [73.01514, 18.85861],
  [73.01540, 18.85822], [73.01600, 18.85734], [73.01612, 18.85708], [73.01624, 18.85679],
  [73.01619, 18.85652], [73.01613, 18.85623], [73.01595, 18.85553], [73.01588, 18.85530],
  [73.01565, 18.85500], [73.01526, 18.85486], [73.01498, 18.85477], [73.01469, 18.85460],
  [73.01449, 18.85425], [73.01431, 18.85395], [73.01360, 18.85225], [73.01341, 18.85191],
  [73.01246, 18.85025], [73.01229, 18.84987], [73.01209, 18.84932], [73.01177, 18.84889],
  [73.01168, 18.84849], [73.01172, 18.84785], [73.01190, 18.84753], [73.01212, 18.84741],
  [73.01237, 18.84733], [73.01263, 18.84718], [73.01274, 18.84643], [73.01277, 18.84533],
  [73.01287, 18.84489], [73.01300, 18.84456], [73.01338, 18.84412], [73.01362, 18.84381],
  [73.01385, 18.84360], [73.01418, 18.84332], [73.01435, 18.84246], [73.01454, 18.84095],
  [73.01458, 18.84064], [73.01480, 18.83894], [73.01527, 18.83706], [73.01535, 18.83679],
  [73.01543, 18.83649], [73.01554, 18.83619], [73.01553, 18.83584], [73.01555, 18.83549],
  [73.01586, 18.83468], [73.01601, 18.83409], [73.01607, 18.83383], [73.01606, 18.83355],
  [73.01606, 18.83305], [73.01599, 18.83194], [73.01604, 18.83166], [73.01647, 18.83149],
  [73.01694, 18.83139], [73.01724, 18.83134], [73.01752, 18.83123], [73.01778, 18.83100],
  [73.01830, 18.83039], [73.01886, 18.82980], [73.01973, 18.82900], [73.02115, 18.82788],
  [73.01991, 18.82568], [73.01980, 18.82542], [73.01997, 18.82435], [73.02072, 18.82189],
  [73.02088, 18.82153], [73.02184, 18.82029], [73.02403, 18.81798], [73.02493, 18.81726],
  [73.02505, 18.81699], [73.02500, 18.81633], [73.02492, 18.81429], [73.02494, 18.81362],
  [73.02497, 18.81339], [73.02533, 18.81316], [73.02604, 18.81301], [73.02722, 18.81283],
  [73.02834, 18.81280], [73.02892, 18.81273], [73.03123, 18.81174], [73.03216, 18.81136],
  [73.03238, 18.81106], [73.03467, 18.80624], [73.03488, 18.80582], [73.03510, 18.80549],
  [73.03548, 18.80518], [73.03579, 18.80502], [73.03635, 18.80474], [73.03700, 18.80451],
  [73.03782, 18.80422], [73.04514, 18.80117], [73.04610, 18.80084], [73.04766, 18.80024],
  [73.05029, 18.79921], [73.05142, 18.79856], [73.05262, 18.79794], [73.05363, 18.79748],
  [73.05401, 18.79739], [73.05424, 18.79723], [73.05402, 18.79578], [73.05399, 18.79553],
  [73.05401, 18.79524], [73.05433, 18.79507], [73.05470, 18.79494], [73.05496, 18.79470],
  [73.05524, 18.79420], [73.05559, 18.79301], [73.05597, 18.79245], [73.05619, 18.79222],
  [73.05700, 18.79195], [73.05771, 18.79164], [73.05793, 18.79144], [73.05807, 18.79113],
  [73.05847, 18.79076], [73.05894, 18.79059], [73.05944, 18.79052], [73.05965, 18.79035],
  [73.05969, 18.79009], [73.06012, 18.78974], [73.06048, 18.78937], [73.06087, 18.78927],
  [73.06180, 18.78911], [73.06270, 18.78884], [73.06467, 18.78886], [73.06494, 18.78882],
  [73.06518, 18.78868], [73.06541, 18.78856], [73.06566, 18.78853], [73.06611, 18.78851],
  [73.06639, 18.78838], [73.06687, 18.78835], [73.06736, 18.78829], [73.06775, 18.78806],
  [73.06790, 18.78788], [73.06805, 18.78770], [73.06867, 18.78741], [73.06954, 18.78709],
  [73.06975, 18.78687], [73.06997, 18.78662], [73.07017, 18.78646], [73.07044, 18.78640],
  [73.07384, 18.78509], [73.07425, 18.78482], [73.07456, 18.78451], [73.07505, 18.78270],
  [73.07554, 18.78172], [73.07585, 18.78111], [73.07603, 18.78096], [73.07630, 18.78096],
  [73.07704, 18.78112], [73.07733, 18.78090], [73.07752, 18.78052], [73.07776, 18.78037],
  [73.07811, 18.78002], [73.07840, 18.77974], [73.07871, 18.77962], [73.07912, 18.77965],
  [73.07963, 18.78007], [73.07993, 18.78002], [73.08000, 18.78002],
];

/**
 * A reusable index over a polyline: the cumulative metric length at each vertex.
 * Build once per snapped route, then reuse across every project/slice call.
 */
export interface PathIndex {
  path: LngLat[];
  cum: number[];
  total: number;
  /** Optional authored polyline this index re-parameterises (see below). */
  authored?: { path: LngLat[]; cum: number[]; total: number };
}

export function buildPathIndex(path: LngLat[], authored?: LngLat[]): PathIndex {
  const cum = [0];
  for (let i = 1; i < path.length; i++) {
    cum[i] = cum[i - 1] + mlen(path[i - 1], path[i]);
  }
  const idx: PathIndex = { path, cum, total: cum[cum.length - 1] ?? 0 };
  if (authored && authored.length > 1) {
    const acum = [0];
    for (let i = 1; i < authored.length; i++) {
      acum[i] = acum[i - 1] + mlen(authored[i - 1], authored[i]);
    }
    idx.authored = { path: authored, cum: acum, total: acum[acum.length - 1] ?? 0 };
  }
  return idx;
}

/** Distance of `p` along a bare polyline (no re-parameterisation). */
function alongRaw(
  path: LngLat[],
  cum: number[],
  p: LngLat,
): { point: LngLat; along: number } {
  let best = { d2: Infinity, point: path[0] ?? p, along: 0 };
  for (let i = 0; i < path.length - 1; i++) {
    const { point, t } = projectToSegment(path[i], path[i + 1], p);
    const d2 = mdist2(point, p);
    if (d2 < best.d2) {
      const segLen = cum[i + 1] - cum[i];
      best = { d2, point, along: cum[i] + segLen * t };
    }
  }
  return { point: best.point, along: best.along };
}

/**
 * Snap `p` onto the polyline; returns the on-road point + its distance along it.
 *
 * When the index carries an `authored` polyline, `p` is first located along THAT
 * line and the resulting fraction is replayed on the road — a monotonic,
 * order-preserving re-parameterisation. That is what lets an authored waypoint
 * sitting 15 km off NH-348 still render at the right RELATIVE position on the
 * real road, instead of dragging the drawn line out to meet it.
 */
export function projectOnPath(idx: PathIndex, p: LngLat): { point: LngLat; along: number } {
  if (idx.authored && idx.authored.total > 0) {
    const a = alongRaw(idx.authored.path, idx.authored.cum, p);
    const along = (a.along / idx.authored.total) * idx.total;
    return { point: pointAtAlong(idx, along), along };
  }
  return alongRaw(idx.path, idx.cum, p);
}

/** Interpolate the polyline point at cumulative distance `d`. */
function pointAtAlong(idx: PathIndex, d: number): LngLat {
  const { path, cum, total } = idx;
  if (d <= 0) return path[0];
  if (d >= total) return path[path.length - 1];
  let i = 1;
  while (i < cum.length && cum[i] < d) i++;
  const segLen = cum[i] - cum[i - 1];
  const t = segLen === 0 ? 0 : (d - cum[i - 1]) / segLen;
  return [
    path[i - 1][0] + (path[i][0] - path[i - 1][0]) * t,
    path[i - 1][1] + (path[i][1] - path[i - 1][1]) * t,
  ];
}

/**
 * Extract the sub-polyline of the road between the two given points (each first
 * projected onto the road). Used to colour the route per corridor segment while
 * keeping every vertex of the real road geometry in between.
 */
export function sliceBetween(idx: PathIndex, from: LngLat, to: LngLat): LngLat[] {
  let a = projectOnPath(idx, from).along;
  let b = projectOnPath(idx, to).along;
  if (a > b) [a, b] = [b, a];
  const out: LngLat[] = [pointAtAlong(idx, a)];
  for (let i = 0; i < idx.path.length; i++) {
    if (idx.cum[i] > a && idx.cum[i] < b) out.push(idx.path[i]);
  }
  out.push(pointAtAlong(idx, b));
  return out;
}

/**
 * The index the corridor map draws with: the stored road centreline, indexed
 * against the authored corridor the API returns.
 *
 * Synchronous and dependency-free — no OSRM round trip, so the drawn route is
 * deterministic and cannot degrade to the straight authored line when a public
 * routing server is slow, blocked or rate-limited. `authored` is the API's own
 * `corridor.polyline`, used ONLY to position API geometry along the road.
 */
export function buildCorridorRoadIndex(authored?: LngLat[]): PathIndex {
  return buildPathIndex(CORRIDOR_ROAD, authored);
}
