import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import MiniMap, { type RouteLine } from "@/components/MiniMap";
import { IconFlag, IconNavigate, IconRoute } from "@/components/icons";
import { trafficFromSpeed } from "@/lib/driverLang";
import { api } from "@/lib/api";
import { cached } from "@/lib/store";
import type { CorridorGeometry, Gate, TruckEnvelope } from "@/lib/types";

// Navigate — a full-screen, ArcGIS/Esri-satellite navigation experience that
// reuses the SAME map stack and basemap as Home (MiniMap + lib/basemap). On top
// of the shared imagery it overlays: the corridor, the destination gate pin,
// parking POIs, the multi-option OSRM route, and the directional truck puck. The
// bottom sheet always shows the recommended route + ETA/distance/traffic +
// alternatives (never a permanent skeleton).

type RouteOpt = { id: string; coords: [number, number][]; durationMin: number; distanceKm: number };
type Park = { id: string; lat: number; lon: number; available?: number | null };

function haversineKm(a: { lat: number; lon: number }, b: { lat: number; lon: number }): number {
  const R = 6371;
  const dLat = ((b.lat - a.lat) * Math.PI) / 180;
  const dLon = ((b.lon - a.lon) * Math.PI) / 180;
  const s =
    Math.sin(dLat / 2) ** 2 +
    Math.cos((a.lat * Math.PI) / 180) * Math.cos((b.lat * Math.PI) / 180) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(s));
}

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
  const [heading, setHeading] = useState<number | null>(null);
  const [speed, setSpeed] = useState<number | null>(null);
  const [targetGate, setTargetGate] = useState<Gate | null>(null);
  const [parking, setParking] = useState<Park[]>([]);

  const [opts, setOpts] = useState<RouteOpt[]>([]);
  const [selected, setSelected] = useState(0);
  const [routeErr, setRouteErr] = useState<string | null>(null);
  const [routing, setRouting] = useState(true);
  const routedFrom = useRef<string>("");

  useEffect(() => {
    void cached<{ gates: Gate[] }>("gates", () => api.gates()).then((g) => g && setGates(g.gates));
    void cached<CorridorGeometry>("corridor", () => api.corridor()).then(
      (c) => c && setCorridor(c),
    );
    // Parking POIs to plot on the map (display only).
    void cached<{ facilities: any[] }>("parking-avail", () => api.parkingAvailability()).then(
      (p) =>
        p &&
        setParking(
          (p.facilities || [])
            .filter((f) => f.lat != null && f.lon != null)
            .map((f) => ({ id: f.facility_id, lat: f.lat, lon: f.lon, available: f.available })),
        ),
    );
  }, []);

  // Live position + heading + speed + destination gate.
  useEffect(() => {
    const poll = async () => {
      try {
        const env: TruckEnvelope = await api.truck(deviceId);
        const p: any = (env as any).record || {};
        const lat = p.lat ?? p.position?.lat;
        const lon = p.lon ?? p.position?.lon;
        if (lat != null) setTruck({ lat, lon });
        if (typeof p.heading === "number") setHeading(p.heading);
        if (typeof p.speed_kmh === "number") setSpeed(p.speed_kmh);
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

  // Compute alternative routes once we have a position + destination.
  useEffect(() => {
    if (!truck || !dest) return;
    const key = `${truck.lat.toFixed(3)},${truck.lon.toFixed(3)}->${dest.id}`;
    if (routedFrom.current === key) return;
    routedFrom.current = key;
    setRouting(true);
    (async () => {
      try {
        const r = await fetchAlternatives(truck, { lat: dest.lat, lon: dest.lon });
        setOpts(r);
        setSelected(0);
        setRouteErr(r.length ? null : t("map.noRoute", { defaultValue: "Route not available yet" }));
      } catch {
        setRouteErr(t("map.routeErr", { defaultValue: "Route not available right now" }));
      } finally {
        setRouting(false);
      }
    })();
  }, [truck, dest, t]);

  const routeLines: RouteLine[] = useMemo(
    () => opts.map((o, i) => ({ id: o.id, coords: o.coords, primary: i === selected })),
    [opts, selected],
  );

  const sel = opts[selected];
  const straightKm = truck && dest ? haversineKm(truck, { lat: dest.lat, lon: dest.lon }) : null;

  // Traffic condition: from the selected route's average speed when available,
  // otherwise the truck's live speed. Presentation only.
  const routeAvgKmh =
    sel && sel.durationMin > 0 ? (sel.distanceKm / sel.durationMin) * 60 : null;
  const traffic = trafficFromSpeed(routeAvgKmh ?? speed);

  return (
    <div className="nav-screen">
      {/* Full-screen map — SAME basemap/engine as Home (no `roads`: Esri satellite). */}
      <div className="nav-map">
        <MiniMap
          fill
          gates={gates}
          truck={truck as any}
          heading={heading}
          targetGateId={dest?.id ?? null}
          destination={dest ? { lat: dest.lat, lon: dest.lon, name: dest.name } : null}
          parking={parking}
          frameToTrip
          // Show the OSRM route when we have one; otherwise fall back to the
          // static corridor line so the map is never bare. Gates render either
          // way (they're decoupled from the corridor now).
          corridor={opts.length ? undefined : corridor}
          routes={routeLines}
        />

        {/* Floating destination card (top) */}
        {dest && (
          <div className="nav-top-card">
            <span className="nav-top-flag">
              <IconFlag size={20} />
            </span>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div className="nav-top-eyebrow">
                {t("map.destination", { defaultValue: "Destination" })}
              </div>
              <div className="nav-top-dest">{dest.name}</div>
            </div>
            <div className="nav-top-eta">
              <div className="v">
                {sel ? sel.durationMin : straightKm != null ? Math.round((straightKm / 25) * 60) : "—"}
                <span className="u">min</span>
              </div>
              <div className="k">
                {sel ? sel.distanceKm : straightKm != null ? straightKm.toFixed(1) : "—"} km
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Bottom instruction sheet — always populated (recommended + ETA + distance
          + traffic + alternatives). */}
      <div className="nav-sheet">
        <div className="nav-sheet-grab" aria-hidden />

        <div className="nav-headline">
          <span className="nav-headline-ico">
            <IconNavigate size={22} />
          </span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="nav-headline-eta">
              {sel ? sel.durationMin : straightKm != null ? Math.round((straightKm / 25) * 60) : "—"}{" "}
              <span className="u">min</span>
              <span className="nav-headline-dist">
                · {sel ? sel.distanceKm : straightKm != null ? straightKm.toFixed(1) : "—"} km
              </span>
            </div>
            <div className="nav-headline-sub">
              {sel
                ? (selected === 0
                    ? t("map.fastest", { defaultValue: "Fastest route" })
                    : t("map.alt", { defaultValue: "Alternate route" })) +
                  (dest ? ` · ${dest.name}` : "")
                : routing
                  ? t("map.finding", { defaultValue: "Finding the fastest route…" })
                  : routeErr || t("map.directDistance", { defaultValue: "Direct distance to gate" })}
            </div>
          </div>
          {sel && selected === 0 ? (
            <span className="nav-recommend">
              {t("map.recommended", { defaultValue: "Recommended" })}
            </span>
          ) : null}
        </div>

        {/* Traffic condition */}
        {traffic ? (
          <div className="nav-traffic">
            <span className={`chip ${traffic.tone}`}>
              <span className="dot" />
              {t("traffic.label", { defaultValue: "Traffic" })}: {t(`traffic.${traffic.key}`)}
            </span>
          </div>
        ) : null}

        {/* Alternative routes */}
        {opts.length > 1 && (
          <div className="nav-routes">
            {opts.map((o, i) => (
              <button
                key={o.id}
                className={`nav-route-chip ${i === selected ? "active" : ""}`}
                onClick={() => setSelected(i)}
              >
                <span className="nav-route-chip-ico">
                  <IconRoute size={18} />
                </span>
                <span>
                  <span className="nav-route-min">{o.durationMin} min</span>
                  <span className="nav-route-km">
                    {o.distanceKm} km ·{" "}
                    {i === 0
                      ? t("map.fastest", { defaultValue: "Fastest" })
                      : t("map.alt", { defaultValue: "Alternate" })}
                  </span>
                </span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
