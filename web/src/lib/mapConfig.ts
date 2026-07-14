// mapConfig — single source of truth for the map basemap + the JNPA operational
// corridor framing/extent (JNPA Gate-1 → Karal Phata). Every map surface (2D
// MapView, 3D SceneView, geofence editor, PWA MiniMap) uses the Esri Satellite
// basemap and is BOTH framed on and clamped to this corridor so the operator/
// driver only ever sees the port corridor — never the wider Navi Mumbai / Uran
// region.
//
// The corridor geometry mirrors shared/jnpa_shared/corridor.py WAYPOINTS:
//   JNPA Gate-1 / NSICT (18.9489, 72.9492)  → SE down NH-348 →
//   Karal Phata junction (18.7800, 73.0800)
// i.e. a ~18 km wide × ~23 km tall bounding box, NOT the whole metropolitan area.
//
// NOTE: this is deliberately duplicated in mobile-pwa/src/lib/mapConfig.ts —
// `web/` and `mobile-pwa/` are independent Vite packages with separate build
// graphs, so the constants are copied rather than shared. Keep the two in sync
// when either changes.
import Extent from "@arcgis/core/geometry/Extent";
import type MapView from "@arcgis/core/views/MapView";

/** Esri Satellite basemap id (World Imagery), token-free. */
export const SATELLITE_BASEMAP = "satellite";

/** Corridor bounding-box mid-point [lon, lat] (mid of the extent below). */
export const CORRIDOR_CENTER: [number, number] = [73.0146, 18.8645];

/** Default corridor framing zoom — tight on the corridor, not the region.
 *  MUST be an integer: the satellite basemap is a TILED layer with integer LODs,
 *  and a fractional zoom/minZoom can leave the view with no resolvable LOD, which
 *  renders a BLANK map (no error). Keep this and CORRIDOR_MIN_ZOOM integer. */
export const CORRIDOR_ZOOM = 13;

/**
 * Bounding extent of the JNPA operational corridor (WGS84 / wkid 4326), padded
 * ~2 km around the NH-348 waypoint envelope (lon 72.9492–73.08, lat 18.78–18.9489).
 * This is the hard pan/clamp boundary — the view centre can never leave it.
 */
export const CORRIDOR_EXTENT = {
  xmin: 72.93,
  ymin: 18.75,
  xmax: 73.11,
  ymax: 18.97,
  spatialReference: { wkid: 4326 },
} as const;

/**
 * Minimum zoom the operator may zoom out to. Set just below the initial framing
 * so the corridor stays filling the viewport and the surrounding region can
 * never be pulled into frame.
 */
export const CORRIDOR_MIN_ZOOM = 13;

/** Fresh Extent instance for the corridor (constraints/goTo consume a geometry). */
export function corridorExtent(): Extent {
  return new Extent({ ...CORRIDOR_EXTENT });
}

/**
 * Constraints object for an Esri MapView `constraints`: clamps panning to the
 * corridor extent, prevents zooming out past the corridor, disables rotation.
 */
export function buildCorridorConstraints(): {
  geometry: Extent;
  minZoom: number;
  rotationEnabled: boolean;
} {
  return {
    geometry: corridorExtent(),
    minZoom: CORRIDOR_MIN_ZOOM,
    rotationEnabled: false,
  };
}

/**
 * Apply the corridor clamp AND frame the initial viewpoint tightly on the
 * corridor for a ready 2D MapView. Call this once from the view-ready handler of
 * every map surface.
 *
 * Two things matter and both are done here:
 *   1. HARD CLAMP — assign `constraints` (extent geometry + minZoom + no
 *      rotation) so the user cannot pan/zoom the corridor out of frame. We set
 *      the sub-properties on the live constraints object (the SDK re-clamps on
 *      the next interaction).
 *   2. INITIAL FRAMING — `goTo` the corridor centre/zoom with no animation so
 *      the FIRST painted frame is already the corridor, not the wider default
 *      extent the element mounts with. Without this the map opens on the whole
 *      region even though the clamp is in place.
 */
export function applyCorridorView(view: MapView): void {
  // Defer to view.when() so constraints only run once the view has a spatial
  // reference + LOD set. We DO NOT call view.goTo here: the map element mounts at
  // CORRIDOR_CENTER / CORRIDOR_ZOOM already (so it opens framed on the corridor),
  // and a goTo during ready both risked a "reading 'scale'" crash AND, together
  // with a fractional minZoom, could leave the tiled satellite layer with no
  // resolvable LOD → a blank map. Applying only the (integer) constraints keeps
  // the basemap rendering while still hard-clamping pan/zoom to the corridor.
  view
    .when(() => {
      try {
        if (view.constraints) {
          view.constraints.snapToZoom = false;
          view.constraints.minZoom = CORRIDOR_MIN_ZOOM;
          view.constraints.rotationEnabled = false;
          view.constraints.geometry = corridorExtent();
        }
        // NOTE: we deliberately do NOT fit the whole corridor *extent* — the
        // corridor is a tall diagonal, and fitting it into a wide landscape panel
        // forces a huge horizontal span (the region comes into view). Instead the
        // map opens at CORRIDOR_CENTER / CORRIDOR_ZOOM (a tight zoom on the corridor
        // mid-point) and the constraints below hard-clamp pan + zoom-out, so the
        // surrounding Navi Mumbai / Uran region is never pulled into frame.
      } catch {
        /* never let corridor framing break the map */
      }
    })
    .catch(() => {});
}
