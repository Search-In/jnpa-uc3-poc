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
import { useTranslation } from "react-i18next";
// Register the <arcgis-map> custom element + bundle its runtime locally. The
// React wrapper below only creates the React→element binding; this side-effect
// import is what actually defines the element (otherwise it never upgrades).
import "@arcgis/map-components/components/arcgis-map";
import { ArcgisMap as ArcgisMapWC } from "@arcgis/map-components-react";
import { Layers as LayersIcon, X as XIcon } from "lucide-react";
import GraphicsLayer from "@arcgis/core/layers/GraphicsLayer";
import Graphic from "@arcgis/core/Graphic";
import Point from "@arcgis/core/geometry/Point";
import Polyline from "@arcgis/core/geometry/Polyline";
import Polygon from "@arcgis/core/geometry/Polygon";
import SimpleMarkerSymbol from "@arcgis/core/symbols/SimpleMarkerSymbol";
import SimpleLineSymbol from "@arcgis/core/symbols/SimpleLineSymbol";
import SimpleFillSymbol from "@arcgis/core/symbols/SimpleFillSymbol";
import TextSymbol from "@arcgis/core/symbols/TextSymbol";
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
import {
  snapPathToRoads,
  buildPathIndex,
  projectOnPath,
  sliceBetween,
  type PathIndex,
  type LngLat,
} from "@/lib/roadSnap";
import { useClickOutside } from "@/hooks/useClickOutside";
// 3D counterpart of this map — fed the SAME live-data props so a 2D↔3D toggle is
// a pure view change over one data source (no separate page, no simulator/mock).
import { Scene3D } from "./scene3d/Scene3D";

const DEFAULT_BASEMAP = "dark-gray-vector";
// Soft outer road "shoulder" drawn under the corridor casing so the ribbon has a
// graduated, anti-aliased edge (a real-road look) instead of a hard boundary.
// Purely cosmetic; kept local alongside the other map-only colour constants.
const CORRIDOR_SHOULDER = "rgba(6,14,24,0.28)";
// Spotlight halo colour (CB-safe info blue, matching the guided-tour ring tone).
const HIGHLIGHT_COLOUR = "#56B4E9";
// Alert-focus halo colour (CB-safe orange) — distinct from the tour spotlight.
const FOCUS_COLOUR = "#E69F00";

// Operator-toggleable operational layers, surfaced in the floating Layers
// control (GIS-5). The tour-driven `highlight` layer is intentionally omitted.
type ToggleLayerKey = "gates" | "corridor" | "trucks" | "heatmap" | "zones" | "parking";
const LAYER_DEFS: { key: ToggleLayerKey; label: string }[] = [
  { key: "gates", label: "Gates" },
  { key: "corridor", label: "NH-348 corridor" },
  { key: "trucks", label: "Trucks (1:50)" },
  { key: "heatmap", label: "Congestion heatmap" },
  { key: "zones", label: "Geofence zones" },
  { key: "parking", label: "Parking facilities" },
];

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
  /**
   * Optional live value labels per highlighted asset id (e.g. "NSICT • 60").
   * Drawn as a chip next to each highlight ring so the operator reads the exact
   * value the simulator is driving without leaving the map.
   */
  highlightLabels?: Record<string, string>;
  /**
   * A single incident location to ring + frame (the header notification drawer
   * publishes this when an operator clicks an alert, or the simulator publishes
   * the asset just changed). Drawn as a distinct amber halo + animated pulse on
   * the highlight layer; null clears it.
   */
  focusPoint?: { lat: number; lon: number } | null;
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
  highlightLabels = {},
  focusPoint = null,
  basemap = DEFAULT_BASEMAP,
  center = JNPA_CENTER,
  zoom = JNPA_ZOOM,
  onGateClick,
  onViewReady,
  className,
}: ArcgisMapProps) {
  const { t } = useTranslation();
  // Map view mode — flat 2D MapView (default, existing behaviour) vs. the 3D
  // SceneView. The toggle lives in the map toolbar and swaps ONLY the canvas;
  // every surrounding panel/filter/KPI in the host screen is untouched.
  const [mode, setMode] = useState<"2d" | "3d">("2d");
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
    pulse: GraphicsLayer;
  } | null>(null);
  const clickHandle = useRef<ViewHandle | null>(null);
  // Snapped road geometry for the corridor (OSRM, render-time only). Null until
  // the route resolves, or permanently if OSRM is unreachable — callers then
  // fall back to the authored straight-line geometry.
  const [roadIndex, setRoadIndex] = useState<PathIndex | null>(null);
  // Floating Layers control (GIS-5): hidden by default, opens on the icon.
  const [layersOpen, setLayersOpen] = useState(false);
  const [layerVis, setLayerVis] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(LAYER_DEFS.map((d) => [d.key, true])),
  );
  const layersCtrlRef = useRef<HTMLDivElement>(null);
  useClickOutside(layersCtrlRef, () => setLayersOpen(false), layersOpen);
  // Last spotlight id-set we framed, so we only re-zoom when it changes — exactly
  // the reference PortMap's lastZoomKey guard.
  const lastZoomKey = useRef<string>("");
  // requestAnimationFrame id for the focus-marker pulse, so it can be cancelled.
  const pulseRaf = useRef<number | null>(null);
  const onGateClickRef = useRef(onGateClick);
  onGateClickRef.current = onGateClick;

  // ---- create the layer set once the view is ready ----------------------
  const handleReady = useCallback(
    (event: { target: { view: MapView; addLayers?: unknown } }) => {
      const view = event.target.view;
      if (!view || !view.map) return;
      viewRef.current = view;

      // GIS-5 UX: continuous (non-stepped) wheel/pinch zoom for a smooth feel.
      if (view.constraints) view.constraints.snapToZoom = false;

      // Ensure the native zoom (+/−) and attribution widgets are present at the
      // top-left corner (canonical ArcGIS default UI). Idempotent — keeps exactly
      // one zoom control regardless of the map-component default UI set.
      view.ui.components = ["zoom", "attribution"];

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
        // Animated focus-pulse ring on its own layer (never cleared by the
        // spotlight redraw), drawn topmost.
        pulse: mk("uc3-pulse"),
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
        set.pulse,
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
      // Draw the actual road polyline for this segment when the snapped route is
      // available (its real on-road vertices between the segment's endpoints);
      // otherwise fall back to the authored straight line.
      const path = roadIndex
        ? sliceBetween(roadIndex, seg.start as LngLat, seg.end as LngLat)
        : [seg.start, seg.end];
      const geometry = new Polyline({
        paths: [path],
        spatialReference: WGS84,
      });
      // Layered road ribbon (bottom → top): a soft wide shoulder for a graduated
      // edge, then a dark casing, then the coloured jam line. Both understrokes
      // are purely cosmetic (no popup, no hit id), so clicks / hit-testing still
      // resolve to the coloured segment line drawn on top of them.
      layer.add(
        new Graphic({
          geometry,
          symbol: new SimpleLineSymbol({
            color: CORRIDOR_SHOULDER,
            width: 13,
            cap: "round",
            join: "round",
          }),
          attributes: { id: seg.id, shoulder: true },
        }),
      );
      layer.add(
        new Graphic({
          geometry,
          symbol: new SimpleLineSymbol({
            color: MAP_TOKENS.corridorHalo,
            width: 9,
            cap: "round",
            join: "round",
          }),
          attributes: { id: seg.id, casing: true },
        }),
      );
      layer.add(
        new Graphic({
          geometry,
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
      const midRaw: [number, number] = [
        (seg.start[0] + seg.end[0]) / 2,
        (seg.start[1] + seg.end[1]) / 2,
      ];
      // Keep the congestion halo on the road when the snapped route is available.
      const mid = roadIndex ? projectOnPath(roadIndex, midRaw as LngLat).point : midRaw;
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
            // Compact congestion cue (≈8–16px) so it reads as a marker on the
            // road rather than a halo that obscures the line geometry.
            size: 8 + ratio * 8,
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
            color: hexToRgba(fill, 0.2),
            outline: new SimpleLineSymbol({
              color: fill,
              width: 2.25,
              cap: "round",
              join: "round",
            }),
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
      // Keep every vehicle on the road network: snap its reported position onto
      // the snapped corridor polyline (falls back to the raw point off-route).
      const raw: LngLat = [t.position.lon, t.position.lat];
      const [lon, lat] = roadIndex ? projectOnPath(roadIndex, raw).point : raw;
      layer.add(
        new Graphic({
          geometry: new Point({
            longitude: lon,
            latitude: lat,
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
      // Live value chip (e.g. "NSICT • 60") pinned just above the ring so the
      // operator reads the exact simulated value on the map.
      const label = highlightLabels[id];
      if (label) {
        layer.add(
          new Graphic({
            geometry: geom,
            symbol: new TextSymbol({
              text: label,
              color: "#ffffff",
              haloColor: "#0b1f33",
              haloSize: 1.5,
              font: { size: 11, weight: "bold" },
              yoffset: 22,
            }),
            attributes: { id, label: true },
          }),
        );
      }
    }
    // Alert-focus halo (from the header notification drawer). Drawn after the
    // tour spotlights and in a distinct colour so the two never read as one.
    if (focusPoint && typeof focusPoint.lon === "number" && typeof focusPoint.lat === "number") {
      const geom = new Point({
        longitude: focusPoint.lon,
        latitude: focusPoint.lat,
        spatialReference: WGS84,
      });
      layer.add(
        new Graphic({
          geometry: geom,
          symbol: new SimpleMarkerSymbol({
            style: "circle",
            color: [0, 0, 0, 0],
            size: 46,
            outline: { color: hexToRgba(FOCUS_COLOUR, 0.45), width: 2 },
          }),
          attributes: { focus: true },
        }),
      );
      layer.add(
        new Graphic({
          geometry: geom,
          symbol: new SimpleMarkerSymbol({
            style: "circle",
            color: [0, 0, 0, 0],
            size: 30,
            outline: { color: FOCUS_COLOUR, width: 4 },
          }),
          attributes: { focus: true },
        }),
      );
    }
  }

  // Snap the corridor waypoints to the road network (render-time only, GIS-1).
  // Re-runs whenever the corridor changes; aborts the in-flight request on
  // change/unmount. On failure roadIndex stays null → straight-line fallback.
  useEffect(() => {
    if (!corridor?.polyline?.length) {
      setRoadIndex(null);
      return;
    }
    const ctrl = new AbortController();
    void snapPathToRoads(corridor.polyline as LngLat[], ctrl.signal).then((road) => {
      if (road) setRoadIndex(buildPathIndex(road));
    });
    return () => ctrl.abort();
  }, [corridor]);

  // ---- reactive prop → layer updates ------------------------------------
  useEffect(() => {
    renderCorridor();
    renderHeatmap();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [corridor, snapshots, roadIndex]);

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
  }, [highlights, highlightLabels, gates, corridor, viewReady, focusPoint]);

  // Pulsing ring on the focused queue/feed item (spec: "marker pulse
  // animation"). On its own layer so the spotlight redraw never clears it; the
  // graphic geometry is the focus point, so it tracks the map during pan/zoom.
  useEffect(() => {
    const layer = layers.current?.pulse;
    if (!layer) return;
    if (pulseRaf.current != null) {
      cancelAnimationFrame(pulseRaf.current);
      pulseRaf.current = null;
    }
    layer.removeAll();
    if (!focusPoint || typeof focusPoint.lon !== "number" || typeof focusPoint.lat !== "number") {
      return;
    }
    const graphic = new Graphic({
      geometry: new Point({
        longitude: focusPoint.lon,
        latitude: focusPoint.lat,
        spatialReference: WGS84,
      }),
      attributes: { pulse: true },
    });
    layer.add(graphic);
    const PERIOD = 1400; // ms per pulse cycle
    let start: number | null = null;
    const tick = (ts: number) => {
      if (start == null) start = ts;
      const phase = ((ts - start) % PERIOD) / PERIOD; // 0 → 1
      graphic.symbol = new SimpleMarkerSymbol({
        style: "circle",
        color: [0, 0, 0, 0],
        size: 26 + phase * 36, // expands outward
        outline: { color: hexToRgba(FOCUS_COLOUR, 0.85 * (1 - phase)), width: 3 }, // fades as it grows
      });
      pulseRaf.current = requestAnimationFrame(tick);
    };
    pulseRaf.current = requestAnimationFrame(tick);
    return () => {
      if (pulseRaf.current != null) cancelAnimationFrame(pulseRaf.current);
      pulseRaf.current = null;
      layer.removeAll();
    };
  }, [focusPoint]);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => renderZones(), [zones]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => renderGates(), [gates]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => renderTrucks(), [trucks, roadIndex]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => renderParking(), [parkingFacilities]);

  // Cleanup the click handler on unmount.
  useEffect(() => {
    return () => {
      clickHandle.current?.remove();
      clickHandle.current = null;
    };
  }, []);

  // Toggle a GraphicsLayer's visibility from the floating Layers control,
  // preserving the exact show/hide capability the old layer-list provided.
  function toggleLayer(key: ToggleLayerKey) {
    const set = layers.current;
    if (!set) return;
    const next = !(layerVis[key] ?? true);
    set[key].visible = next;
    setLayerVis((v) => ({ ...v, [key]: next }));
  }

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
      {/* 2D canvas — the existing MapView. Kept mounted only in 2D so a single
          heavy ArcGIS view exists at a time. */}
      {mode === "2d" && (
        <ArcgisMapWC
          basemap={basemap}
          // The typed `center` prop is a Point (the element's getter type), so we
          // build one from the [lon, lat] tuple rather than passing the array.
          center={initialCenter}
          zoom={zoom}
          onArcgisViewReadyChange={handleReady}
          style={{ height: "100%", width: "100%" }}
        />
      )}

      {/* 3D canvas — the SceneView, fed the SAME live-data props. Replaces only
          the map canvas; the host screen's panels/KPIs are unchanged. */}
      {mode === "3d" && (
        <Scene3D
          corridor={corridor}
          gates={gates}
          zones={zones}
          snapshots={snapshots}
          trucks={trucks}
          parkingFacilities={parkingFacilities}
          highlights={highlights}
          highlightLabels={highlightLabels}
          focusPoint={focusPoint}
          basemap={basemap}
          center={center}
          zoom={zoom}
          onGateClick={onGateClick}
        />
      )}

      {/* 2D / 3D toggle — the Google-Maps-style view switcher in the map toolbar
          (top-right). Swaps only the canvas above; the surrounding screen and its
          data source are unchanged. */}
      <div className="absolute right-[15px] top-[15px] z-10 flex overflow-hidden rounded-md border border-border bg-card/90 shadow-md backdrop-blur">
        {(["2d", "3d"] as const).map((m) => (
          <button
            key={m}
            type="button"
            onClick={() => setMode(m)}
            aria-pressed={mode === m}
            title={m === "2d" ? t("map.view2d", "2D map") : t("map.view3d", "3D scene")}
            className={
              "px-3 py-1.5 text-xs font-semibold uppercase tracking-wide transition " +
              (mode === m ? "bg-severity-info text-white" : "text-foreground hover:bg-muted")
            }
          >
            {m === "2d" ? "2D" : "3D"}
          </button>
        ))}
      </div>

      {/* Floating Layers control (GIS-5): 2D only (it toggles 2D GraphicsLayers).
          Hidden by default, opens on the icon, closes on the icon again or an
          outside click. Positioned just BELOW the native zoom (+/−) widget. */}
      {mode === "2d" && (
        <div ref={layersCtrlRef} className="absolute left-[15px] top-[92px] z-10">
          <button
            type="button"
            onClick={() => setLayersOpen((o) => !o)}
            aria-label={t("map.toggleLayers")}
            aria-expanded={layersOpen}
            title={t("map.layersTitle")}
            className="flex h-9 w-9 items-center justify-center rounded-md border border-border bg-card/90 text-foreground shadow-md backdrop-blur transition hover:bg-muted"
          >
            <LayersIcon className="h-4 w-4" />
          </button>
          {layersOpen && (
            <div className="mt-2 w-52 rounded-md border border-border bg-card/95 p-2 shadow-lg backdrop-blur">
              <div className="mb-1 flex items-center justify-between px-1">
                <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  {t("map.layersTitle")}
                </span>
                <button
                  type="button"
                  onClick={() => setLayersOpen(false)}
                  aria-label={t("map.closeLayers")}
                  className="text-muted-foreground transition hover:text-foreground"
                >
                  <XIcon className="h-3.5 w-3.5" />
                </button>
              </div>
              {LAYER_DEFS.map((d) => (
                <label
                  key={d.key}
                  className="flex cursor-pointer items-center gap-2 rounded px-1 py-1 text-xs hover:bg-muted"
                >
                  <input
                    type="checkbox"
                    checked={layerVis[d.key] ?? true}
                    onChange={() => toggleLayer(d.key)}
                    className="h-3.5 w-3.5 accent-severity-info"
                  />
                  {t(`map.layer.${d.key}`)}
                </label>
              ))}
            </div>
          )}
        </div>
      )}
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
