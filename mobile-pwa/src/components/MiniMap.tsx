import { useCallback, useEffect, useMemo, useRef, useState } from "react";
// Register the <arcgis-map> custom element + bundle its runtime locally (the
// same side-effect import the dashboard uses in web/src/components/map/
// ArcgisMap.tsx). The React wrapper below only creates the React->element
// binding; this import is what actually defines/upgrades the element.
import "@arcgis/map-components/components/arcgis-map";
import { ArcgisMap as ArcgisMapWC } from "@arcgis/map-components-react";
import GraphicsLayer from "@arcgis/core/layers/GraphicsLayer";
import Graphic from "@arcgis/core/Graphic";
import Point from "@arcgis/core/geometry/Point";
import Polyline from "@arcgis/core/geometry/Polyline";
import SimpleMarkerSymbol from "@arcgis/core/symbols/SimpleMarkerSymbol";
import SimpleLineSymbol from "@arcgis/core/symbols/SimpleLineSymbol";
import PictureMarkerSymbol from "@arcgis/core/symbols/PictureMarkerSymbol";
import type MapView from "@arcgis/core/views/MapView";
import { basemapId } from "@/lib/basemap";
import { applyCorridorView } from "@/lib/mapConfig";
import type { CorridorGeometry, DevicePosition, Gate } from "@/lib/types";

// One route option to draw (Google-Maps-style): a polyline in [lon,lat] pairs,
// flagged primary (highlighted) or alternate (greyed). `id` keys the feature.
export interface RouteLine {
  id: string;
  coords: [number, number][];
  primary?: boolean;
}

// "Traffic ahead" mini-map — now rendered on the ArcGIS Maps SDK (Esri), the
// SAME engine as the operations dashboard, so the driver PWA and control room
// share one map stack (not just the same imagery). It loads an Esri basemap
// (imagery+labels hybrid by default; see lib/basemap.ts) and overlays ArcGIS
// GraphicsLayers for the corridor polyline, the four gates, route options, the
// destination pin, parking POIs and the live directional truck marker.

interface Props {
  corridor?: CorridorGeometry;
  gates?: Gate[];
  truck?: DevicePosition | null;
  targetGateId?: string | null;
  // segment_id -> jam_factor (0..1), drives the "traffic ahead" colour.
  jam?: Record<string, number>;
  // Fill the parent container (full-screen nav map) instead of the fixed 200px.
  fill?: boolean;
  // Use the Esri street navigation basemap instead of the default (imagery).
  roads?: boolean;
  // Multiple route options to draw (primary highlighted, alternates greyed).
  routes?: RouteLine[];
  // Compass heading in degrees (0 = N) — rotates the directional truck marker.
  heading?: number | null;
  // Destination pin (the target gate) — drawn as a Maps-style teardrop pin.
  destination?: { lat: number; lon: number; name?: string } | null;
  // Parking POIs — drawn as green "P" markers.
  parking?: { id: string; lat: number; lon: number; available?: number | null }[];
  // When set with a destination, the map opens framed on truck → destination.
  frameToTrip?: boolean;
}

const WGS84 = { wkid: 4326 } as const;
// Opening camera — the JNPA corridor framing the old MapLibre map used.
const INITIAL_CENTER: [number, number] = [72.952, 18.948];
const INITIAL_ZOOM = 11.5;

function jamColor(j: number): string {
  if (j >= 0.66) return "#d55e00"; // vermillion — heavy
  if (j >= 0.33) return "#e69f00"; // orange — moderate
  return "#009e73"; // green — free-flow
}

/** Encode an inline SVG as a data URI for a PictureMarkerSymbol. */
function svgUri(svg: string): string {
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
}

// Directional navigation puck (points north at angle 0; PictureMarkerSymbol
// `angle` rotates it clockwise to the compass heading).
const TRUCK_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="30" height="30" viewBox="0 0 24 24">' +
  '<circle cx="12" cy="12" r="10" fill="#1f78c2" stroke="#fff" stroke-width="2.5"/>' +
  '<path d="M12 6.5 15.5 15 12 13 8.5 15Z" fill="#fff"/></svg>';

// Maps-style teardrop destination pin (tip at the bottom).
const DEST_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="30" height="38" viewBox="0 0 24 30">' +
  '<path d="M12 0C6.5 0 2 4.4 2 9.9 2 17 12 30 12 30s10-13 10-20.1C22 4.4 17.5 0 12 0Z" fill="#c4441f" stroke="#fff" stroke-width="2"/>' +
  '<circle cx="12" cy="10" r="4" fill="#fff"/></svg>';

function parkingSvg(full: boolean): string {
  return (
    '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">' +
    `<circle cx="11" cy="11" r="9" fill="${full ? "#8a94a6" : "#007a5a"}" stroke="#fff" stroke-width="2"/>` +
    '<text x="11" y="15.5" font-family="Arial, sans-serif" font-size="13" font-weight="800" ' +
    'fill="#fff" text-anchor="middle">P</text></svg>'
  );
}

export default function MiniMap({
  corridor,
  gates,
  truck,
  targetGateId,
  jam,
  fill,
  roads,
  routes,
  heading,
  destination,
  parking,
  frameToTrip,
}: Props) {
  const viewRef = useRef<MapView | null>(null);
  const layers = useRef<{
    corridor: GraphicsLayer;
    routes: GraphicsLayer;
    gates: GraphicsLayer;
    parking: GraphicsLayer;
    destination: GraphicsLayer;
    truck: GraphicsLayer;
  } | null>(null);
  // Flips true once the MapView is ready so the render effects (re)run and paint
  // whatever data props already exist on this mount.
  const [ready, setReady] = useState(false);
  // Fit-once guards, mirroring the previous MapLibre implementation.
  const didCorridorFit = useRef(false);
  const framedTrip = useRef(false);

  // ---- view ready: build the GraphicsLayer stack ------------------------
  const handleReady = useCallback((event: { target: { view: MapView } }) => {
    const view = event.target.view;
    if (!view || !view.map) return;
    viewRef.current = view;
    // Frame + hard-clamp the driver map to the JNPA operational corridor so it
    // opens on the port corridor and can never be panned/zoomed out to the wider
    // region. (A live trip, when present, re-frames within this clamp below.)
    applyCorridorView(view);
    // Keep only the compact zoom + attribution UI on the small driver map.
    view.ui.components = ["zoom", "attribution"];

    const mk = (id: string) => new GraphicsLayer({ id });
    const set = {
      corridor: mk("mm-corridor"),
      routes: mk("mm-routes"),
      gates: mk("mm-gates"),
      parking: mk("mm-parking"),
      destination: mk("mm-destination"),
      truck: mk("mm-truck"),
    };
    layers.current = set;
    // Bottom -> top: corridor/routes under markers; truck drawn topmost.
    view.map.addMany([
      set.corridor,
      set.routes,
      set.gates,
      set.parking,
      set.destination,
      set.truck,
    ]);
    setReady(true);
  }, []);

  // ---- render helpers ---------------------------------------------------
  const goTo = useCallback((target: unknown, zoom?: number) => {
    const view = viewRef.current;
    if (!view) return;
    void view
      .when(() =>
        view.goTo(zoom != null ? { target, zoom } : (target as never), {
          duration: 500,
          easing: "ease-in-out",
        }),
      )
      // goTo rejects when a newer animation interrupts it — expected, ignore.
      .catch(() => {});
  }, []);

  const renderCorridor = useCallback(() => {
    const layer = layers.current?.corridor;
    if (!layer) return;
    layer.removeAll();
    // Corridor hidden (e.g. Navigate once a route is drawn) — leave it cleared.
    if (!corridor) return;
    const worstJam = jam ? Math.max(0, ...Object.values(jam)) : 0;
    const geometry = new Polyline({ paths: [corridor.polyline], spatialReference: WGS84 });
    layer.add(
      new Graphic({
        geometry,
        symbol: new SimpleLineSymbol({
          color: jam ? jamColor(worstJam) : "#94a3b8",
          width: 4,
          cap: "round",
          join: "round",
        }),
      }),
    );
    // Fit to the corridor once (only when the caller isn't framing the trip).
    if (!frameToTrip && !didCorridorFit.current) {
      didCorridorFit.current = true;
      goTo(geometry);
    }
  }, [corridor, jam, frameToTrip, goTo]);

  const renderGates = useCallback(() => {
    const layer = layers.current?.gates;
    if (!layer) return;
    layer.removeAll();
    for (const g of gates ?? []) {
      const target = g.id === targetGateId;
      layer.add(
        new Graphic({
          geometry: new Point({ longitude: g.lon, latitude: g.lat, spatialReference: WGS84 }),
          symbol: new SimpleMarkerSymbol({
            style: "circle",
            color: target ? "#1f78c2" : "#64748b",
            size: target ? 16 : 10,
            outline: { color: "#ffffff", width: 2 },
          }),
          attributes: { id: g.id },
        }),
      );
    }
  }, [gates, targetGateId]);

  const renderRoutes = useCallback(() => {
    const layer = layers.current?.routes;
    if (!layer) return;
    layer.removeAll();
    if (!routes || !routes.length) return;
    // Order so the primary route draws last (on top).
    const ordered = [...routes].sort((a, b) => Number(!!a.primary) - Number(!!b.primary));
    // Casings first (all under the coloured lines) for a clean Maps-style look.
    for (const r of ordered) {
      const geometry = new Polyline({ paths: [r.coords], spatialReference: WGS84 });
      layer.add(
        new Graphic({
          geometry,
          symbol: new SimpleLineSymbol({
            color: r.primary ? [26, 86, 219, 0.35] : [154, 164, 178, 0.25],
            width: r.primary ? 9 : 6,
            cap: "round",
            join: "round",
          }),
        }),
      );
    }
    for (const r of ordered) {
      const geometry = new Polyline({ paths: [r.coords], spatialReference: WGS84 });
      layer.add(
        new Graphic({
          geometry,
          symbol: new SimpleLineSymbol({
            color: r.primary ? "#1a56db" : "#7b8794",
            width: r.primary ? 5 : 3.5,
            cap: "round",
            join: "round",
          }),
          attributes: { id: r.id, primary: !!r.primary },
        }),
      );
    }
    // Frame all route geometries together.
    goTo(ordered.map((r) => new Polyline({ paths: [r.coords], spatialReference: WGS84 })));
  }, [routes, goTo]);

  const renderTruck = useCallback(() => {
    const layer = layers.current?.truck;
    if (!layer) return;
    layer.removeAll();
    if (!truck) return;
    const angle = heading != null && Number.isFinite(heading) ? heading : 0;
    layer.add(
      new Graphic({
        geometry: new Point({ longitude: truck.lon, latitude: truck.lat, spatialReference: WGS84 }),
        symbol: new PictureMarkerSymbol({ url: svgUri(TRUCK_SVG), width: 30, height: 30, angle }),
      }),
    );
  }, [truck, heading]);

  const renderDestination = useCallback(() => {
    const layer = layers.current?.destination;
    if (!layer) return;
    layer.removeAll();
    if (!destination) return;
    layer.add(
      new Graphic({
        geometry: new Point({
          longitude: destination.lon,
          latitude: destination.lat,
          spatialReference: WGS84,
        }),
        // yoffset lifts the 38px teardrop so its tip (bottom) sits on the point.
        symbol: new PictureMarkerSymbol({
          url: svgUri(DEST_SVG),
          width: 30,
          height: 38,
          yoffset: 19,
        }),
      }),
    );
  }, [destination]);

  const renderParking = useCallback(() => {
    const layer = layers.current?.parking;
    if (!layer) return;
    layer.removeAll();
    for (const p of parking ?? []) {
      if (p.lat == null || p.lon == null) continue;
      const full = (p.available ?? 1) <= 0;
      layer.add(
        new Graphic({
          geometry: new Point({ longitude: p.lon, latitude: p.lat, spatialReference: WGS84 }),
          symbol: new PictureMarkerSymbol({ url: svgUri(parkingSvg(full)), width: 22, height: 22 }),
          attributes: { id: p.id },
        }),
      );
    }
  }, [parking]);

  // ---- reactive prop -> layer updates -----------------------------------
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (ready) renderCorridor();
  }, [ready, renderCorridor]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (ready) renderGates();
  }, [ready, renderGates]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (ready) renderRoutes();
  }, [ready, renderRoutes]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (ready) renderTruck();
  }, [ready, renderTruck]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (ready) renderDestination();
  }, [ready, renderDestination]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (ready) renderParking();
  }, [ready, renderParking]);

  // Reserve room for the floating cards (top destination card + bottom sheet) on
  // the full-screen nav map so framing never hides the route/markers behind them.
  useEffect(() => {
    const view = viewRef.current;
    if (!ready || !view) return;
    view.padding =
      fill && (frameToTrip || (routes && routes.length))
        ? { top: 96, bottom: 190, left: 40, right: 40 }
        : { top: 12, bottom: 12, left: 12, right: 12 };
  }, [ready, fill, frameToTrip, routes]);

  // Open framed on truck → destination (once), so the driver sees the whole trip
  // before the route tightens the view.
  useEffect(() => {
    if (!ready || !frameToTrip || framedTrip.current || !destination) return;
    const destPt = new Point({
      longitude: destination.lon,
      latitude: destination.lat,
      spatialReference: WGS84,
    });
    if (!truck) {
      // Only the gate is known so far — centre on it, but DON'T lock framing so a
      // proper truck→gate fit still runs once the first fix arrives.
      goTo(destPt, 13.5);
      return;
    }
    const truckPt = new Point({
      longitude: truck.lon,
      latitude: truck.lat,
      spatialReference: WGS84,
    });
    goTo([destPt, truckPt]);
    framedTrip.current = true;
  }, [ready, truck, destination, frameToTrip, goTo]);

  // The map element is created EXACTLY ONCE and reused across re-renders. The
  // @arcgis/map-components-react wrapper re-applies element props (center/zoom)
  // on every React render; because this component re-renders on each truck/
  // heading tick, freezing the element keeps the wrapper from re-commanding the
  // camera and snapping back to the initial framing. All later updates flow
  // through the GraphicsLayers + imperative goTo above. (Same guard the
  // dashboard's ArcgisMap uses.)
  const initialCenter = useMemo(
    () => new Point({ longitude: INITIAL_CENTER[0], latitude: INITIAL_CENTER[1] }),
    [],
  );
  const mapElement = useMemo(
    () => (
      <ArcgisMapWC
        basemap={basemapId(roads)}
        center={initialCenter}
        zoom={INITIAL_ZOOM}
        onArcgisViewReadyChange={handleReady}
        style={{ height: "100%", width: "100%" }}
      />
    ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  return <div className={fill ? "minimap minimap-fill" : "minimap"}>{mapElement}</div>;
}
