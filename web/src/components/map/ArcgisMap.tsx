// ArcgisMap — the UC1-parity map surface for UC-III.
//
// Wraps <arcgis-map> (ArcGIS Maps SDK for JavaScript web component, via the
// official @arcgis/map-components-react React wrapper — NO deprecated widget
// classes). The component is fully DATA-DRIVEN: screens pass adapter data
// (gates, corridor, zones, snapshots, trucks, parkingFacilities) as props and
// this component projects each into a dedicated GraphicsLayer. It owns no data
// fetching of its own.
//
// Layers (bottom → top):
//   heatmap   — congestion from traffic snapshots (graduated point renderer)
//   zones     — geofence / no-parking polygons
//   corridor  — NH-348 polyline, coloured per-segment by jam factor
//   parking   — parking facilities (status-coloured squares)
//   trucks    — live truck dots (1:50 sample)
//   gates     — gate markers, coloured by throughput utilisation
//
// Colours come exclusively from src/lib/tokens.ts (single source of truth).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
// Register the <arcgis-map> custom element + bundle its runtime locally. The
// React wrapper below only creates the React→element binding; this side-effect
// import is what actually defines the element (otherwise it never upgrades).
import "@arcgis/map-components/components/arcgis-map";
// Layer-list toggle (GIS-5): an operator can show/hide each operational layer
// (gates, road network, geofences, heatmap, parking, corridor). The web
// component auto-binds to the parent <arcgis-map> view.
import "@arcgis/map-components/components/arcgis-layer-list";
import { ArcgisMap as ArcgisMapWC, ArcgisLayerList } from "@arcgis/map-components-react";
import GraphicsLayer from "@arcgis/core/layers/GraphicsLayer";
import Graphic from "@arcgis/core/Graphic";
import Point from "@arcgis/core/geometry/Point";
import Polyline from "@arcgis/core/geometry/Polyline";
import Polygon from "@arcgis/core/geometry/Polygon";
import SimpleMarkerSymbol from "@arcgis/core/symbols/SimpleMarkerSymbol";
import SimpleLineSymbol from "@arcgis/core/symbols/SimpleLineSymbol";
import SimpleFillSymbol from "@arcgis/core/symbols/SimpleFillSymbol";
import type MapView from "@arcgis/core/views/MapView";
import esriConfig from "@arcgis/core/config";

import type {
  CorridorGeometry,
  Gate,
  ParkingFacility,
  TrafficSnapshot,
  TruckDevice,
  Zone,
} from "@/lib/types";
import { gateColour, jamColour, MAP_TOKENS, parkingStatusColour, zoneColour } from "@/lib/tokens";
import { JNPA_CENTER, JNPA_ZOOM } from "@/lib/basemap";

const DEFAULT_BASEMAP = "dark-gray-vector";
// Spotlight halo colour (CB-safe info blue, matching the guided-tour ring tone).
const HIGHLIGHT_COLOUR = "#56B4E9";

export interface ArcgisMapProps {
  /** NH-348 corridor geometry (polyline + segments). */
  corridor?: CorridorGeometry;
  /** Gate facilities. */
  gates?: Gate[];
  /** Geofence / no-parking zones. */
  zones?: Zone[];
  /** Per-segment traffic snapshots, keyed by segment_id, drive corridor + heat. */
  snapshots?: TrafficSnapshot[];
  /** Live truck positions (already sampled by the gateway). */
  trucks?: TruckDevice[];
  /** Parking facilities. */
  parkingFacilities?: ParkingFacility[];
  /**
   * Asset ids the guided What-If tour is spotlighting for the current step
   * (gate ids / corridor segment ids). The map rings each with a halo and
   * pans/zooms to frame them — the direct analog of the reference project's
   * PortMap `highlights` prop (highlightGraphics + view.goTo). Empty = no focus.
   */
  highlights?: string[];
  /** Override basemap (default "dark-gray-vector" — no API key needed in dev). */
  basemap?: string;
  /** Map centre [lon, lat]; defaults to the JNPA corridor mid-point. */
  center?: [number, number];
  /** Initial zoom. */
  zoom?: number;
  /** Fired when a gate graphic is clicked. */
  onGateClick?: (gateId: string) => void;
  /** Fired once the MapView is ready, so a screen can pan/fly programmatically. */
  onViewReady?: (view: MapView) => void;
  className?: string;
}

const WGS84 = { wkid: 4326 } as const;

/** Handle returned by view.on(...) — typed without naming the module-scoped IHandle. */
type ViewHandle = ReturnType<MapView["on"]>;

// Apply an API key once at module load if one is provided. Basemaps like
// "dark-gray-vector" do NOT require a key in dev, so this is a graceful upgrade
// rather than a hard dependency.
const ARCGIS_API_KEY = (() => {
  const key = import.meta.env.VITE_ARCGIS_API_KEY;
  return typeof key === "string" && key.trim() ? key.trim() : undefined;
})();
if (ARCGIS_API_KEY) {
  esriConfig.apiKey = ARCGIS_API_KEY;
}

export function ArcgisMap({
  corridor,
  gates = [],
  zones = [],
  snapshots = [],
  trucks = [],
  parkingFacilities = [],
  highlights = [],
  basemap = DEFAULT_BASEMAP,
  center = JNPA_CENTER,
  zoom = JNPA_ZOOM,
  onGateClick,
  onViewReady,
  className,
}: ArcgisMapProps) {
  const viewRef = useRef<MapView | null>(null);
  // Flips true once the MapView is ready. Because /live remounts each time the
  // guided tour navigates to it, the spotlight effect must (re)run after the view
  // exists — otherwise the first frame on a fresh mount never zooms.
  const [viewReady, setViewReady] = useState(false);
  const layers = useRef<{
    heatmap: GraphicsLayer;
    zones: GraphicsLayer;
    corridor: GraphicsLayer;
    parking: GraphicsLayer;
    trucks: GraphicsLayer;
    gates: GraphicsLayer;
    highlight: GraphicsLayer;
  } | null>(null);
  const clickHandle = useRef<ViewHandle | null>(null);
  // Last spotlight id-set we framed, so we only re-zoom when it changes — exactly
  // the reference PortMap's lastZoomKey guard.
  const lastZoomKey = useRef<string>("");
  const onGateClickRef = useRef(onGateClick);
  onGateClickRef.current = onGateClick;

  // ---- create the layer set once the view is ready ----------------------
  const handleReady = useCallback(
    (event: { target: { view: MapView; addLayers?: unknown } }) => {
      const view = event.target.view;
      if (!view || !view.map) return;
      viewRef.current = view;

      // GraphicsLayers, ordered bottom → top via add order.
      const mk = (id: string) => new GraphicsLayer({ id, title: id });
      const set = {
        heatmap: mk("uc3-heatmap"),
        zones: mk("uc3-zones"),
        corridor: mk("uc3-corridor"),
        parking: mk("uc3-parking"),
        trucks: mk("uc3-trucks"),
        gates: mk("uc3-gates"),
        // Spotlight halos sit on top so the ring is never occluded.
        highlight: mk("uc3-highlight"),
      };
      layers.current = set;
      view.map.addMany([
        set.heatmap,
        set.zones,
        set.corridor,
        set.parking,
        set.trucks,
        set.gates,
        set.highlight,
      ]);

      // Gate click → callback.
      clickHandle.current?.remove();
      clickHandle.current = view.on("click", (e) => {
        void view.hitTest(e).then((res) => {
          const hit = res.results.find(
            (r) => r.type === "graphic" && r.graphic?.layer === layers.current?.gates,
          );
          if (hit && hit.type === "graphic") {
            const gateId = hit.graphic.getAttribute("id") as string | undefined;
            if (gateId) onGateClickRef.current?.(gateId);
          }
        });
      });

      // Paint everything we already have.
      renderAll();
      onViewReady?.(view);
      // Signal readiness so the spotlight effect frames the current assets even
      // on a fresh mount (the effect runs once before the view exists).
      setViewReady(true);
    },
    // renderAll/onViewReady are stable enough for our purposes; deps kept tight.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  // ---- render helpers ---------------------------------------------------
  const renderAll = useCallback(() => {
    renderHeatmap();
    renderZones();
    renderCorridor();
    renderParking();
    renderTrucks();
    renderGates();
    renderHighlight();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function renderCorridor() {
    const layer = layers.current?.corridor;
    if (!layer) return;
    layer.removeAll();
    if (!corridor) return;
    const jamBySeg = new Map(snapshots.map((s) => [s.segment_id, s.jam_factor]));
    for (const seg of corridor.segments) {
      const jf = jamBySeg.get(seg.id) ?? 0;
      layer.add(
        new Graphic({
          geometry: new Polyline({
            paths: [[seg.start, seg.end]],
            spatialReference: WGS84,
          }),
          symbol: new SimpleLineSymbol({
            color: jamColour(jf),
            width: 5,
            cap: "round",
            join: "round",
          }),
          attributes: { id: seg.id, jam: jf },
          popupTemplate: {
            title: `Segment ${seg.id}`,
            content: `Jam factor: ${jf} · ${seg.length_km.toFixed(2)} km`,
          },
        }),
      );
    }
  }

  function renderHeatmap() {
    const layer = layers.current?.heatmap;
    if (!layer) return;
    layer.removeAll();
    if (!corridor) return;
    const jamBySeg = new Map(snapshots.map((s) => [s.segment_id, s.jam_factor]));
    for (const seg of corridor.segments) {
      const jfRaw = jamBySeg.get(seg.id) ?? 0;
      const ratio = jfRaw > 1 ? jfRaw / 10 : jfRaw;
      if (ratio <= 0) continue;
      const mid: [number, number] = [
        (seg.start[0] + seg.end[0]) / 2,
        (seg.start[1] + seg.end[1]) / 2,
      ];
      // Graduated translucent halo as a lightweight congestion "heat" cue.
      const stop =
        MAP_TOKENS.heatStops.find((s) => ratio <= s.ratio) ??
        MAP_TOKENS.heatStops[MAP_TOKENS.heatStops.length - 1];
      layer.add(
        new Graphic({
          geometry: new Point({ longitude: mid[0], latitude: mid[1] }),
          symbol: new SimpleMarkerSymbol({
            style: "circle",
            color: stop.color,
            size: 18 + ratio * 22,
            outline: { color: [0, 0, 0, 0], width: 0 },
          }),
          attributes: { segment_id: seg.id, ratio },
        }),
      );
    }
  }

  function renderZones() {
    const layer = layers.current?.zones;
    if (!layer) return;
    layer.removeAll();
    for (const z of zones) {
      if (!z.polygon || z.polygon.length < 3) continue;
      const fill = zoneColour(z.kind);
      layer.add(
        new Graphic({
          geometry: new Polygon({
            rings: [closeRing(z.polygon)],
            spatialReference: WGS84,
          }),
          symbol: new SimpleFillSymbol({
            color: hexToRgba(fill, 0.18),
            outline: new SimpleLineSymbol({ color: fill, width: 1.5 }),
          }),
          attributes: { id: z.id, name: z.name, kind: z.kind },
          popupTemplate: { title: z.name, content: `Zone kind: ${z.kind}` },
        }),
      );
    }
  }

  function renderParking() {
    const layer = layers.current?.parking;
    if (!layer) return;
    layer.removeAll();
    for (const p of parkingFacilities) {
      layer.add(
        new Graphic({
          geometry: new Point({ longitude: p.lon, latitude: p.lat }),
          symbol: new SimpleMarkerSymbol({
            style: "square",
            color: parkingStatusColour(p.status),
            size: 11,
            outline: { color: MAP_TOKENS.gateStroke, width: 1.5 },
          }),
          attributes: { id: p.facility_id, name: p.name },
          popupTemplate: {
            title: p.name,
            content: `Occupied ${p.occupied}/${p.capacity} · ${p.status}`,
          },
        }),
      );
    }
  }

  function renderTrucks() {
    const layer = layers.current?.trucks;
    if (!layer) return;
    layer.removeAll();
    for (const t of trucks) {
      if (typeof t.position?.lon !== "number" || typeof t.position?.lat !== "number") continue;
      layer.add(
        new Graphic({
          geometry: new Point({
            longitude: t.position.lon,
            latitude: t.position.lat,
          }),
          symbol: new SimpleMarkerSymbol({
            style: "circle",
            color: MAP_TOKENS.truckFill,
            size: 7,
            outline: { color: MAP_TOKENS.truckStroke, width: 1 },
          }),
          attributes: { id: t.device_id, plate: t.plate ?? "" },
        }),
      );
    }
  }

  function renderGates() {
    const layer = layers.current?.gates;
    if (!layer) return;
    layer.removeAll();
    for (const g of gates) {
      layer.add(
        new Graphic({
          geometry: new Point({ longitude: g.lon, latitude: g.lat }),
          symbol: new SimpleMarkerSymbol({
            style: "circle",
            color: gateColour(g.utilisation),
            size: 15,
            outline: { color: MAP_TOKENS.gateStroke, width: 2 },
          }),
          attributes: {
            id: g.id,
            name: g.name,
            util: g.utilisation,
          },
          popupTemplate: {
            title: g.name || g.id,
            content: `Throughput ${g.throughput_60min}/${g.target_vph} vph`,
          },
        }),
      );
    }
  }

  // Resolve a spotlight asset id (gate or corridor segment) to its on-map point.
  // Gates use their explicit lon/lat; a segment uses its mid-point, normalised so
  // the halo lands at the correct geographic location regardless of whether the
  // segment coords are authored [lon,lat] or [lat,lon] (JNPA lat ≈ 18.9 ≤ 30,
  // lon ≈ 73 > 30, so the value ≤ 30 is the latitude).
  function geomFor(id: string): Point | null {
    const g = gates.find((x) => x.id === id);
    if (g) return new Point({ longitude: g.lon, latitude: g.lat, spatialReference: WGS84 });
    const seg = corridor?.segments.find((s) => s.id === id);
    if (seg) {
      const a = (seg.start[0] + seg.end[0]) / 2;
      const b = (seg.start[1] + seg.end[1]) / 2;
      const lat = Math.abs(a) <= 30 ? a : b;
      const lon = Math.abs(a) <= 30 ? b : a;
      return new Point({ longitude: lon, latitude: lat, spatialReference: WGS84 });
    }
    return null;
  }

  // Spotlight halos — a static double ring around each spotlighted asset (the
  // analog of the reference highlightedAssetsLayer's outline halo).
  function renderHighlight() {
    const layer = layers.current?.highlight;
    if (!layer) return;
    layer.removeAll();
    for (const id of highlights) {
      const geom = geomFor(id);
      if (!geom) continue;
      // Outer faint ring + inner solid ring read as a halo without animation.
      layer.add(
        new Graphic({
          geometry: geom,
          symbol: new SimpleMarkerSymbol({
            style: "circle",
            color: [0, 0, 0, 0],
            size: 46,
            outline: { color: hexToRgba(HIGHLIGHT_COLOUR, 0.45), width: 2 },
          }),
          attributes: { id },
        }),
      );
      layer.add(
        new Graphic({
          geometry: geom,
          symbol: new SimpleMarkerSymbol({
            style: "circle",
            color: [0, 0, 0, 0],
            size: 30,
            outline: { color: HIGHLIGHT_COLOUR, width: 4 },
          }),
          attributes: { id },
        }),
      );
    }
  }

  // ---- reactive prop → layer updates ------------------------------------
  useEffect(() => {
    renderCorridor();
    renderHeatmap();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [corridor, snapshots]);

  // Spotlight sync — reproduces the reference PortMap effect exactly: redraw the
  // halos, then (only when the spotlight id-set changes — lastZoomKey guard)
  // pan/zoom-frame the assets. Single asset → zoom in to ≥ 14; multiple → frame
  // them all. Re-runs when the asset geometry sources change so the halo tracks.
  useEffect(() => {
    renderHighlight();
    const view = viewRef.current;
    const targets = highlights.map(geomFor).filter((g): g is Point => g != null);
    const zoomKey = [...highlights].sort().join("|");
    if (view && targets.length > 0 && zoomKey !== lastZoomKey.current) {
      lastZoomKey.current = zoomKey;
      void view.when(() => {
        void view
          .goTo(
            targets.length === 1
              ? { target: targets[0], zoom: Math.max(view.zoom ?? 0, 14) }
              : { target: targets },
            { duration: 700, easing: "ease-in-out" },
          )
          // goTo rejects if a newer animation interrupts it — expected, ignore.
          .catch(() => {});
      });
    } else if (targets.length === 0) {
      lastZoomKey.current = "";
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [highlights, gates, corridor, viewReady]);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => renderZones(), [zones]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => renderGates(), [gates]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => renderTrucks(), [trucks]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => renderParking(), [parkingFacilities]);

  // Cleanup the click handler on unmount.
  useEffect(() => {
    return () => {
      clickHandle.current?.remove();
      clickHandle.current = null;
    };
  }, []);

  // The initial centre Point (the prop's getter type is Point, not a tuple).
  // Only the FIRST value is honoured by the element, so we memoise on mount.
  const initialCenter = useMemo(
    () => new Point({ longitude: center[0], latitude: center[1] }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  return (
    <div
      data-testid="live-map"
      className={className ?? "h-full w-full"}
      style={{ position: "relative" }}
    >
      <ArcgisMapWC
        basemap={basemap}
        // The typed `center` prop is a Point (the element's getter type), so we
        // build one from the [lon, lat] tuple rather than passing the array.
        center={initialCenter}
        zoom={zoom}
        onArcgisViewReadyChange={handleReady}
        style={{ height: "100%", width: "100%" }}
      >
        {/* Layer-toggle control (GIS-5). position via the map's UI manager. */}
        <ArcgisLayerList position="top-left" />
      </ArcgisMapWC>
    </div>
  );
}

export default ArcgisMap;

// ---- small geometry / colour helpers ------------------------------------
function closeRing(ring: [number, number][]): [number, number][] {
  if (
    ring.length &&
    (ring[0][0] !== ring[ring.length - 1][0] || ring[0][1] !== ring[ring.length - 1][1])
  ) {
    return [...ring, ring[0]];
  }
  return ring;
}

/** Convert "#RRGGBB" + alpha → [r,g,b,a] tuple for ArcGIS Color. */
function hexToRgba(hex: string, alpha: number): [number, number, number, number] {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return [r, g, b, alpha];
}
