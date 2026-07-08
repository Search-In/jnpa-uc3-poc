import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import MiniMap, { type RouteLine } from "@/components/MiniMap";
import { api } from "@/lib/api";
import { cached } from "@/lib/store";
import type { CorridorGeometry, Gate, TruckEnvelope } from "@/lib/types";

// Live Map — full-screen, Google-Maps-style driver navigation. Road basemap,
// your live position, the destination gate, and MULTIPLE route options (fastest
// + alternates) from OSRM, selectable like Google Maps. All data is gateway/RDS
// backed and cached for offline.

type RouteOpt = { id: string; coords: [number, number][]; durationMin: number; distanceKm: number };

async function fetchAlternatives(
  from: { lat: number; lon: number },
  to: { lat: number; lon: number },
): Promise<RouteOpt[]> {
  const url =
    `https://router.project-osrm.org/route/v1/driving/` +
    `${from.lon},${from.lat};${to.lon},${to.lat}` +
    `?alternatives=true&geometries=geojson&overview=full`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`osrm ${res.status}`);
  const data = await res.json();
  return (data.routes || []).slice(0, 3).map((r: any, i: number) => ({
    id: `route-${i}`,
    coords: r.geometry.coordinates as [number, number][],
    durationMin: Math.round((r.duration || 0) / 60),
    distanceKm: Math.round((r.distance || 0) / 100) / 10,
  }));
}

export default function MapView({ deviceId }: { deviceId: string }) {
  const { t } = useTranslation();
  const [corridor, setCorridor] = useState<CorridorGeometry | undefined>();
  const [gates, setGates] = useState<Gate[] | undefined>();
  const [truck, setTruck] = useState<{ lat: number; lon: number } | null>(null);
  const [targetGate, setTargetGate] = useState<Gate | null>(null);
  const [zones, setZones] = useState<{ id: string; name: string; kind: string }[]>([]);
  const [parkingFree, setParkingFree] = useState<number | null>(null);

  const [opts, setOpts] = useState<RouteOpt[]>([]);
  const [selected, setSelected] = useState(0);
  const [routeErr, setRouteErr] = useState<string | null>(null);
  const routedFrom = useRef<string>("");

  useEffect(() => {
    void cached<{ gates: Gate[] }>("gates", () => api.gates()).then((g) => g && setGates(g.gates));
    void cached<{ zones: { id: string; name: string; kind: string }[] }>("geo-zones", () =>
      api.geoZones(),
    ).then((z) => z && setZones(z.zones || []));
    void cached<{ total_available?: number }>("parking-sum", () => api.parkingSummary()).then(
      (p) => p && setParkingFree(p.total_available ?? null),
    );
    void cached<CorridorGeometry>("corridor", () => api.corridor()).then(
      (c) => c && setCorridor(c),
    );
  }, []);

  // Live position + destination gate.
  useEffect(() => {
    const poll = async () => {
      try {
        const env: TruckEnvelope = await api.truck(deviceId);
        const p: any = (env as any).record || {};
        if (p.lat != null) setTruck({ lat: p.lat, lon: p.lon });
        const gid: string | undefined = p.gate_id || (env as any).gate_id;
        if (gid && gates) {
          const g = gates.find((x) => x.id === gid || x.id.endsWith(gid.replace(/^GATE-/, "")));
          if (g) setTargetGate(g);
        }
      } catch {
        /* offline / no fix */
      }
    };
    void poll();
    const iv = window.setInterval(poll, 10000);
    return () => window.clearInterval(iv);
  }, [deviceId, gates]);

  // Destination fallback: first gate if the truck's gate isn't resolved.
  const dest = targetGate || (gates && gates[0]) || null;

  // Compute alternative routes once we have a position + destination (re-run only
  // when the origin moves materially, to avoid spamming OSRM).
  useEffect(() => {
    if (!truck || !dest) return;
    const key = `${truck.lat.toFixed(3)},${truck.lon.toFixed(3)}->${dest.id}`;
    if (routedFrom.current === key) return;
    routedFrom.current = key;
    (async () => {
      try {
        const r = await fetchAlternatives(truck, { lat: dest.lat, lon: dest.lon });
        setOpts(r);
        setSelected(0);
        setRouteErr(r.length ? null : t("map.noRoute", { defaultValue: "No route found" }));
      } catch {
        setRouteErr(t("map.routeErr", { defaultValue: "Routing unavailable" }));
      }
    })();
  }, [truck, dest, t]);

  const routeLines: RouteLine[] = useMemo(
    () => opts.map((o, i) => ({ id: o.id, coords: o.coords, primary: i === selected })),
    [opts, selected],
  );

  const restricted = zones.filter((z) => z.kind === "restricted").length;
  const noParking = zones.filter((z) => z.kind !== "restricted").length;

  return (
    // Fill the content area (which already reserves bottom space for the fixed
    // tab bar via .content's padding). The map grows to fill, the route bar docks
    // below it, and the tab bar stays visible.
    <div style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0 }}>
      {/* Full-screen road map with route options */}
      <div style={{ position: "relative", flex: 1, minHeight: 0 }}>
        <MiniMap
          fill
          roads
          gates={gates}
          truck={truck as any}
          targetGateId={dest?.id ?? null}
          corridor={opts.length ? undefined : corridor}
          routes={routeLines}
        />

        {/* Destination banner (Google-Maps style) */}
        {dest && (
          <div
            style={{
              position: "absolute",
              top: 10,
              left: 10,
              right: 10,
              background: "var(--surface,#fff)",
              borderRadius: 12,
              padding: "10px 14px",
              boxShadow: "0 2px 8px rgba(0,0,0,.15)",
            }}
          >
            <div className="muted" style={{ fontSize: 11 }}>
              {t("map.destination", { defaultValue: "Destination" })}
            </div>
            <div style={{ fontWeight: 700, fontSize: 15 }}>🏁 {dest.name}</div>
          </div>
        )}
      </div>

      {/* Route options — tap to select (like Google Maps). Docks just above the
          fixed tab bar (the content area already reserves that space). */}
      <div
        style={{
          padding: "10px 12px",
          borderTop: "1px solid var(--border,#ddd)",
          background: "var(--surface,#fff)",
        }}
      >
        {routeErr && (
          <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
            {routeErr}
          </div>
        )}
        {opts.length > 0 ? (
          <div style={{ display: "flex", gap: 8, overflowX: "auto", paddingBottom: 4 }}>
            {opts.map((o, i) => (
              <button
                key={o.id}
                onClick={() => setSelected(i)}
                style={{
                  flex: "0 0 auto",
                  textAlign: "left",
                  borderRadius: 12,
                  padding: "8px 14px",
                  border: `2px solid ${i === selected ? "#1a56db" : "var(--border,#ddd)"}`,
                  background: i === selected ? "rgba(26,86,219,.08)" : "transparent",
                  minWidth: 120,
                }}
              >
                <div
                  style={{
                    fontWeight: 700,
                    fontSize: 16,
                    color: i === selected ? "#1a56db" : undefined,
                  }}
                >
                  {o.durationMin} min
                </div>
                <div className="muted" style={{ fontSize: 12 }}>
                  {o.distanceKm} km ·{" "}
                  {i === 0
                    ? t("map.fastest", { defaultValue: "Fastest" })
                    : t("map.alt", { defaultValue: "Alternate" })}
                </div>
              </button>
            ))}
          </div>
        ) : (
          !routeErr && (
            <div className="muted" style={{ fontSize: 12 }}>
              {t("map.computing", { defaultValue: "Computing routes…" })}
            </div>
          )
        )}

        {/* Legend + quick counts */}
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 8, fontSize: 12 }}>
          <span>
            <span style={{ color: "#16a34a" }}>●</span>{" "}
            {t("map.normal", { defaultValue: "Normal" })}
          </span>
          <span>
            <span style={{ color: "#d97706" }}>●</span>{" "}
            {t("map.medium", { defaultValue: "Medium" })}
          </span>
          <span>
            <span style={{ color: "#dc2626" }}>●</span> {t("map.heavy", { defaultValue: "Heavy" })}
          </span>
          <span className="muted">
            🅿 {parkingFree ?? "—"} · ⛔ {noParking} · 🚫 {restricted} · 🏗 {gates?.length ?? 0}
          </span>
        </div>
      </div>
    </div>
  );
}
