import { useEffect, useRef } from "react";
import maplibregl, { Map as MlMap, type GeoJSONSource } from "maplibre-gl";
import { mapStyle, JNPA_CENTER, JNPA_ZOOM } from "@/lib/basemap";
import type {
  CorridorGeometry,
  Gate,
  TrafficSnapshot,
  Zone,
} from "@/lib/types";
import { gateColour, jamColour } from "@/lib/palette";

// Live truck dot with a fading trail. Positions stream over the WS (sampled
// 1:50 by the gateway). We keep a short history per device and expire any track
// not seen for TRAIL_MS so the trails fade over ~5 min as the spec asks.
const TRAIL_MS = 5 * 60 * 1000;

interface TruckTrack {
  id: string;
  points: { lon: number; lat: number; t: number }[];
}

export interface LiveMapHandle {
  panTo: (lon: number, lat: number) => void;
}

interface Props {
  corridor?: CorridorGeometry;
  gates: Gate[];
  snapshots: TrafficSnapshot[];
  zones?: Zone[];
  /** Register a position update callback; returns an unsubscribe. */
  onReady?: (push: (deviceId: string, lon: number, lat: number) => void, map: MlMap) => void;
  onGateClick?: (gateId: string) => void;
}

export function LiveMap({ corridor, gates, snapshots, zones, onReady, onGateClick }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MlMap | null>(null);
  const tracks = useRef<Map<string, TruckTrack>>(new Map());
  const loaded = useRef(false);
  const resizeObs = useRef<ResizeObserver | null>(null);

  // ---- init map once ----
  useEffect(() => {
    if (!ref.current || mapRef.current) return;
    const map = new maplibregl.Map({
      container: ref.current,
      style: mapStyle(),
      center: JNPA_CENTER,
      zoom: JNPA_ZOOM,
      attributionControl: { compact: true },
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    mapRef.current = map;

    // The map often initialises before the flex layout has given the container
    // its final size, so MapLibre computes a 0-height viewport and paints no
    // tiles (style loads — hence attribution — but the canvas stays blank).
    // Observe the container and resize the map whenever its box changes.
    const ro = new ResizeObserver(() => mapRef.current?.resize());
    ro.observe(ref.current);
    resizeObs.current = ro;

    map.on("load", () => {
      loaded.current = true;
      // Force one resize after load in case the observer fired before init.
      map.resize();

      // Empty GeoJSON sources we update reactively below.
      const empty = { type: "FeatureCollection", features: [] } as GeoJSON.FeatureCollection;
      map.addSource("corridor", { type: "geojson", data: empty });
      map.addSource("heatmap", { type: "geojson", data: empty });
      map.addSource("zones", { type: "geojson", data: empty });
      map.addSource("gates", { type: "geojson", data: empty });
      map.addSource("trucks", { type: "geojson", data: empty });
      map.addSource("trails", { type: "geojson", data: empty });

      // Traffic heatmap from segment jam factors (under everything else).
      map.addLayer({
        id: "heatmap",
        type: "heatmap",
        source: "heatmap",
        paint: {
          "heatmap-weight": ["get", "weight"],
          "heatmap-intensity": 1.2,
          "heatmap-radius": 28,
          "heatmap-opacity": 0.6,
          "heatmap-color": [
            "interpolate", ["linear"], ["heatmap-density"],
            0, "rgba(0,158,115,0)",
            0.3, "#009E73",
            0.6, "#E69F00",
            1, "#D55E00",
          ],
        },
      });

      // Restricted / no-parking zones (filled polygons).
      map.addLayer({
        id: "zones-fill",
        type: "fill",
        source: "zones",
        paint: {
          "fill-color": ["match", ["get", "kind"], "restricted", "#D55E00", "#56B4E9"],
          "fill-opacity": 0.18,
        },
      });
      map.addLayer({
        id: "zones-outline",
        type: "line",
        source: "zones",
        paint: { "line-color": ["match", ["get", "kind"], "restricted", "#D55E00", "#56B4E9"], "line-width": 1.5 },
      });

      // Corridor polyline coloured by jam factor.
      map.addLayer({
        id: "corridor",
        type: "line",
        source: "corridor",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": ["get", "colour"], "line-width": 5 },
      });

      // Truck trails (fading) then live dots.
      map.addLayer({
        id: "trails",
        type: "line",
        source: "trails",
        paint: { "line-color": "#56B4E9", "line-width": 2, "line-opacity": ["get", "opacity"] },
      });
      map.addLayer({
        id: "trucks",
        type: "circle",
        source: "trucks",
        paint: {
          "circle-radius": 4,
          "circle-color": "#0072B2",
          "circle-stroke-color": "#ffffff",
          "circle-stroke-width": 1,
        },
      });

      // Gate markers coloured by throughput utilisation.
      map.addLayer({
        id: "gates",
        type: "circle",
        source: "gates",
        paint: {
          "circle-radius": 9,
          "circle-color": ["get", "colour"],
          "circle-stroke-color": "#ffffff",
          "circle-stroke-width": 2,
        },
      });
      map.addLayer({
        id: "gate-labels",
        type: "symbol",
        source: "gates",
        layout: {
          "text-field": ["get", "label"],
          "text-size": 11,
          "text-offset": [0, 1.4],
          "text-anchor": "top",
        },
        paint: { "text-color": "#1f2937", "text-halo-color": "#ffffff", "text-halo-width": 1.6 },
      });

      map.on("click", "gates", (e) => {
        const f = e.features?.[0];
        if (f && onGateClick) onGateClick(f.properties?.id as string);
      });
      map.on("mouseenter", "gates", () => (map.getCanvas().style.cursor = "pointer"));
      map.on("mouseleave", "gates", () => (map.getCanvas().style.cursor = ""));

      // Expose a push() so the parent can feed WS truck positions in.
      onReady?.((deviceId, lon, lat) => pushTruck(deviceId, lon, lat), map);
      // start the trail-decay loop
      startDecayLoop();
    });

    return () => {
      resizeObs.current?.disconnect();
      resizeObs.current = null;
      map.remove();
      mapRef.current = null;
      loaded.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- helpers operating on the live map ----
  function pushTruck(deviceId: string, lon: number, lat: number) {
    const now = Date.now();
    let trk = tracks.current.get(deviceId);
    if (!trk) {
      trk = { id: deviceId, points: [] };
      tracks.current.set(deviceId, trk);
    }
    trk.points.push({ lon, lat, t: now });
    if (trk.points.length > 60) trk.points.shift();
    renderTrucks();
  }

  function renderTrucks() {
    const map = mapRef.current;
    if (!map || !loaded.current) return;
    const now = Date.now();
    const dotFeatures: GeoJSON.Feature[] = [];
    const trailFeatures: GeoJSON.Feature[] = [];

    for (const [id, trk] of tracks.current) {
      trk.points = trk.points.filter((p) => now - p.t < TRAIL_MS);
      if (trk.points.length === 0) {
        tracks.current.delete(id);
        continue;
      }
      const head = trk.points[trk.points.length - 1];
      dotFeatures.push({
        type: "Feature",
        geometry: { type: "Point", coordinates: [head.lon, head.lat] },
        properties: { id },
      });
      if (trk.points.length >= 2) {
        const age = now - trk.points[0].t;
        const opacity = Math.max(0.05, 1 - age / TRAIL_MS);
        trailFeatures.push({
          type: "Feature",
          geometry: { type: "LineString", coordinates: trk.points.map((p) => [p.lon, p.lat]) },
          properties: { opacity },
        });
      }
    }
    (map.getSource("trucks") as GeoJSONSource)?.setData({
      type: "FeatureCollection",
      features: dotFeatures,
    });
    (map.getSource("trails") as GeoJSONSource)?.setData({
      type: "FeatureCollection",
      features: trailFeatures,
    });
  }

  function startDecayLoop() {
    const tick = () => {
      renderTrucks();
      decayTimer.current = window.setTimeout(tick, 4000);
    };
    decayTimer.current = window.setTimeout(tick, 4000);
  }
  const decayTimer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(decayTimer.current), []);

  // ---- corridor + heatmap reactive update (coloured by jam_factor) ----
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !loaded.current || !corridor) return;
    const jamBySeg = new Map(snapshots.map((s) => [s.segment_id, s.jam_factor]));

    // Build per-segment coloured line features.
    const segFeatures: GeoJSON.Feature[] = corridor.segments.map((seg) => {
      const jf = jamBySeg.get(seg.id) ?? 0;
      return {
        type: "Feature",
        geometry: { type: "LineString", coordinates: [seg.start, seg.end] },
        properties: { id: seg.id, colour: jamColour(jf), jam: jf },
      };
    });
    (map.getSource("corridor") as GeoJSONSource)?.setData({
      type: "FeatureCollection",
      features: segFeatures,
    });

    // Heatmap: a weighted point at each segment midpoint.
    const heatFeatures: GeoJSON.Feature[] = corridor.segments.map((seg) => {
      const jf = jamBySeg.get(seg.id) ?? 0;
      const w = jf > 1 ? jf / 10 : jf;
      const mid: [number, number] = [
        (seg.start[0] + seg.end[0]) / 2,
        (seg.start[1] + seg.end[1]) / 2,
      ];
      return { type: "Feature", geometry: { type: "Point", coordinates: mid }, properties: { weight: w } };
    });
    (map.getSource("heatmap") as GeoJSONSource)?.setData({
      type: "FeatureCollection",
      features: heatFeatures,
    });
  }, [corridor, snapshots]);

  // ---- gates reactive update ----
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !loaded.current) return;
    (map.getSource("gates") as GeoJSONSource)?.setData({
      type: "FeatureCollection",
      features: gates.map((g) => ({
        type: "Feature",
        geometry: { type: "Point", coordinates: [g.lon, g.lat] },
        properties: {
          id: g.id,
          colour: gateColour(g.utilisation),
          label: `${g.id.replace("G-", "")} · ${g.throughput_60min}/${g.target_vph}`,
        },
      })),
    });
  }, [gates]);

  // ---- zones reactive update ----
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !loaded.current || !zones) return;
    (map.getSource("zones") as GeoJSONSource)?.setData({
      type: "FeatureCollection",
      features: zones
        .filter((z) => z.polygon?.length >= 3)
        .map((z) => ({
          type: "Feature",
          geometry: { type: "Polygon", coordinates: [closeRing(z.polygon)] },
          properties: { id: z.id, kind: z.kind, name: z.name },
        })),
    });
  }, [zones]);

  // expose panTo via window-less ref pattern handled by parent through onReady map.
  return <div ref={ref} className="h-full w-full" data-testid="live-map" />;
}

function closeRing(ring: [number, number][]): [number, number][] {
  if (ring.length && (ring[0][0] !== ring[ring.length - 1][0] || ring[0][1] !== ring[ring.length - 1][1])) {
    return [...ring, ring[0]];
  }
  return ring;
}
