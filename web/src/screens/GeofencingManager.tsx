import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
// Register the <arcgis-map> custom element + bundle its runtime locally (the
// side-effect import is what actually defines the element). This is the SAME
// ESRI / ArcGIS Maps SDK surface used by the operations map (ArcgisMap.tsx), so
// the whole app now draws on one map technology.
import "@arcgis/map-components/components/arcgis-map";
import { ArcgisMap as ArcgisMapWC } from "@arcgis/map-components-react";
import GraphicsLayer from "@arcgis/core/layers/GraphicsLayer";
import Graphic from "@arcgis/core/Graphic";
import Polygon from "@arcgis/core/geometry/Polygon";
import Point from "@arcgis/core/geometry/Point";
import SimpleFillSymbol from "@arcgis/core/symbols/SimpleFillSymbol";
import SimpleLineSymbol from "@arcgis/core/symbols/SimpleLineSymbol";
import SketchViewModel from "@arcgis/core/widgets/Sketch/SketchViewModel";
import { webMercatorToGeographic } from "@arcgis/core/geometry/support/webMercatorUtils";
import type MapView from "@arcgis/core/views/MapView";
import esriConfig from "@arcgis/core/config";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getAdapter } from "@/data";
import type { Zone } from "@/lib/types";
import { JNPA_CENTER, JNPA_ZOOM } from "@/lib/basemap";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/misc";
import { Pencil, MousePointer2, Save, Trash2 } from "lucide-react";

const WGS84 = { wkid: 4326 } as const;
// Zone fill/outline — the CB-safe info blue used across the map surfaces.
const ZONE_COLOUR = "#56B4E9";
// Token-free basemap that renders in dev without an ArcGIS API key (same choice
// as the operations map). A key, if provided, is a graceful upgrade only.
const DEFAULT_BASEMAP = "dark-gray-vector";

const ARCGIS_API_KEY = (() => {
  const key = import.meta.env.VITE_ARCGIS_API_KEY;
  return typeof key === "string" && key.trim() ? key.trim() : undefined;
})();
if (ARCGIS_API_KEY) {
  esriConfig.apiKey = ARCGIS_API_KEY;
}

/** Handle returned by view.on(...) — typed without naming the module-scoped IHandle. */
type ViewHandle = ReturnType<MapView["on"]>;

// Geo-fence editor on the ESRI / ArcGIS map. The on-map "geofencing menu"
// (Select / edit · Draw zone) drives ArcGIS's native SketchViewModel to draw and
// reshape no-parking / restricted polygons directly on the map. Drawing or
// editing a polygon updates the local zone set; "Save zones" PUTs the whole set
// to /api/zones (Postgres) which the anomaly service then reads live. The
// escalation timeline (5/15/30 min) is editable per zone.
export default function GeofencingManager() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const viewRef = useRef<MapView | null>(null);
  const layerRef = useRef<GraphicsLayer | null>(null);
  const svmRef = useRef<SketchViewModel | null>(null);
  const clickHandle = useRef<ViewHandle | null>(null);
  // Read the live mode inside imperative ArcGIS event handlers (which close over
  // the mount-time value otherwise).
  const modeRef = useRef<"select" | "polygon">("select");
  // Load the server zones onto the canvas exactly once (later edits are local
  // until saved, so a background refetch must never clobber in-flight drawing).
  const loadedRef = useRef(false);
  const [mode, setMode] = useState<"select" | "polygon">("select");
  const [zones, setZones] = useState<Zone[]>([]);
  const [dirty, setDirty] = useState(false);

  const zonesQ = useQuery({
    queryKey: ["zones"],
    queryFn: () => getAdapter().zones(),
    staleTime: 30_000,
  });

  const save = useMutation({
    mutationFn: () => getAdapter().putZones(zones),
    onSuccess: () => {
      setDirty(false);
      qc.invalidateQueries({ queryKey: ["zones"] });
    },
  });

  const zoneSymbol = useMemo(
    () =>
      new SimpleFillSymbol({
        color: hexToRgba(ZONE_COLOUR, 0.2),
        outline: new SimpleLineSymbol({
          color: ZONE_COLOUR,
          width: 2.25,
          cap: "round",
          join: "round",
        }),
      }),
    [],
  );

  // Find the polygon graphic on the sketch layer that backs a given zone id.
  const graphicFor = useCallback((id: string): Graphic | undefined => {
    return layerRef.current?.graphics.find((g) => g.getAttribute("zoneId") === id) as
      | Graphic
      | undefined;
  }, []);

  // Extract the outer ring of a sketch graphic as a [lon,lat][] ring in WGS84
  // (the sketch geometry comes back in the view's Web-Mercator SR, so project).
  function ringOf(g: Graphic): [number, number][] {
    const geom = g.geometry as Polygon;
    if (!geom || geom.type !== "polygon") return [];
    const geo = geom.spatialReference?.isWGS84 ? geom : (webMercatorToGeographic(geom) as Polygon);
    return (geo.rings[0] as [number, number][]) ?? [];
  }

  // Upsert the zone backing a freshly-drawn / reshaped graphic. New graphics get
  // a generated zone id (stamped back onto the graphic so future edits match).
  const syncZoneFromGraphic = useCallback((g: Graphic, isNew: boolean) => {
    const ring = ringOf(g);
    if (ring.length < 4) return; // a closed ring has >= 4 points
    setZones((prev) => {
      if (!isNew) {
        const id = g.getAttribute("zoneId") as string | undefined;
        return prev.map((z) => (z.id === id ? { ...z, polygon: ring } : z));
      }
      const n = prev.filter((z) => z.id.startsWith("NPZ-DRAWN-")).length + 1;
      const id = `NPZ-DRAWN-${n}`;
      g.setAttribute("zoneId", id);
      g.setAttribute("kind", "no_parking");
      return [
        ...prev,
        {
          id,
          name: `Drawn zone ${n}`,
          kind: "no_parking",
          polygon: ring,
          escalation: { warn_min: 5, notice_min: 15, challan_min: 30 },
          enabled: true,
        },
      ];
    });
    setDirty(true);
  }, []);

  // ---- create the sketch layer + SketchViewModel once the view is ready ----
  const handleReady = useCallback(
    (event: { target: { view: MapView } }) => {
      const view = event.target.view;
      if (!view || !view.map) return;
      viewRef.current = view;
      if (view.constraints) view.constraints.snapToZoom = false;
      view.ui.components = ["zoom", "attribution"];

      const layer = new GraphicsLayer({ id: "uc3-geofence-edit", title: "Geo-fence zones" });
      view.map.add(layer);
      layerRef.current = layer;

      const svm = new SketchViewModel({
        view,
        layer,
        polygonSymbol: zoneSymbol,
        defaultUpdateOptions: { tool: "reshape", enableRotation: false, toggleToolOnClick: false },
      });
      svmRef.current = svm;

      // A completed draw becomes a new zone; staying in draw mode lets the
      // operator lay down several zones in a row (matching the old editor).
      svm.on("create", (e) => {
        if (e.state !== "complete") return;
        syncZoneFromGraphic(e.graphic, true);
        if (modeRef.current === "polygon") svm.create("polygon");
      });
      // A completed reshape/move writes the new geometry back to its zone.
      svm.on("update", (e) => {
        if (e.state !== "complete") return;
        for (const g of e.graphics) syncZoneFromGraphic(g, false);
      });
      // Deleting a graphic (Sketch delete key) drops its zone.
      svm.on("delete", (e) => {
        const ids = e.graphics
          .map((g) => g.getAttribute("zoneId") as string | undefined)
          .filter(Boolean) as string[];
        if (!ids.length) return;
        setZones((zs) => zs.filter((z) => !ids.includes(z.id)));
        setDirty(true);
      });

      // In select mode, clicking a zone graphic opens it for reshape/move.
      clickHandle.current?.remove();
      clickHandle.current = view.on("click", (e) => {
        if (modeRef.current !== "select") return;
        void view.hitTest(e).then((res) => {
          const hit = res.results.find(
            (r) => r.type === "graphic" && r.graphic?.layer === layerRef.current,
          );
          if (hit && hit.type === "graphic") svm.update([hit.graphic]);
        });
      });

      loadZonesToLayer(zonesQ.data);
    },
    // Stable deps: refs + memoised symbol; render helpers close over refs only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  // Draw the saved zones onto the sketch layer + seed local state, exactly once.
  const loadZonesToLayer = useCallback((data: Zone[] | undefined) => {
    const layer = layerRef.current;
    if (!layer || !data || loadedRef.current) return;
    loadedRef.current = true;
    setZones(data);
    layer.removeAll();
    for (const z of data) {
      if (!z.polygon || z.polygon.length < 3) continue;
      layer.add(
        new Graphic({
          geometry: new Polygon({ rings: [closeRing(z.polygon)], spatialReference: WGS84 }),
          symbol: zoneSymbol,
          attributes: { zoneId: z.id, kind: z.kind, name: z.name },
        }),
      );
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Server data may arrive after the view — load it whenever both are ready.
  useEffect(() => {
    if (zonesQ.data) loadZonesToLayer(zonesQ.data);
  }, [zonesQ.data, loadZonesToLayer]);

  // Clean up the sketch model + click handler on unmount.
  useEffect(() => {
    return () => {
      clickHandle.current?.remove();
      clickHandle.current = null;
      svmRef.current?.destroy();
      svmRef.current = null;
    };
  }, []);

  function patchZone(id: string, patch: Partial<Zone>) {
    setZones((zs) => zs.map((z) => (z.id === id ? { ...z, ...patch } : z)));
    // Keep the graphic's kind attribute in step so its future edits carry it.
    if (patch.kind != null) graphicFor(id)?.setAttribute("kind", patch.kind);
    setDirty(true);
  }

  function setDrawMode(m: "select" | "polygon") {
    setMode(m);
    modeRef.current = m;
    const svm = svmRef.current;
    if (!svm) return;
    svm.cancel();
    if (m === "polygon") svm.create("polygon");
  }

  // The centre Point, built once so the memoised element receives it a single
  // time (the @lit/react wrapper re-applies element props on every render, which
  // would otherwise re-command the camera — see ArcgisMap.tsx for the full note).
  const initialCenter = useMemo(
    () => new Point({ longitude: JNPA_CENTER[0], latitude: JNPA_CENTER[1] }),
    [],
  );
  const mapElement = useMemo(
    () => (
      <ArcgisMapWC
        basemap={DEFAULT_BASEMAP}
        center={initialCenter}
        zoom={JNPA_ZOOM}
        onArcgisViewReadyChange={handleReady}
        style={{ height: "100%", width: "100%" }}
      />
    ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  return (
    <div className="flex h-full">
      <div className="relative min-w-0 flex-1">
        <div className="h-full w-full" data-testid="geofence-map">
          {mapElement}
        </div>
        <div className="absolute left-3 top-3 z-10 flex gap-2 rounded-md border border-border bg-card/85 p-1.5 backdrop-blur">
          <Button
            size="sm"
            variant={mode === "select" ? "default" : "ghost"}
            onClick={() => setDrawMode("select")}
          >
            <MousePointer2 className="h-3.5 w-3.5" /> {t("geofencing.selectEdit")}
          </Button>
          <Button
            size="sm"
            variant={mode === "polygon" ? "default" : "ghost"}
            onClick={() => setDrawMode("polygon")}
          >
            <Pencil className="h-3.5 w-3.5" /> {t("geofencing.drawZone")}
          </Button>
        </div>
      </div>

      <aside className="flex w-96 shrink-0 flex-col border-l border-border bg-card/40">
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div>
            <h2 className="text-sm font-semibold">{t("geofencing.zonesTitle")}</h2>
            <p className="text-[11px] text-muted-foreground">
              {t("geofencing.zonesSubtitle", { count: zones.length })}
            </p>
          </div>
          <Button size="sm" onClick={() => save.mutate()} disabled={!dirty || save.isPending}>
            {save.isPending ? <Spinner /> : <Save className="h-3.5 w-3.5" />}
            {t("common.save")}
          </Button>
        </div>
        {save.isSuccess && !dirty && (
          <div className="bg-severity-ok/15 px-4 py-1.5 text-xs text-severity-ok">
            {t("geofencing.savedToPostgres")}
          </div>
        )}
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
          {zonesQ.isLoading && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Spinner /> {t("common.loading")}
            </div>
          )}
          {zones.map((z) => (
            <ZoneCard
              key={z.id}
              zone={z}
              onPatch={(p) => patchZone(z.id, p)}
              onDelete={() => {
                setZones((zs) => zs.filter((x) => x.id !== z.id));
                const g = graphicFor(z.id);
                if (g) layerRef.current?.remove(g);
                setDirty(true);
              }}
            />
          ))}
        </div>
      </aside>
    </div>
  );
}

function ZoneCard({
  zone,
  onPatch,
  onDelete,
}: {
  zone: Zone;
  onPatch: (p: Partial<Zone>) => void;
  onDelete: () => void;
}) {
  const { t } = useTranslation();
  const esc = zone.escalation || { warn_min: 5, notice_min: 15, challan_min: 30 };
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="truncate">{zone.name}</CardTitle>
        <div className="flex items-center gap-2">
          <Badge colour={zone.kind === "restricted" ? "#D55E00" : "#56B4E9"}>
            {zone.kind === "restricted"
              ? t("geofencing.kindRestricted")
              : t("geofencing.kindNoParking")}
          </Badge>
          <button
            onClick={onDelete}
            aria-label={t("geofencing.deleteZone")}
            className="text-muted-foreground hover:text-severity-critical"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <label className="block text-[11px] text-muted-foreground">
          {t("geofencing.kind")}
          <select
            value={zone.kind}
            onChange={(e) => onPatch({ kind: e.target.value })}
            className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1 text-xs"
          >
            <option value="no_parking">{t("geofencing.kindNoParking")}</option>
            <option value="restricted">{t("geofencing.kindRestricted")}</option>
          </select>
        </label>

        <div>
          <div className="mb-1 text-[11px] text-muted-foreground">
            {t("geofencing.escalationTimeline")}
          </div>
          <div className="grid grid-cols-3 gap-2">
            <EscInput
              label={t("geofencing.warn")}
              colour="#E69F00"
              value={esc.warn_min}
              onChange={(v) => onPatch({ escalation: { ...esc, warn_min: v } })}
            />
            <EscInput
              label={t("geofencing.notice")}
              colour="#D55E00"
              value={esc.notice_min}
              onChange={(v) => onPatch({ escalation: { ...esc, notice_min: v } })}
            />
            <EscInput
              label={t("geofencing.challan")}
              colour="#D55E00"
              value={esc.challan_min}
              onChange={(v) => onPatch({ escalation: { ...esc, challan_min: v } })}
            />
          </div>
          <EscalationBar esc={esc} />
        </div>
      </CardContent>
    </Card>
  );
}

function EscInput({
  label,
  colour,
  value,
  onChange,
}: {
  label: string;
  colour: string;
  value: number;
  onChange: (v: number) => void;
}) {
  return (
    <label className="text-[11px]">
      <span className="flex items-center gap-1" style={{ color: colour }}>
        {label}
      </span>
      <input
        type="number"
        min={0}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1 text-xs tabular-nums"
      />
    </label>
  );
}

function EscalationBar({
  esc,
}: {
  esc: { warn_min: number; notice_min: number; challan_min: number };
}) {
  const max = Math.max(esc.challan_min, 30, 1);
  const seg = (m: number) => `${Math.min(100, (m / max) * 100)}%`;
  return (
    <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-muted" aria-hidden>
      <div className="relative h-full">
        <div
          className="absolute h-full bg-severity-warning"
          style={{ left: 0, width: seg(esc.warn_min) }}
        />
        <div
          className="absolute h-full bg-severity-critical/70"
          style={{ left: seg(esc.notice_min), right: 0 }}
        />
      </div>
    </div>
  );
}

function closeRing(ring: [number, number][]): [number, number][] {
  if (
    ring.length &&
    (ring[0][0] !== ring[ring.length - 1][0] || ring[0][1] !== ring[ring.length - 1][1])
  ) {
    return [...ring, ring[0]];
  }
  return ring;
}

/** Convert "#RRGGBB" + alpha → [r,g,b,a] tuple for an ArcGIS Color. */
function hexToRgba(hex: string, alpha: number): [number, number, number, number] {
  const h = hex.replace("#", "");
  return [
    parseInt(h.slice(0, 2), 16),
    parseInt(h.slice(2, 4), 16),
    parseInt(h.slice(4, 6), 16),
    alpha,
  ];
}
