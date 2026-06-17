import { useEffect, useRef, useState } from "react";
import maplibregl, { Map as MlMap } from "maplibre-gl";
import { TerraDraw, TerraDrawPolygonMode, TerraDrawSelectMode } from "terra-draw";
import { TerraDrawMapLibreGLAdapter } from "terra-draw-maplibre-gl-adapter";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getAdapter } from "@/data";
import type { Zone } from "@/lib/types";
import { mapStyle, JNPA_CENTER, JNPA_ZOOM } from "@/lib/basemap";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/misc";
import { Pencil, MousePointer2, Save, Trash2 } from "lucide-react";

// Map editor (terra-draw) for no-parking / restricted polygons. Drawing or
// editing a polygon updates the local zone set; "Save zones" PUTs the whole set
// to /api/zones (Postgres) which the anomaly service then reads live. The
// escalation timeline (5/15/30 min) is editable per zone.
export default function GeofencingManager() {
  const qc = useQueryClient();
  const mapEl = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MlMap | null>(null);
  const drawRef = useRef<TerraDraw | null>(null);
  const [mode, setMode] = useState<"select" | "polygon">("select");
  const [zones, setZones] = useState<Zone[]>([]);
  const [dirty, setDirty] = useState(false);

  const zonesQ = useQuery({ queryKey: ["zones"], queryFn: () => getAdapter().zones(), staleTime: 30_000 });

  const save = useMutation({
    mutationFn: () => getAdapter().putZones(zones),
    onSuccess: () => {
      setDirty(false);
      qc.invalidateQueries({ queryKey: ["zones"] });
    },
  });

  // Initialise the map + terra-draw once.
  useEffect(() => {
    if (!mapEl.current || mapRef.current) return;
    const map = new maplibregl.Map({
      container: mapEl.current,
      style: mapStyle(),
      center: JNPA_CENTER,
      zoom: JNPA_ZOOM,
      attributionControl: { compact: true },
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    mapRef.current = map;

    map.on("load", () => {
      const draw = new TerraDraw({
        adapter: new TerraDrawMapLibreGLAdapter({ map }),
        modes: [
          new TerraDrawSelectMode({
            flags: {
              polygon: {
                feature: {
                  draggable: true,
                  coordinates: { midpoints: true, draggable: true, deletable: true },
                },
              },
            },
          }),
          new TerraDrawPolygonMode({
            styles: { fillColor: "#56B4E9", outlineColor: "#56B4E9", fillOpacity: 0.2 },
          }),
        ],
      });
      draw.start();
      draw.setMode("select");
      drawRef.current = draw;

      // Any change to the drawn features marks the set dirty + syncs local zones.
      draw.on("finish", () => syncFromDraw());
      draw.on("change", () => setDirty(true));
    });

    return () => {
      drawRef.current?.stop();
      drawRef.current = null;
      map.remove();
      mapRef.current = null;
    };
  }, []);

  // Load the saved zones into both local state and the draw canvas.
  useEffect(() => {
    const draw = drawRef.current;
    if (!zonesQ.data || !draw) return;
    const loaded = zonesQ.data;
    setZones(loaded);
    try {
      draw.clear();
      draw.addFeatures(
        loaded
          .filter((z) => z.polygon?.length >= 3)
          .map((z) => ({
            type: "Feature" as const,
            id: z.id,
            properties: { mode: "polygon", zoneId: z.id, kind: z.kind, name: z.name },
            geometry: { type: "Polygon" as const, coordinates: [closeRing(z.polygon)] },
          }))
      );
    } catch {
      /* draw not ready yet — retried on next data tick */
    }
  }, [zonesQ.data, drawRef.current]); // eslint-disable-line react-hooks/exhaustive-deps

  function syncFromDraw() {
    const draw = drawRef.current;
    if (!draw) return;
    const snapshot = draw.getSnapshot();
    const existing = new Map(zones.map((z) => [z.id, z]));
    const next: Zone[] = [];
    let nNew = 0;
    for (const f of snapshot) {
      if (f.geometry.type !== "Polygon") continue;
      const ring = (f.geometry.coordinates[0] as [number, number][]) ?? [];
      if (ring.length < 4) continue; // closed ring has >=4 points
      const id = (f.properties?.zoneId as string) || String(f.id);
      const prev = existing.get(id);
      next.push(
        prev
          ? { ...prev, polygon: ring }
          : {
              id: `NPZ-DRAWN-${++nNew}-${String(f.id).slice(0, 6)}`,
              name: `Drawn zone ${nNew}`,
              kind: "no_parking",
              polygon: ring,
              escalation: { warn_min: 5, notice_min: 15, challan_min: 30 },
              enabled: true,
            }
      );
    }
    setZones(next);
    setDirty(true);
  }

  function patchZone(id: string, patch: Partial<Zone>) {
    setZones((zs) => zs.map((z) => (z.id === id ? { ...z, ...patch } : z)));
    setDirty(true);
  }

  function setDrawMode(m: "select" | "polygon") {
    setMode(m);
    drawRef.current?.setMode(m);
  }

  return (
    <div className="flex h-full">
      <div className="relative min-w-0 flex-1">
        <div ref={mapEl} className="h-full w-full" data-testid="geofence-map" />
        <div className="absolute left-3 top-3 flex gap-2 rounded-md border border-border bg-card/85 p-1.5 backdrop-blur">
          <Button size="sm" variant={mode === "select" ? "default" : "ghost"} onClick={() => setDrawMode("select")}>
            <MousePointer2 className="h-3.5 w-3.5" /> Select / edit
          </Button>
          <Button size="sm" variant={mode === "polygon" ? "default" : "ghost"} onClick={() => setDrawMode("polygon")}>
            <Pencil className="h-3.5 w-3.5" /> Draw zone
          </Button>
        </div>
      </div>

      <aside className="flex w-96 shrink-0 flex-col border-l border-border bg-card/40">
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div>
            <h2 className="text-sm font-semibold">Geo-fence zones</h2>
            <p className="text-[11px] text-muted-foreground">{zones.length} zones · anomaly service reads live</p>
          </div>
          <Button size="sm" onClick={() => save.mutate()} disabled={!dirty || save.isPending}>
            {save.isPending ? <Spinner /> : <Save className="h-3.5 w-3.5" />}
            Save
          </Button>
        </div>
        {save.isSuccess && !dirty && (
          <div className="bg-severity-ok/15 px-4 py-1.5 text-xs text-severity-ok">Saved to Postgres.</div>
        )}
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
          {zonesQ.isLoading && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Spinner /> loading…
            </div>
          )}
          {zones.map((z) => (
            <ZoneCard key={z.id} zone={z} onPatch={(p) => patchZone(z.id, p)} onDelete={() => {
              setZones((zs) => zs.filter((x) => x.id !== z.id));
              try { drawRef.current?.removeFeatures([z.id]); } catch { /* not on canvas */ }
              setDirty(true);
            }} />
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
  const esc = zone.escalation || { warn_min: 5, notice_min: 15, challan_min: 30 };
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="truncate">{zone.name}</CardTitle>
        <div className="flex items-center gap-2">
          <Badge colour={zone.kind === "restricted" ? "#D55E00" : "#56B4E9"}>{zone.kind}</Badge>
          <button onClick={onDelete} aria-label="delete zone" className="text-muted-foreground hover:text-severity-critical">
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <label className="block text-[11px] text-muted-foreground">
          Kind
          <select
            value={zone.kind}
            onChange={(e) => onPatch({ kind: e.target.value })}
            className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1 text-xs"
          >
            <option value="no_parking">no_parking</option>
            <option value="restricted">restricted</option>
          </select>
        </label>

        <div>
          <div className="mb-1 text-[11px] text-muted-foreground">Escalation timeline (minutes)</div>
          <div className="grid grid-cols-3 gap-2">
            <EscInput label="Warn" colour="#E69F00" value={esc.warn_min} onChange={(v) => onPatch({ escalation: { ...esc, warn_min: v } })} />
            <EscInput label="Notice" colour="#D55E00" value={esc.notice_min} onChange={(v) => onPatch({ escalation: { ...esc, notice_min: v } })} />
            <EscInput label="Challan" colour="#D55E00" value={esc.challan_min} onChange={(v) => onPatch({ escalation: { ...esc, challan_min: v } })} />
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

function EscalationBar({ esc }: { esc: { warn_min: number; notice_min: number; challan_min: number } }) {
  const max = Math.max(esc.challan_min, 30, 1);
  const seg = (m: number) => `${Math.min(100, (m / max) * 100)}%`;
  return (
    <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-muted" aria-hidden>
      <div className="relative h-full">
        <div className="absolute h-full bg-severity-warning" style={{ left: 0, width: seg(esc.warn_min) }} />
        <div className="absolute h-full bg-severity-critical/70" style={{ left: seg(esc.notice_min), right: 0 }} />
      </div>
    </div>
  );
}

function closeRing(ring: [number, number][]): [number, number][] {
  if (ring.length && (ring[0][0] !== ring[ring.length - 1][0] || ring[0][1] !== ring[ring.length - 1][1])) {
    return [...ring, ring[0]];
  }
  return ring;
}
