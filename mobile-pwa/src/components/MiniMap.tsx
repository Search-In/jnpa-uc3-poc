import { useEffect, useRef } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { mapStyle, roadStyle } from "@/lib/basemap";
import type { CorridorGeometry, DevicePosition, Gate } from "@/lib/types";

// One route option to draw (Google-Maps-style): a polyline in [lon,lat] pairs,
// flagged primary (highlighted) or alternate (greyed). `id` keys the feature.
export interface RouteLine {
  id: string;
  coords: [number, number][];
  primary?: boolean;
}

// "Traffic ahead" mini-map. It loads the SAME basemap as the dashboard (Carto
// Positron raster by default, token-free; Mapbox/Bhuvan optional via the shared
// VITE_* contract — see lib/basemap.ts) so the driver sees real roads, the port
// area and surrounding geography. On top of the basemap it overlays GeoJSON
// layers for the corridor polyline, the four gates, the segment "traffic ahead"
// colouring, and the live truck marker.

interface Props {
  corridor?: CorridorGeometry;
  gates?: Gate[];
  truck?: DevicePosition | null;
  targetGateId?: string | null;
  // segment_id -> jam_factor (0..1), drives the "traffic ahead" colour.
  jam?: Record<string, number>;
  // Fill the parent container (full-screen nav map) instead of the fixed 200px.
  fill?: boolean;
  // Use the Google-Maps-style road basemap instead of the default (satellite).
  roads?: boolean;
  // Multiple route options to draw (primary highlighted, alternates greyed).
  routes?: RouteLine[];
}

function jamColor(j: number): string {
  if (j >= 0.66) return "#d55e00"; // vermillion — heavy
  if (j >= 0.33) return "#e69f00"; // orange — moderate
  return "#009e73"; // green — free-flow
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
}: Props) {
  const el = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const truckMarker = useRef<maplibregl.Marker | null>(null);

  useEffect(() => {
    if (!el.current || map.current) return;
    const m = new maplibregl.Map({
      container: el.current,
      style: roads ? roadStyle() : mapStyle(),
      center: [72.952, 18.948],
      zoom: 11.5,
      // Carto/OSM tiles require attribution; use the compact "ⓘ" toggle so it
      // stays unobtrusive on the small driver map.
      attributionControl: { compact: true },
      interactive: true,
      dragRotate: false,
      pitchWithRotate: false,
    });
    map.current = m;
    m.on("load", () => m.resize());
    // Full-screen/flex containers settle their size AFTER mount, so observe the
    // container and resize the map whenever it changes (fixes a map that renders
    // at the wrong height and leaves blank space).
    const ro = new ResizeObserver(() => m.resize());
    ro.observe(el.current);
    return () => {
      ro.disconnect();
      m.remove();
      map.current = null;
    };
  }, []);

  // Draw corridor + gates + traffic colouring when geometry arrives.
  useEffect(() => {
    const m = map.current;
    if (!m || !corridor) return;
    const draw = () => {
      // Corridor base line, tinted by the worst "traffic ahead" jam factor so
      // the driver sees free-flow (green) vs heavy (vermillion) at a glance.
      const worstJam = jam ? Math.max(0, ...Object.values(jam)) : 0;
      const lineFc: GeoJSON.Feature = {
        type: "Feature",
        properties: {},
        geometry: { type: "LineString", coordinates: corridor.polyline },
      };
      if (!m.getSource("corridor")) {
        m.addSource("corridor", { type: "geojson", data: lineFc });
        m.addLayer({
          id: "corridor-line",
          type: "line",
          source: "corridor",
          paint: { "line-color": jam ? jamColor(worstJam) : "#94a3b8", "line-width": 4 },
        });
      } else {
        (m.getSource("corridor") as maplibregl.GeoJSONSource).setData(lineFc);
        if (jam) m.setPaintProperty("corridor-line", "line-color", jamColor(worstJam));
      }

      // Gates.
      if (gates && gates.length) {
        const gateFc: GeoJSON.FeatureCollection = {
          type: "FeatureCollection",
          features: gates.map((g) => ({
            type: "Feature",
            properties: { id: g.id, target: g.id === targetGateId },
            geometry: { type: "Point", coordinates: [g.lon, g.lat] },
          })),
        };
        if (!m.getSource("gates")) {
          m.addSource("gates", { type: "geojson", data: gateFc });
          m.addLayer({
            id: "gates-pt",
            type: "circle",
            source: "gates",
            paint: {
              "circle-radius": ["case", ["get", "target"], 8, 5],
              "circle-color": ["case", ["get", "target"], "#1f78c2", "#64748b"],
              "circle-stroke-color": "#ffffff",
              "circle-stroke-width": 2,
            },
          });
        } else {
          (m.getSource("gates") as maplibregl.GeoJSONSource).setData(gateFc);
        }
      }

      // Fit to the corridor once.
      try {
        const lons = corridor.polyline.map((p) => p[0]);
        const lats = corridor.polyline.map((p) => p[1]);
        m.fitBounds(
          [
            [Math.min(...lons), Math.min(...lats)],
            [Math.max(...lons), Math.max(...lats)],
          ],
          { padding: 28, animate: false, maxZoom: 13 },
        );
      } catch {
        /* degenerate geometry */
      }
    };
    if (m.isStyleLoaded()) draw();
    else m.once("load", draw);
  }, [corridor, gates, targetGateId, jam]);

  // Multiple route options (Google-Maps-style): alternates greyed, primary blue
  // on top. Fits the map to the routes when present.
  useEffect(() => {
    const m = map.current;
    if (!m || !routes || !routes.length) return;
    const draw = () => {
      // Order so the primary route draws last (on top).
      const ordered = [...routes].sort((a, b) => Number(!!a.primary) - Number(!!b.primary));
      const fc: GeoJSON.FeatureCollection = {
        type: "FeatureCollection",
        features: ordered.map((r) => ({
          type: "Feature",
          properties: { primary: !!r.primary, id: r.id },
          geometry: { type: "LineString", coordinates: r.coords },
        })),
      };
      if (!m.getSource("routes")) {
        m.addSource("routes", { type: "geojson", data: fc });
        // Casing under the line for a clean Google-Maps look.
        m.addLayer({
          id: "routes-casing",
          type: "line",
          source: "routes",
          layout: { "line-cap": "round", "line-join": "round" },
          paint: {
            "line-color": ["case", ["get", "primary"], "#1a56db", "#9aa4b2"],
            "line-width": ["case", ["get", "primary"], 9, 6],
            "line-opacity": ["case", ["get", "primary"], 0.35, 0.25],
          },
        });
        m.addLayer({
          id: "routes-line",
          type: "line",
          source: "routes",
          layout: { "line-cap": "round", "line-join": "round" },
          paint: {
            "line-color": ["case", ["get", "primary"], "#1a56db", "#7b8794"],
            "line-width": ["case", ["get", "primary"], 5, 3.5],
          },
        });
      } else {
        (m.getSource("routes") as maplibregl.GeoJSONSource).setData(fc);
      }
      // Fit to all route coordinates.
      try {
        const all = ordered.flatMap((r) => r.coords);
        const lons = all.map((p) => p[0]);
        const lats = all.map((p) => p[1]);
        m.fitBounds(
          [
            [Math.min(...lons), Math.min(...lats)],
            [Math.max(...lons), Math.max(...lats)],
          ],
          { padding: 40, animate: true, maxZoom: 13 },
        );
      } catch {
        /* degenerate */
      }
    };
    if (m.isStyleLoaded()) draw();
    else m.once("load", draw);
  }, [routes]);

  // Live truck marker.
  useEffect(() => {
    const m = map.current;
    if (!m || !truck) return;
    const lngLat: [number, number] = [truck.lon, truck.lat];
    if (!truckMarker.current) {
      const node = document.createElement("div");
      node.style.cssText =
        "width:16px;height:16px;border-radius:50%;background:#eab308;border:3px solid #ffffff;box-shadow:0 0 0 3px rgba(234,179,8,.35)";
      truckMarker.current = new maplibregl.Marker({ element: node }).setLngLat(lngLat).addTo(m);
    } else {
      truckMarker.current.setLngLat(lngLat);
    }
  }, [truck]);

  return <div className={fill ? "minimap minimap-fill" : "minimap"} ref={el} />;
}
