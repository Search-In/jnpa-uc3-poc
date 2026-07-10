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
  // Compass heading in degrees (0 = N) — rotates the directional truck marker.
  heading?: number | null;
  // Destination pin (the target gate) — drawn as a Maps-style teardrop pin.
  destination?: { lat: number; lon: number; name?: string } | null;
  // Parking POIs — drawn as green "P" markers.
  parking?: { id: string; lat: number; lon: number; available?: number | null }[];
  // When set with a destination, the map opens framed on truck → destination.
  frameToTrip?: boolean;
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
  heading,
  destination,
  parking,
  frameToTrip,
}: Props) {
  const el = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const truckMarker = useRef<maplibregl.Marker | null>(null);
  const truckArrow = useRef<HTMLDivElement | null>(null);
  const destMarker = useRef<maplibregl.Marker | null>(null);
  const parkingMarkers = useRef<maplibregl.Marker[]>([]);
  const framedTrip = useRef(false);

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

  // Corridor line (tinted by the worst "traffic ahead" jam factor). Drawn on its
  // own so it can be hidden/shown WITHOUT affecting the gate markers below.
  const didCorridorFit = useRef(false);
  useEffect(() => {
    const m = map.current;
    if (!m) return;
    const draw = () => {
      if (!corridor) {
        // Corridor hidden (e.g. Navigate once a route is drawn) — clear the line.
        if (m.getLayer("corridor-line")) m.removeLayer("corridor-line");
        if (m.getSource("corridor")) m.removeSource("corridor");
        return;
      }
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
      // Fit to the corridor once (only when the caller isn't framing the trip).
      if (!frameToTrip && !didCorridorFit.current) {
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
          didCorridorFit.current = true;
        } catch {
          /* degenerate geometry */
        }
      }
    };
    if (m.isStyleLoaded()) draw();
    else m.once("load", draw);
  }, [corridor, jam, frameToTrip]);

  // Gate markers — INDEPENDENT of the corridor so they always render (this is the
  // fix for "Navigate loses all markers": gates used to be drawn inside the
  // corridor effect, which bailed out whenever `corridor` was absent).
  useEffect(() => {
    const m = map.current;
    if (!m || !gates || !gates.length) return;
    const draw = () => {
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
    };
    if (m.isStyleLoaded()) draw();
    else m.once("load", draw);
  }, [gates, targetGateId]);

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
          // Reserve room for the floating destination card (top) and the bottom
          // instruction sheet so the route line is never hidden behind them.
          { padding: { top: 96, bottom: 190, left: 40, right: 40 }, animate: true, maxZoom: 14 },
        );
      } catch {
        /* degenerate */
      }
    };
    if (m.isStyleLoaded()) draw();
    else m.once("load", draw);
  }, [routes]);

  // Live truck marker — a directional "navigation puck" (Google-Maps style) that
  // rotates to the heading and eases between positions.
  useEffect(() => {
    const m = map.current;
    if (!m || !truck) return;
    const lngLat: [number, number] = [truck.lon, truck.lat];
    if (!truckMarker.current) {
      const node = document.createElement("div");
      node.style.cssText = "width:30px;height:30px;position:relative";
      const arrow = document.createElement("div");
      arrow.style.cssText =
        "position:absolute;inset:0;display:grid;place-items:center;transition:transform .5s ease-out";
      arrow.innerHTML =
        '<svg width="30" height="30" viewBox="0 0 24 24" style="filter:drop-shadow(0 1px 2px rgba(0,0,0,.35))">' +
        '<circle cx="12" cy="12" r="10" fill="#1f78c2" stroke="#fff" stroke-width="2.5"/>' +
        '<path d="M12 6.5 15.5 15 12 13 8.5 15Z" fill="#fff"/></svg>';
      node.appendChild(arrow);
      truckArrow.current = arrow;
      truckMarker.current = new maplibregl.Marker({ element: node }).setLngLat(lngLat).addTo(m);
    } else {
      truckMarker.current.setLngLat(lngLat);
    }
    if (truckArrow.current && heading != null && Number.isFinite(heading)) {
      truckArrow.current.style.transform = `rotate(${heading}deg)`;
    }
  }, [truck, heading]);

  // Destination pin (Maps-style teardrop) at the target gate.
  useEffect(() => {
    const m = map.current;
    if (!m) return;
    if (!destination) {
      destMarker.current?.remove();
      destMarker.current = null;
      return;
    }
    const lngLat: [number, number] = [destination.lon, destination.lat];
    if (!destMarker.current) {
      const node = document.createElement("div");
      node.style.cssText = "transform:translateY(2px)";
      node.innerHTML =
        '<svg width="30" height="38" viewBox="0 0 24 30" style="filter:drop-shadow(0 2px 3px rgba(0,0,0,.4))">' +
        '<path d="M12 0C6.5 0 2 4.4 2 9.9 2 17 12 30 12 30s10-13 10-20.1C22 4.4 17.5 0 12 0Z" fill="#c4441f" stroke="#fff" stroke-width="2"/>' +
        '<circle cx="12" cy="10" r="4" fill="#fff"/></svg>';
      destMarker.current = new maplibregl.Marker({ element: node, anchor: "bottom" })
        .setLngLat(lngLat)
        .addTo(m);
    } else {
      destMarker.current.setLngLat(lngLat);
    }
  }, [destination]);

  // Parking POI markers (green "P").
  useEffect(() => {
    const m = map.current;
    if (!m) return;
    // Rebuild from scratch whenever the set changes (small N).
    parkingMarkers.current.forEach((mk) => mk.remove());
    parkingMarkers.current = [];
    for (const p of parking || []) {
      if (p.lat == null || p.lon == null) continue;
      const full = (p.available ?? 1) <= 0;
      const node = document.createElement("div");
      node.style.cssText =
        `width:22px;height:22px;border-radius:50%;border:2px solid #fff;color:#fff;` +
        `font-weight:800;font-size:12px;display:grid;place-items:center;` +
        `box-shadow:0 1px 3px rgba(0,0,0,.45);background:${full ? "#8a94a6" : "#007a5a"}`;
      node.textContent = "P";
      parkingMarkers.current.push(
        new maplibregl.Marker({ element: node }).setLngLat([p.lon, p.lat]).addTo(m),
      );
    }
  }, [parking]);

  // Open framed on truck → destination (once), so the driver sees the whole trip
  // before the route tightens the view.
  useEffect(() => {
    const m = map.current;
    if (!m || !frameToTrip || framedTrip.current || !destination) return;
    const pts: [number, number][] = [[destination.lon, destination.lat]];
    if (truck) pts.push([truck.lon, truck.lat]);
    const fit = () => {
      try {
        if (pts.length === 1) {
          // Only the gate is known so far — centre on it, but DON'T lock framing
          // so a proper truck→gate fit still runs once the first fix arrives.
          m.easeTo({ center: pts[0], zoom: 13.5, duration: 400 });
        } else {
          const lons = pts.map((p) => p[0]);
          const lats = pts.map((p) => p[1]);
          m.fitBounds(
            [
              [Math.min(...lons), Math.min(...lats)],
              [Math.max(...lons), Math.max(...lats)],
            ],
            { padding: { top: 96, bottom: 190, left: 46, right: 46 }, animate: false, maxZoom: 14 },
          );
          framedTrip.current = true;
        }
      } catch {
        /* degenerate */
      }
    };
    if (m.isStyleLoaded()) fit();
    else m.once("load", fit);
  }, [truck, destination, frameToTrip]);

  return <div className={fill ? "minimap minimap-fill" : "minimap"} ref={el} />;
}
