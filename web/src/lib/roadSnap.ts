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
// So this is UC2's pattern: the road-true centreline, stored once. It threads
// the port road network THROUGH all four gate checkpoints and then runs the
// corridor out to its far end — JNPA Gate-1 -> G-NSICT -> G-NSIGT -> G-JNPCT ->
// G-BMCT -> 73.080, 18.780 — over JNPT, JNPT Terminal 1, JNPT Service Road,
// JNPT Terminal 3, Panvel Uran Road and Uran Koproli Road. 43.9 km of
// carriageway; every sampled vertex verified within 1 m of a mapped road, and
// every gate within 4-60 m of the line (OSRM /nearest).
//
// TWO properties of this reference matter, and both were learned the hard way:
//
//  * LENGTH. projectOnPath below maps an authored point's FRACTION along the
//    corridor onto this line, so a reference shorter than the corridor squeezes
//    the whole thing into it. An 8.35 km reference still drew all 13 segments —
//    just compressed into a third of their extent, which reads as one short line.
//
//  * GATE COVERAGE. A reference that merely runs down the corridor passes only
//    the two northern gates (G-JNPCT and G-BMCT were 1226 m and 1451 m off it),
//    so at the zoom level that shows the gate cluster almost none of the traffic
//    ribbon is on screen. Routing THROUGH the gates is what puts the green/amber
//    line on the roads the operator is actually looking at.
//
// Rendering only: no API value, segment id, jam factor, threshold, gate position
// or backend behaviour is derived from or altered by it.
//
// prettier-ignore — this is a coordinate TABLE, not code. Prettier reflows it to
// one [lon, lat] pair per line, turning ~110 readable rows into ~440 and making
// every future diff on the polyline unreadable. The 4-per-line grouping is the
// point: it keeps the corridor scannable. Formatting is suppressed deliberately.
// prettier-ignore
export const CORRIDOR_ROAD: LngLat[] = [
  [72.94936, 18.94922], [72.95032, 18.94879], [72.95075, 18.94933], [72.95123, 18.94995],
  [72.95207, 18.95102], [72.95284, 18.95081], [72.95359, 18.95049], [72.95431, 18.95018],
  [72.95525, 18.94978], [72.95659, 18.94922], [72.95711, 18.94988], [72.95771, 18.95070],
  [72.95848, 18.95167], [72.95869, 18.95190], [72.95887, 18.95205], [72.95923, 18.95234],
  [72.95793, 18.95292], [72.95973, 18.95311], [72.96020, 18.95299], [72.96045, 18.95299],
  [72.96078, 18.95299], [72.96152, 18.95299], [72.96180, 18.95297], [72.96204, 18.95293],
  [72.96250, 18.95281], [72.96294, 18.95262], [72.96374, 18.95223], [72.96437, 18.95194],
  [72.96479, 18.95169], [72.96499, 18.95155], [72.96569, 18.95095], [72.96600, 18.95059],
  [72.96624, 18.95016], [72.96639, 18.94977], [72.96648, 18.94934], [72.96657, 18.94891],
  [72.96662, 18.94846], [72.96660, 18.94800], [72.96635, 18.94581], [72.96639, 18.94548],
  [72.96517, 18.93233], [72.96527, 18.93161], [72.96543, 18.93096], [72.96574, 18.93023],
  [72.96619, 18.92955], [72.96671, 18.92895], [72.97109, 18.92451], [72.97175, 18.92395],
  [72.97194, 18.92379], [72.97173, 18.92361], [72.97130, 18.92404], [72.97096, 18.92439],
  [72.97037, 18.92499], [72.97012, 18.92524], [72.96864, 18.92674], [72.96818, 18.92708],
  [72.96650, 18.92877], [72.96595, 18.92941], [72.96552, 18.93008], [72.96516, 18.93090],
  [72.96499, 18.93154], [72.96491, 18.93231], [72.96600, 18.94496], [72.96615, 18.94550],
  [72.96612, 18.94582], [72.96630, 18.94802], [72.96630, 18.94844], [72.96626, 18.94886],
  [72.96617, 18.94925], [72.96605, 18.94961], [72.96589, 18.94993], [72.96557, 18.95040],
  [72.96538, 18.95077], [72.96517, 18.95101], [72.96479, 18.95130], [72.96419, 18.95165],
  [72.96355, 18.95196], [72.96316, 18.95206], [72.96240, 18.95218], [72.96021, 18.95218],
  [72.95948, 18.95215], [72.95910, 18.95208], [72.95866, 18.95173], [72.95827, 18.95126],
  [72.95789, 18.95076], [72.95767, 18.95047], [72.95718, 18.94984], [72.95654, 18.94900],
  [72.95572, 18.94798], [72.95484, 18.94682], [72.95419, 18.94590], [72.95399, 18.94571],
  [72.95326, 18.94476], [72.95309, 18.94451], [72.94940, 18.93977], [72.94941, 18.93950],
  [72.94878, 18.93865], [72.94847, 18.93859], [72.94820, 18.93834], [72.94822, 18.93803],
  [72.94846, 18.93763], [72.94834, 18.93656], [72.95056, 18.93395], [72.95103, 18.93367],
  [72.95337, 18.93095], [72.95422, 18.92994], [72.95103, 18.93367], [72.95056, 18.93395],
  [72.95445, 18.92941], [72.95410, 18.92919], [72.95388, 18.92898], [72.95370, 18.92881],
  [72.95434, 18.92818], [72.95515, 18.92738], [72.95495, 18.92719], [72.95459, 18.92687],
  [72.95515, 18.92645], [72.95500, 18.92626], [72.95437, 18.92662], [72.95381, 18.92700],
  [72.95329, 18.92743], [72.95210, 18.92860], [72.95015, 18.93052], [72.94830, 18.93296],
  [72.94400, 18.93723], [72.94373, 18.93779], [72.94312, 18.93838], [72.94267, 18.93884],
  [72.94229, 18.93923], [72.94190, 18.93962], [72.94164, 18.93988], [72.94385, 18.93790],
  [72.94414, 18.93735], [72.94734, 18.93414], [72.94857, 18.93291], [72.95367, 18.92771],
  [72.95412, 18.92727], [72.95459, 18.92687], [72.95515, 18.92645], [72.95695, 18.92532],
  [72.95891, 18.92403], [72.96045, 18.92294], [72.96155, 18.92221], [72.96380, 18.92066],
  [72.96426, 18.92037], [72.97268, 18.91541], [72.97428, 18.91450], [72.97497, 18.91420],
  [72.97606, 18.91376], [72.97667, 18.91347], [72.97746, 18.91302], [72.98004, 18.91147],
  [72.98027, 18.91144], [72.98061, 18.91131], [72.98086, 18.91121], [72.98143, 18.91096],
  [72.98177, 18.91076], [72.98201, 18.91059], [72.98250, 18.91021], [72.98374, 18.90926],
  [72.98615, 18.90742], [72.98647, 18.90712], [72.98668, 18.90683], [72.98752, 18.90518],
  [72.98758, 18.90494], [72.98761, 18.90466], [72.98762, 18.90439], [72.98756, 18.90405],
  [72.98707, 18.90249], [72.98693, 18.90225], [72.98667, 18.90202], [72.98643, 18.90178],
  [72.98629, 18.90159], [72.98593, 18.90096], [72.98474, 18.89996], [72.98395, 18.89945],
  [72.98198, 18.89800], [72.98067, 18.89705], [72.97897, 18.89584], [72.97783, 18.89493],
  [72.97859, 18.89404], [72.98124, 18.89094], [72.98152, 18.89064], [72.98184, 18.89038],
  [72.98229, 18.89020], [72.98276, 18.89014], [72.98322, 18.89014], [72.98353, 18.89017],
  [72.98376, 18.89027], [72.98398, 18.89015], [72.98422, 18.88962], [72.98446, 18.88926],
  [72.98543, 18.88811], [72.98612, 18.88729], [72.98814, 18.88490], [72.98911, 18.88375],
  [72.98934, 18.88348], [72.98985, 18.88288], [72.99145, 18.88098], [72.99370, 18.87832],
  [72.99452, 18.87735], [72.99490, 18.87690], [72.99548, 18.87627], [72.99575, 18.87609],
  [72.99617, 18.87594], [72.99661, 18.87578], [72.99700, 18.87557], [72.99896, 18.87413],
  [72.99980, 18.87338], [73.00084, 18.87245], [73.00247, 18.87118], [73.00272, 18.87098],
  [73.00293, 18.87081], [73.00319, 18.87054], [73.00380, 18.86968], [73.00427, 18.86904],
  [73.00496, 18.86797], [73.00560, 18.86700], [73.00577, 18.86674], [73.00634, 18.86587],
  [73.00697, 18.86494], [73.00717, 18.86465], [73.00732, 18.86426], [73.00763, 18.86324],
  [73.00787, 18.86266], [73.00834, 18.86234], [73.00895, 18.86196], [73.00920, 18.86184],
  [73.01319, 18.86067], [73.01374, 18.86055], [73.01512, 18.86044], [73.01506, 18.85990],
  [73.01493, 18.85912], [73.01500, 18.85882], [73.01514, 18.85861], [73.01540, 18.85822],
  [73.01600, 18.85734], [73.01612, 18.85708], [73.01624, 18.85679], [73.01619, 18.85652],
  [73.01613, 18.85623], [73.01595, 18.85553], [73.01588, 18.85530], [73.01565, 18.85500],
  [73.01526, 18.85486], [73.01498, 18.85477], [73.01469, 18.85460], [73.01449, 18.85425],
  [73.01431, 18.85395], [73.01360, 18.85225], [73.01341, 18.85191], [73.01246, 18.85025],
  [73.01229, 18.84987], [73.01209, 18.84932], [73.01177, 18.84889], [73.01168, 18.84849],
  [73.01172, 18.84785], [73.01190, 18.84753], [73.01212, 18.84741], [73.01237, 18.84733],
  [73.01263, 18.84718], [73.01274, 18.84643], [73.01277, 18.84533], [73.01287, 18.84489],
  [73.01300, 18.84456], [73.01338, 18.84412], [73.01362, 18.84381], [73.01385, 18.84360],
  [73.01418, 18.84332], [73.01435, 18.84246], [73.01454, 18.84095], [73.01458, 18.84064],
  [73.01480, 18.83894], [73.01527, 18.83706], [73.01535, 18.83679], [73.01543, 18.83649],
  [73.01554, 18.83619], [73.01553, 18.83584], [73.01555, 18.83549], [73.01586, 18.83468],
  [73.01601, 18.83409], [73.01607, 18.83383], [73.01606, 18.83355], [73.01606, 18.83305],
  [73.01599, 18.83194], [73.01604, 18.83166], [73.01647, 18.83149], [73.01694, 18.83139],
  [73.01724, 18.83134], [73.01752, 18.83123], [73.01778, 18.83100], [73.01830, 18.83039],
  [73.01886, 18.82980], [73.01973, 18.82900], [73.02115, 18.82788], [73.01991, 18.82568],
  [73.01980, 18.82542], [73.01997, 18.82435], [73.02072, 18.82189], [73.02088, 18.82153],
  [73.02184, 18.82029], [73.02403, 18.81798], [73.02493, 18.81726], [73.02505, 18.81699],
  [73.02500, 18.81633], [73.02492, 18.81429], [73.02494, 18.81362], [73.02497, 18.81339],
  [73.02533, 18.81316], [73.02604, 18.81301], [73.02722, 18.81283], [73.02834, 18.81280],
  [73.02892, 18.81273], [73.03123, 18.81174], [73.03216, 18.81136], [73.03238, 18.81106],
  [73.03467, 18.80624], [73.03488, 18.80582], [73.03510, 18.80549], [73.03548, 18.80518],
  [73.03579, 18.80502], [73.03635, 18.80474], [73.03700, 18.80451], [73.03782, 18.80422],
  [73.04514, 18.80117], [73.04610, 18.80084], [73.04766, 18.80024], [73.05029, 18.79921],
  [73.05142, 18.79856], [73.05262, 18.79794], [73.05363, 18.79748], [73.05401, 18.79739],
  [73.05424, 18.79723], [73.05402, 18.79578], [73.05399, 18.79553], [73.05401, 18.79524],
  [73.05433, 18.79507], [73.05470, 18.79494], [73.05496, 18.79470], [73.05524, 18.79420],
  [73.05559, 18.79301], [73.05597, 18.79245], [73.05619, 18.79222], [73.05700, 18.79195],
  [73.05771, 18.79164], [73.05793, 18.79144], [73.05807, 18.79113], [73.05847, 18.79076],
  [73.05894, 18.79059], [73.05944, 18.79052], [73.05965, 18.79035], [73.05969, 18.79009],
  [73.06012, 18.78974], [73.06048, 18.78937], [73.06087, 18.78927], [73.06180, 18.78911],
  [73.06270, 18.78884], [73.06467, 18.78886], [73.06494, 18.78882], [73.06518, 18.78868],
  [73.06541, 18.78856], [73.06566, 18.78853], [73.06611, 18.78851], [73.06639, 18.78838],
  [73.06687, 18.78835], [73.06736, 18.78829], [73.06775, 18.78806], [73.06790, 18.78788],
  [73.06805, 18.78770], [73.06867, 18.78741], [73.06954, 18.78709], [73.06975, 18.78687],
  [73.06997, 18.78662], [73.07017, 18.78646], [73.07044, 18.78640], [73.07384, 18.78509],
  [73.07425, 18.78482], [73.07456, 18.78451], [73.07505, 18.78270], [73.07554, 18.78172],
  [73.07585, 18.78111], [73.07603, 18.78096], [73.07630, 18.78096], [73.07704, 18.78112],
  [73.07733, 18.78090], [73.07752, 18.78052], [73.07776, 18.78037], [73.07811, 18.78002],
  [73.07840, 18.77974], [73.07871, 18.77962], [73.07912, 18.77965], [73.07963, 18.78007],
  [73.07993, 18.78002], [73.08000, 18.78002],
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
function alongRaw(path: LngLat[], cum: number[], p: LngLat): { point: LngLat; along: number } {
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
