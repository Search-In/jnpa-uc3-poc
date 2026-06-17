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

import { useCallback, useEffect, useMemo, useRef } from "react";
// Register the <arcgis-map> custom element + bundle its runtime locally. The
// React wrapper below only creates the React→element binding; this side-effect
// import is what actually defines the element (otherwise it never upgrades).
import "@arcgis/map-components/components/arcgis-map";
import { ArcgisMap as ArcgisMapWC } from "@arcgis/map-components-react";
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
import {
  gateColour,
  jamColour,
  MAP_TOKENS,
  parkingStatusColour,
  zoneColour,
} from "@/lib/tokens";
import { JNPA_CENTER, JNPA_ZOOM } from "@/lib/basemap";

const DEFAULT_BASEMAP = "dark-gray-vector";

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
  basemap = DEFAULT_BASEMAP,
  center = JNPA_CENTER,
  zoom = JNPA_ZOOM,
  onGateClick,
  onViewReady,
  className,
}: ArcgisMapProps) {
  const viewRef = useRef<MapView | null>(null);
  const layers = useRef<{
    heatmap: GraphicsLayer;
    zones: GraphicsLayer;
    corridor: GraphicsLayer;
    parking: GraphicsLayer;
    trucks: GraphicsLayer;
    gates: GraphicsLayer;
  } | null>(null);
  const clickHandle = useRef<ViewHandle | null>(null);
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
      };
      layers.current = set;
      view.map.addMany([
        set.heatmap,
        set.zones,
        set.corridor,
        set.parking,
        set.trucks,
        set.gates,
      ]);

      // Gate click → callback.
      clickHandle.current?.remove();
      clickHandle.current = view.on("click", (e) => {
        void view.hitTest(e).then((res) => {
          const hit = res.results.find(
            (r) =>
              r.type === "graphic" &&
              r.graphic?.layer === layers.current?.gates,
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
      if (
        typeof t.position?.lon !== "number" ||
        typeof t.position?.lat !== "number"
      )
        continue;
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

  // ---- reactive prop → layer updates ------------------------------------
  useEffect(() => {
    renderCorridor();
    renderHeatmap();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [corridor, snapshots]);

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
      />
    </div>
  );
}

export default ArcgisMap;

// ---- small geometry / colour helpers ------------------------------------
function closeRing(ring: [number, number][]): [number, number][] {
  if (
    ring.length &&
    (ring[0][0] !== ring[ring.length - 1][0] ||
      ring[0][1] !== ring[ring.length - 1][1])
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
