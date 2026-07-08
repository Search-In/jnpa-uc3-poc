import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";

// Driver geo-fence awareness — pushes the device location to the DB-driven
// geo-fence engine and surfaces: current zone(s), allowed vs restricted zones,
// and live warnings (entering a restricted zone, no-parking / dwell exceeded).
// The engine persists every transition to jnpa.geofence_events server-side.

type Zone = { id: string; name: string; kind: string };
type Warning = { text: string; kind: string; at: number };

export default function Zones({ deviceId, plate }: { deviceId: string; plate?: string | null }) {
  const { t } = useTranslation();
  const [zones, setZones] = useState<Zone[]>([]);
  const [inside, setInside] = useState<Zone[]>([]);
  const [warnings, setWarnings] = useState<Warning[]>([]);
  const [pos, setPos] = useState<{ lat: number; lon: number } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const watchRef = useRef<number | null>(null);

  useEffect(() => {
    api.geoZones().then((d) => setZones(d.zones || [])).catch(() => {});
  }, []);

  // Evaluate the current position against the engine, mapping events to warnings.
  const evaluate = async (lat: number, lon: number) => {
    if (!plate) return;
    try {
      const r = await api.geoEvaluate(plate, lat, lon, deviceId);
      setInside(r.inside_zones || []);
      const newWarns: Warning[] = [];
      for (const e of r.events || []) {
        if (e.event === "RESTRICTED_ENTRY")
          newWarns.push({ text: t("zones.warnRestricted", { zone: e.zone_id }), kind: "restricted", at: Date.now() });
        else if (e.event === "NO_PARKING_VIOLATION")
          newWarns.push({ text: t("zones.warnNoParking", { zone: e.zone_id }), kind: "noparking", at: Date.now() });
        else if (e.event === "ENTER")
          newWarns.push({ text: t("zones.enter", { zone: e.zone_id }), kind: "enter", at: Date.now() });
      }
      if (newWarns.length) {
        setWarnings((w) => [...newWarns, ...w].slice(0, 10));
        if (navigator.vibrate) navigator.vibrate(200);
      }
    } catch (e) {
      setErr(String(e));
    }
  };

  useEffect(() => {
    if (!navigator.geolocation) {
      setPos({ lat: 18.95, lon: 72.95 });
      return;
    }
    watchRef.current = navigator.geolocation.watchPosition(
      (p) => {
        const c = { lat: p.coords.latitude, lon: p.coords.longitude };
        setPos(c);
        void evaluate(c.lat, c.lon);
      },
      () => setErr(t("zones.noLocation")),
      { enableHighAccuracy: true, maximumAge: 10000, timeout: 8000 },
    );
    return () => {
      if (watchRef.current != null) navigator.geolocation.clearWatch(watchRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plate]);

  const restricted = zones.filter((z) => z.kind === "restricted");
  const allowed = zones.filter((z) => z.kind !== "restricted");

  return (
    <div style={{ padding: 12 }}>
      <h2 style={{ fontSize: 16, marginBottom: 8 }}>{t("zones.title")}</h2>

      <div className="card" style={{ padding: 12, marginBottom: 12 }}>
        <div className="muted" style={{ fontSize: 12 }}>{t("zones.current")}</div>
        {inside.length ? (
          inside.map((z) => (
            <div key={z.id} style={{ fontWeight: 600, color: z.kind === "restricted" ? "var(--red, #c00)" : "var(--amber, #d80)" }}>
              {z.name} · {z.kind}
            </div>
          ))
        ) : (
          <div style={{ fontWeight: 600, color: "var(--green)" }}>{t("zones.clear")}</div>
        )}
        {pos && <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>{pos.lat.toFixed(5)}, {pos.lon.toFixed(5)}</div>}
      </div>

      {warnings.length > 0 && (
        <div className="card" style={{ padding: 12, marginBottom: 12, borderLeft: "3px solid var(--red, #c00)" }}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>⚠ {t("zones.alerts")}</div>
          {warnings.map((w, i) => (
            <div key={i} style={{ fontSize: 13, color: w.kind === "restricted" || w.kind === "noparking" ? "var(--red, #c00)" : undefined }}>
              {w.text}
            </div>
          ))}
        </div>
      )}

      {err && <div style={{ color: "var(--red, #c00)", fontSize: 13, marginBottom: 8 }}>{err}</div>}
      {!plate && <div className="muted" style={{ fontSize: 13, marginBottom: 8 }}>{t("zones.noPlate")}</div>}

      <div className="card" style={{ padding: 12, marginBottom: 8 }}>
        <div style={{ fontWeight: 600, color: "var(--red, #c00)", marginBottom: 4 }}>{t("zones.restricted")} ({restricted.length})</div>
        {restricted.map((z) => <div key={z.id} style={{ fontSize: 13 }}>{z.name}</div>)}
      </div>
      <div className="card" style={{ padding: 12 }}>
        <div style={{ fontWeight: 600, marginBottom: 4 }}>{t("zones.noParkingZones")} ({allowed.length})</div>
        {allowed.map((z) => <div key={z.id} style={{ fontSize: 13 }}>{z.name}</div>)}
      </div>
    </div>
  );
}
