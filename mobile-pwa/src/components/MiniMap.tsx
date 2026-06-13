import { useEffect, useRef } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { CorridorGeometry, DevicePosition, Gate } from "@/lib/types";

// A lightweight "traffic ahead" mini-map. To stay fast (FCP target) and work
// offline / without a paid tile key, it renders on a tile-less dark MapLibre
// style: just a background colour plus GeoJSON layers for the corridor polyline,
// the four gates, the segment "traffic ahead" colouring, and the live truck. The
// dashboard (web/) owns the full basemap; the driver only needs context.

const STYLE: maplibregl.StyleSpecification = {
  version: 8,
  sources: {},
  layers: [{ id: "bg", type: "background", paint: { "background-color": "#0e1626" } }],
};

interface Props {
  corridor?: CorridorGeometry;
  gates?: Gate[];
  truck?: DevicePosition | null;
  targetGateId?: string | null;
  // segment_id -> jam_factor (0..1), drives the "traffic ahead" colour.
  jam?: Record<string, number>;
}

function jamColor(j: number): string {
  if (j >= 0.66) return "#d55e00"; // vermillion — heavy
  if (j >= 0.33) return "#e69f00"; // orange — moderate
  return "#009e73"; // green — free-flow
}

export default function MiniMap({ corridor, gates, truck, targetGateId, jam }: Props) {
  const el = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const truckMarker = useRef<maplibregl.Marker | null>(null);

  useEffect(() => {
    if (!el.current || map.current) return;
    const m = new maplibregl.Map({
      container: el.current,
      style: STYLE,
      center: [72.952, 18.948],
      zoom: 11.5,
      attributionControl: false,
      interactive: true,
      dragRotate: false,
      pitchWithRotate: false,
    });
    map.current = m;
    m.on("load", () => m.resize());
    return () => {
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
          paint: { "line-color": jam ? jamColor(worstJam) : "#3b4a66", "line-width": 4 },
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
              "circle-color": ["case", ["get", "target"], "#56b4e9", "#64748b"],
              "circle-stroke-color": "#0b1220",
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
          { padding: 28, animate: false, maxZoom: 13 }
        );
      } catch {
        /* degenerate geometry */
      }
    };
    if (m.isStyleLoaded()) draw();
    else m.once("load", draw);
  }, [corridor, gates, targetGateId, jam]);

  // Live truck marker.
  useEffect(() => {
    const m = map.current;
    if (!m || !truck) return;
    const lngLat: [number, number] = [truck.lon, truck.lat];
    if (!truckMarker.current) {
      const node = document.createElement("div");
      node.style.cssText =
        "width:16px;height:16px;border-radius:50%;background:#f0e442;border:3px solid #0b1220;box-shadow:0 0 0 3px rgba(240,228,66,.35)";
      truckMarker.current = new maplibregl.Marker({ element: node }).setLngLat(lngLat).addTo(m);
    } else {
      truckMarker.current.setLngLat(lngLat);
    }
  }, [truck]);

  return <div className="minimap" ref={el} />;
}
