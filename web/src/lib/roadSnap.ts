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

/**
 * A reusable index over a polyline: the cumulative metric length at each vertex.
 * Build once per snapped route, then reuse across every project/slice call.
 */
export interface PathIndex {
  path: LngLat[];
  cum: number[];
  total: number;
}

export function buildPathIndex(path: LngLat[]): PathIndex {
  const cum = [0];
  for (let i = 1; i < path.length; i++) {
    cum[i] = cum[i - 1] + mlen(path[i - 1], path[i]);
  }
  return { path, cum, total: cum[cum.length - 1] ?? 0 };
}

/** Snap `p` onto the polyline; returns the on-road point + its distance along it. */
export function projectOnPath(idx: PathIndex, p: LngLat): { point: LngLat; along: number } {
  const { path, cum } = idx;
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
