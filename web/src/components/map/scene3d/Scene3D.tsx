// Scene3D — the 3D (WebGL SceneView) counterpart of the 2D ArcgisMap. It is fed
// the EXACT SAME live UC3 data props the 2D map receives (corridor, gates, zones,
// snapshots, trucks, parkingFacilities, highlights, focusPoint) and projects each
// into a georeferenced 3D layer, so switching 2D↔3D is a pure view change over one
// data source — no simulator/mock/sample assets.
//
// The 3D engine (ArcGIS SceneView + glTF object symbols + polygon extrusion +
// day/dusk sun lighting + offline basemap fallback + in-place FeatureLayer diff)
// is reused from the jnpa_poc_2 reference. Only the DATA MAPPING is UC3-specific:
//   corridor → per-segment 3D ribbons coloured by jam factor
//   heatmap  → congestion columns that rise with jam
//   zones    → extruded geofence prisms
//   parking  → status-coloured blocks whose height tracks occupancy
//   gates    → boom-barrier kiosks coloured by throughput utilisation
//   trucks   → live vehicle glTF models facing their reported heading
//   highlight/focus → translucent spotlight beams (tour + alert focus)
//
// Colours come exclusively from src/lib/tokens.ts (single source of truth).
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import EsriMap from "@arcgis/core/Map";
import SceneView from "@arcgis/core/views/SceneView";
import GraphicsLayer from "@arcgis/core/layers/GraphicsLayer";
import FeatureLayer from "@arcgis/core/layers/FeatureLayer";
import Graphic from "@arcgis/core/Graphic";
import Point from "@arcgis/core/geometry/Point";
import Polyline from "@arcgis/core/geometry/Polyline";
import Polygon from "@arcgis/core/geometry/Polygon";
import { Sun, Moon } from "lucide-react";

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
import { installBasemapFallback, isOfflineRequested, initialBasemap } from "./basemapFallback";
import { applyGraphics, stableOid } from "./sceneUtils";

const MODELS = "/models";
const WGS84 = { wkid: 4326 } as const;
// Truck glTF models point along +Y; offset the compass heading so the model nose
// faces its travel direction (matches the reference truck orientation).
const TRUCK_MODEL_OFFSET = 180;
// Cap the number of glTF trucks drawn at once so a large live fleet stays smooth
// (GPU-instanced object symbols, but still bounded). Excess is sampled out.
const MAX_TRUCKS_3D = 400;
const HIGHLIGHT_COLOUR = "#56B4E9";
const FOCUS_COLOUR = "#E69F00";

/** Scene3D consumes the same live-data subset the 2D ArcgisMap does. */
export interface Scene3DProps {
  corridor?: CorridorGeometry;
  gates?: Gate[];
  zones?: Zone[];
  snapshots?: TrafficSnapshot[];
  trucks?: TruckDevice[];
  parkingFacilities?: ParkingFacility[];
  highlights?: string[];
  highlightLabels?: Record<string, string>;
  focusPoint?: { lat: number; lon: number } | null;
  basemap?: string;
  center?: [number, number];
  zoom?: number;
  onGateClick?: (gateId: string) => void;
  className?: string;
}

type LayerSet = {
  corridor: GraphicsLayer;
  heatmap: GraphicsLayer;
  zones: GraphicsLayer;
  parking: GraphicsLayer;
  gates: GraphicsLayer;
  highlight: GraphicsLayer;
};

export function Scene3D({
  corridor,
  gates = [],
  zones = [],
  snapshots = [],
  trucks = [],
  parkingFacilities = [],
  highlights = [],
  highlightLabels = {},
  focusPoint = null,
  basemap,
  center = JNPA_CENTER,
  zoom = JNPA_ZOOM,
  onGateClick,
  className,
}: Scene3DProps) {
  const { t } = useTranslation();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<SceneView | null>(null);
  const layersRef = useRef<LayerSet | null>(null);
  const trucksRef = useRef<FeatureLayer | null>(null);
  const [lighting, setLighting] = useState<"day" | "dusk">("day");
  const lastZoomKey = useRef<string>("");
  const framedOnce = useRef(false);
  // Latest props readable inside the once-registered handlers.
  const propsRef = useRef({ corridor, gates, snapshots, parkingFacilities });
  propsRef.current = { corridor, gates, snapshots, parkingFacilities };
  const onGateClickRef = useRef(onGateClick);
  onGateClickRef.current = onGateClick;

  // ---- init the SceneView + layers once ----------------------------------
  useEffect(() => {
    if (!containerRef.current) return;
    const offline = isOfflineRequested();
    const map = new EsriMap({
      basemap: basemap ?? initialBasemap("dark-gray-vector"),
      ...(offline ? {} : { ground: "world-elevation" }),
    });

    const mk = (id: string, elevation: "on-the-ground" | "relative-to-ground" = "on-the-ground") =>
      new GraphicsLayer({ id, title: id, elevationInfo: { mode: elevation } });
    const set: LayerSet = {
      heatmap: mk("uc3-3d-heatmap", "relative-to-ground"),
      corridor: mk("uc3-3d-corridor"),
      zones: mk("uc3-3d-zones"),
      parking: mk("uc3-3d-parking"),
      gates: mk("uc3-3d-gates"),
      highlight: mk("uc3-3d-highlight", "relative-to-ground"),
    };
    layersRef.current = set;

    const truckLayer = makeTruckLayer([]);
    trucksRef.current = truckLayer;

    map.addMany([
      set.heatmap,
      set.corridor,
      set.zones,
      set.parking,
      truckLayer,
      set.gates,
      set.highlight,
    ]);

    const view = new SceneView({
      container: containerRef.current,
      map,
      camera: { position: { longitude: center[0], latitude: center[1] - 0.14, z: 9000 }, tilt: 62, heading: 0 },
      qualityProfile: "high",
      environment: {
        atmosphereEnabled: true,
        lighting: { type: "sun", date: new Date("2026-06-16T06:30:00Z"), directShadowsEnabled: true },
      } as never,
      ui: { components: ["zoom", "compass", "navigation-toggle", "attribution"] },
    });
    viewRef.current = view;

    const teardownFallback = installBasemapFallback(view);

    // Frame the corridor once the view is ready + data is present.
    view.when(() => {
      renderAll();
      frameToData();
    });

    // Click → gate hit-test → callback.
    const clickHandle = view.on("click", (event) => {
      void view.hitTest(event).then((res) => {
        const g = res.results.find((r) => "graphic" in r)?.graphic as
          | { attributes?: Record<string, unknown> }
          | undefined;
        const id = g?.attributes?.gateId as string | undefined;
        if (id) onGateClickRef.current?.(id);
      });
    });

    return () => {
      teardownFallback();
      clickHandle.remove();
      view.destroy();
      viewRef.current = null;
      layersRef.current = null;
      trucksRef.current = null;
      framedOnce.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- frame the camera on the corridor/gates the first time data lands ----
  function frameToData() {
    const view = viewRef.current;
    if (!view || framedOnce.current) return;
    const p = propsRef.current;
    // Prefer framing the operational asset cluster (gates + parking) so the glTF
    // vehicles/kiosks/blocks read at a human scale; only fall back to the full
    // ~20 km corridor extent when no point assets are present yet.
    const pts: [number, number][] = [];
    for (const g of p.gates) pts.push([g.lon, g.lat]);
    for (const pf of p.parkingFacilities ?? []) pts.push([pf.lon, pf.lat]);
    if (pts.length < 2 && p.corridor?.polyline?.length) {
      pts.push(...(p.corridor.polyline as [number, number][]));
    }
    if (pts.length < 2) return;
    framedOnce.current = true;
    const target = new Polyline({ paths: [pts], spatialReference: WGS84 });
    void view.goTo({ target, tilt: 62 } as never, { duration: 900, easing: "ease-in-out" }).catch(() => {});
  }

  // ---- render helpers (rebuild the GraphicsLayers from live props) ---------
  function renderAll() {
    renderCorridor();
    renderHeatmap();
    renderZones();
    renderParking();
    renderGates();
    renderTrucks();
    renderHighlight();
  }

  function renderCorridor() {
    const layer = layersRef.current?.corridor;
    if (!layer) return;
    layer.removeAll();
    if (!corridor) return;
    const jamBySeg = new Map(snapshots.map((s) => [s.segment_id, s.jam_factor]));
    for (const seg of corridor.segments) {
      const jf = jamBySeg.get(seg.id) ?? 0;
      layer.add(
        new Graphic({
          geometry: new Polyline({ paths: [[seg.start, seg.end]], spatialReference: WGS84 }),
          symbol: {
            type: "line-3d",
            symbolLayers: [
              { type: "line", size: 6, material: { color: jamColour(jf) }, cap: "round", join: "round" },
            ],
          } as never,
          attributes: { segId: seg.id },
          popupTemplate: {
            title: `Segment ${seg.id}`,
            content: `Jam factor: ${jf} · ${seg.length_km.toFixed(2)} km`,
          },
        }),
      );
    }
  }

  function renderHeatmap() {
    const layer = layersRef.current?.heatmap;
    if (!layer) return;
    layer.removeAll();
    if (!corridor) return;
    const jamBySeg = new Map(snapshots.map((s) => [s.segment_id, s.jam_factor]));
    for (const seg of corridor.segments) {
      const jfRaw = jamBySeg.get(seg.id) ?? 0;
      const ratio = jfRaw > 1 ? jfRaw / 10 : jfRaw;
      if (ratio <= 0) continue;
      const mid: [number, number] = [(seg.start[0] + seg.end[0]) / 2, (seg.start[1] + seg.end[1]) / 2];
      const stop =
        MAP_TOKENS.heatStops.find((s) => ratio <= s.ratio) ??
        MAP_TOKENS.heatStops[MAP_TOKENS.heatStops.length - 1];
      layer.add(
        new Graphic({
          geometry: new Point({ longitude: mid[0], latitude: mid[1], z: 0, spatialReference: WGS84 }),
          symbol: {
            type: "point-3d",
            symbolLayers: [
              {
                type: "object",
                resource: { primitive: "cylinder" },
                width: 120,
                depth: 120,
                height: 30 + ratio * 260, // taller column = worse congestion
                material: { color: rgba(stop.color, 0.55) },
                anchor: "bottom",
              },
            ],
          } as never,
          attributes: { segId: seg.id },
        }),
      );
    }
  }

  function renderZones() {
    const layer = layersRef.current?.zones;
    if (!layer) return;
    layer.removeAll();
    for (const z of zones) {
      if (!z.polygon || z.polygon.length < 3) continue;
      const fill = zoneColour(z.kind);
      layer.add(
        new Graphic({
          geometry: new Polygon({ rings: [closeRing(z.polygon)], spatialReference: WGS84 }),
          symbol: {
            type: "polygon-3d",
            symbolLayers: [
              {
                type: "extrude",
                size: 18,
                material: { color: rgba(fill, 0.28) },
                edges: { type: "solid", color: fill, size: 1 },
              },
            ],
          } as never,
          attributes: { id: z.id, name: z.name, kind: z.kind },
          popupTemplate: { title: z.name, content: `Zone kind: ${z.kind}` },
        }),
      );
    }
  }

  function renderParking() {
    const layer = layersRef.current?.parking;
    if (!layer) return;
    layer.removeAll();
    for (const p of parkingFacilities) {
      const pct = typeof p.utilisation_pct === "number" ? p.utilisation_pct : 0;
      layer.add(
        new Graphic({
          geometry: new Point({ longitude: p.lon, latitude: p.lat, spatialReference: WGS84 }),
          symbol: {
            type: "point-3d",
            symbolLayers: [
              {
                type: "object",
                resource: { primitive: "cube" },
                width: 70,
                depth: 70,
                height: 12 + (pct / 100) * 60, // taller block = fuller lot
                material: { color: parkingStatusColour(p.status) },
                anchor: "bottom",
              },
            ],
          } as never,
          attributes: { id: p.facility_id, name: p.name },
          popupTemplate: {
            title: p.name,
            content: `Occupied ${p.occupied}/${p.capacity} · ${p.status}`,
          },
        }),
      );
    }
  }

  function renderGates() {
    const layer = layersRef.current?.gates;
    if (!layer) return;
    layer.removeAll();
    for (const g of gates) {
      layer.add(
        new Graphic({
          geometry: new Point({ longitude: g.lon, latitude: g.lat, spatialReference: WGS84 }),
          symbol: {
            type: "point-3d",
            symbolLayers: [
              // Red boom barrier across the lane — the recognisable checkpoint.
              { type: "object", resource: { href: `${MODELS}/gate-boom.glb` }, height: 9, anchor: "bottom" },
              // Canopy roof slab coloured by live throughput utilisation.
              {
                type: "object",
                resource: { primitive: "cube" },
                width: 30,
                depth: 12,
                height: 2.5,
                material: { color: gateColour(g.utilisation) },
                anchor: "bottom",
              },
            ],
          } as never,
          attributes: { gateId: g.id, name: g.name },
          popupTemplate: {
            title: g.name || g.id,
            content: `Throughput ${g.throughput_60min}/${g.target_vph} vph`,
          },
        }),
      );
    }
  }

  // Trucks live on a FeatureLayer (GPU-instanced object symbols + rotation
  // visual variable) so a large live fleet updates in place without blinking.
  function renderTrucks() {
    const layer = trucksRef.current;
    if (!layer) return;
    void applyGraphics(layer, truckGraphics(trucks));
  }

  // Resolve a spotlight asset id (gate or corridor segment) to its ground point.
  function geomFor(id: string): [number, number] | null {
    const g = gates.find((x) => x.id === id);
    if (g) return [g.lon, g.lat];
    const seg = corridor?.segments.find((s) => s.id === id);
    if (seg) {
      const a = (seg.start[0] + seg.end[0]) / 2;
      const b = (seg.start[1] + seg.end[1]) / 2;
      const lat = Math.abs(a) <= 30 ? a : b;
      const lon = Math.abs(a) <= 30 ? b : a;
      return [lon, lat];
    }
    return null;
  }

  function beam(lon: number, lat: number, color: string, alpha: number): Graphic {
    return new Graphic({
      geometry: new Point({ longitude: lon, latitude: lat, z: 0, spatialReference: WGS84 }),
      symbol: {
        type: "point-3d",
        symbolLayers: [
          {
            type: "object",
            resource: { primitive: "cylinder" },
            width: 240,
            depth: 240,
            height: 420,
            material: { color: rgba(color, alpha) },
            anchor: "bottom",
          },
        ],
      } as never,
      attributes: { beam: true },
    });
  }

  function renderHighlight() {
    const layer = layersRef.current?.highlight;
    if (!layer) return;
    layer.removeAll();
    for (const id of highlights) {
      const p = geomFor(id);
      if (!p) continue;
      layer.add(beam(p[0], p[1], HIGHLIGHT_COLOUR, 0.16));
      const label = highlightLabels[id];
      layer.add(
        new Graphic({
          geometry: new Point({ longitude: p[0], latitude: p[1], z: 460, spatialReference: WGS84 }),
          symbol: {
            type: "point-3d",
            symbolLayers: [
              {
                type: "text",
                text: label ?? id,
                material: { color: "#ffffff" },
                halo: { color: [11, 31, 51, 1], size: 1.5 },
                size: 12,
                font: { weight: "bold" },
              },
            ],
          } as never,
          attributes: { labelFor: id },
        }),
      );
    }
    if (focusPoint && typeof focusPoint.lon === "number" && typeof focusPoint.lat === "number") {
      layer.add(beam(focusPoint.lon, focusPoint.lat, FOCUS_COLOUR, 0.22));
    }
  }

  // ---- reactive prop → layer updates -------------------------------------
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { renderCorridor(); renderHeatmap(); frameToData(); }, [corridor, snapshots]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => renderZones(), [zones]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { renderParking(); frameToData(); }, [parkingFacilities]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { renderGates(); frameToData(); }, [gates]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => renderTrucks(), [trucks]);

  // Spotlight sync — redraw beams, then frame the assets when the id-set changes.
  useEffect(() => {
    renderHighlight();
    const view = viewRef.current;
    const targets = highlights
      .map(geomFor)
      .filter((p): p is [number, number] => p != null)
      .map((p) => new Point({ longitude: p[0], latitude: p[1], spatialReference: WGS84 }));
    const zoomKey = [...highlights].sort().join("|");
    if (view && targets.length > 0 && zoomKey !== lastZoomKey.current) {
      lastZoomKey.current = zoomKey;
      void view.when(() => {
        void view
          .goTo({ target: targets, tilt: 60 } as never, { duration: 700, easing: "ease-in-out" })
          .catch(() => {});
      });
    } else if (targets.length === 0) {
      lastZoomKey.current = "";
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [highlights, highlightLabels, gates, corridor, focusPoint]);

  // Day / dusk lighting toggle — repositions the sun (deterministic dates).
  function toggleLighting() {
    const view = viewRef.current;
    const next = lighting === "day" ? "dusk" : "day";
    setLighting(next);
    if (!view) return;
    const env = view.environment as unknown as { lighting?: { type?: string; date?: Date; directShadowsEnabled?: boolean } };
    if (env.lighting) {
      env.lighting.type = "sun";
      env.lighting.date =
        next === "dusk" ? new Date("2026-06-16T12:45:00Z") : new Date("2026-06-16T06:30:00Z");
      env.lighting.directShadowsEnabled = true;
    }
  }

  void zoom; // camera framing is data-driven; zoom prop kept for API parity with 2D.

  return (
    <div
      className={className ?? "h-full w-full"}
      style={{ position: "relative" }}
      data-testid="live-map-3d"
    >
      <div ref={containerRef} style={{ height: "100%", width: "100%" }} role="application" aria-label="JNPA 3D scene" />
      {/* Day / dusk lighting toggle, mirroring the reference cinematic control. */}
      <button
        type="button"
        onClick={toggleLighting}
        title={lighting === "day" ? t("map.lightingDusk", "Dusk lighting") : t("map.lightingDay", "Day lighting")}
        className="absolute right-[15px] top-[15px] z-10 flex h-9 w-9 items-center justify-center rounded-md border border-border bg-card/90 text-foreground shadow-md backdrop-blur transition hover:bg-muted"
      >
        {lighting === "day" ? <Moon className="h-4 w-4" /> : <Sun className="h-4 w-4" />}
      </button>
    </div>
  );
}

export default Scene3D;

// ---- truck FeatureLayer (GPU-instanced glTF vehicles) --------------------
function truckGraphics(trucks: TruckDevice[]): Graphic[] {
  const capped = trucks.length > MAX_TRUCKS_3D ? sample(trucks, MAX_TRUCKS_3D) : trucks;
  const out: Graphic[] = [];
  for (const tk of capped) {
    if (typeof tk.position?.lon !== "number" || typeof tk.position?.lat !== "number") continue;
    const heading = typeof tk.heading === "number" ? tk.heading : 0;
    out.push(
      new Graphic({
        geometry: new Point({ longitude: tk.position.lon, latitude: tk.position.lat, spatialReference: WGS84 }),
        attributes: {
          objectId: stableOid(tk.device_id),
          rot: (heading + TRUCK_MODEL_OFFSET) % 360,
          model: (tk.device_id.charCodeAt(tk.device_id.length - 1) % 3 === 0) ? "pickup-realistic" : "truck-realistic",
        },
      }),
    );
  }
  return out;
}

function makeTruckLayer(initial: Graphic[]): FeatureLayer {
  return new FeatureLayer({
    id: "uc3-3d-trucks",
    title: "3D · Trucks (live)",
    source: initial as unknown as Graphic[],
    objectIdField: "objectId",
    geometryType: "point",
    spatialReference: WGS84,
    fields: [
      { name: "objectId", type: "oid" },
      { name: "rot", type: "double" },
      { name: "model", type: "string" },
    ],
    elevationInfo: { mode: "on-the-ground" },
    renderer: {
      type: "unique-value",
      field: "model",
      uniqueValueInfos: [
        { model: "truck-realistic", h: 8 },
        { model: "pickup-realistic", h: 5 },
      ].map(({ model, h }) => ({
        value: model,
        symbol: {
          type: "point-3d",
          symbolLayers: [
            { type: "object", resource: { href: `${MODELS}/${model}.glb` }, height: h, anchor: "bottom" },
          ],
        },
      })),
      visualVariables: [{ type: "rotation", field: "rot", rotationType: "geographic" }],
    } as never,
    popupTemplate: { title: "Truck", content: "Live vehicle position." } as never,
  });
}

// ---- small helpers -------------------------------------------------------
function sample<T>(arr: T[], n: number): T[] {
  const step = arr.length / n;
  const out: T[] = [];
  for (let i = 0; i < n; i++) out.push(arr[Math.floor(i * step)]!);
  return out;
}

function closeRing(ring: [number, number][]): [number, number][] {
  if (ring.length && (ring[0][0] !== ring[ring.length - 1][0] || ring[0][1] !== ring[ring.length - 1][1])) {
    return [...ring, ring[0]];
  }
  return ring;
}

/** hex "#RRGGBB" (or an "rgba(...)"/rgba tuple string) + alpha → [r,g,b,a]. */
function rgba(color: string, alpha: number): [number, number, number, number] {
  if (color.startsWith("rgba") || color.startsWith("rgb")) {
    const nums = color.replace(/[^0-9.,]/g, "").split(",").map(Number);
    return [nums[0] ?? 0, nums[1] ?? 0, nums[2] ?? 0, alpha];
  }
  const h = color.replace("#", "");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16), alpha];
}
