import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";

// Driver parking view — nearby parking areas (RDS-backed availability), request a
// slot, confirmation, and release. The vehicle plate (from pairing) is the
// vehicle_id used for allocation. A slot-allocated notification is also delivered
// server-side (jnpa.notifications) via the allocate flow.

type Facility = {
  facility_id: string;
  name?: string | null;
  lat?: number | null;
  lon?: number | null;
  capacity: number;
  available: number;
  occupied: number;
  free_pct?: number | null;
  status: string;
};

function haversineKm(a: { lat: number; lon: number }, b: { lat: number; lon: number }): number {
  const R = 6371;
  const dLat = ((b.lat - a.lat) * Math.PI) / 180;
  const dLon = ((b.lon - a.lon) * Math.PI) / 180;
  const s =
    Math.sin(dLat / 2) ** 2 +
    Math.cos((a.lat * Math.PI) / 180) * Math.cos((b.lat * Math.PI) / 180) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(s));
}

export default function Parking({ deviceId, plate }: { deviceId: string; plate?: string | null }) {
  const { t } = useTranslation();
  const [facilities, setFacilities] = useState<Facility[]>([]);
  const [pos, setPos] = useState<{ lat: number; lon: number } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{ facility: string; slot: string } | null>(null);
  const [active, setActive] = useState<boolean>(false);
  const [err, setErr] = useState<string | null>(null);

  const load = () => {
    api
      .parkingAvailability()
      .then((d) => setFacilities(d.facilities || []))
      .catch((e) => setErr(String(e)));
  };

  useEffect(() => {
    load();
    // Best-effort device geolocation for the "distance" column.
    if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition(
        (p) => setPos({ lat: p.coords.latitude, lon: p.coords.longitude }),
        () => setPos({ lat: 18.95, lon: 72.95 }), // fallback: port centroid
        { timeout: 4000 },
      );
    } else {
      setPos({ lat: 18.95, lon: 72.95 });
    }
  }, []);

  const withDistance = facilities
    .map((f) => ({
      ...f,
      km:
        pos && f.lat != null && f.lon != null ? haversineKm(pos, { lat: f.lat, lon: f.lon }) : null,
    }))
    .sort((a, b) => (a.km ?? 1e9) - (b.km ?? 1e9));

  const request = async (facilityId: string) => {
    if (!plate) {
      setErr(t("parking.noPlate"));
      return;
    }
    setBusy(facilityId);
    setErr(null);
    try {
      const r = await api.parkingAllocate(facilityId, plate, deviceId);
      if (r.allocated && r.slot_number) {
        setConfirm({ facility: facilityId, slot: r.slot_number });
        setActive(true);
        load();
      } else {
        setErr(r.reason === "facility_full" ? t("parking.full") : t("parking.failed"));
      }
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(null);
    }
  };

  const release = async () => {
    if (!plate) return;
    setBusy("release");
    try {
      await api.parkingRelease(plate);
      setConfirm(null);
      setActive(false);
      load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(null);
    }
  };

  const navTo = (f: Facility) => {
    if (f.lat != null && f.lon != null) {
      window.open(`https://www.google.com/maps/dir/?api=1&destination=${f.lat},${f.lon}`, "_blank");
    }
  };

  return (
    <div style={{ padding: 12 }}>
      <h2 style={{ fontSize: 16, marginBottom: 8 }}>{t("parking.title")}</h2>

      {confirm && (
        <div
          className="card"
          style={{ background: "var(--green-bg, #0d3)", padding: 12, marginBottom: 12 }}
        >
          <div style={{ fontWeight: 600 }}>{t("parking.confirmed")}</div>
          <div style={{ fontSize: 13 }}>
            {confirm.facility} · {t("parking.slot")} <b>{confirm.slot}</b>
          </div>
          <button
            className="btn"
            style={{ marginTop: 8 }}
            disabled={busy === "release"}
            onClick={release}
          >
            {busy === "release" ? "…" : t("parking.release")}
          </button>
        </div>
      )}

      {err && <div style={{ color: "var(--red, #c00)", fontSize: 13, marginBottom: 8 }}>{err}</div>}

      {withDistance.length === 0 ? (
        <div className="muted">{t("parking.loading")}</div>
      ) : (
        withDistance.map((f) => (
          <div key={f.facility_id} className="card" style={{ padding: 12, marginBottom: 8 }}>
            <div
              style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}
            >
              <div style={{ fontWeight: 600 }}>{f.name || f.facility_id}</div>
              <div
                style={{
                  fontSize: 12,
                  color: f.available > 0 ? "var(--green)" : "var(--red, #c00)",
                }}
              >
                {f.available}/{f.capacity} {t("parking.free")}
              </div>
            </div>
            <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
              {f.km != null ? `${f.km.toFixed(1)} km · ` : ""}
              {f.status}
            </div>
            <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
              <button
                className="btn"
                disabled={busy === f.facility_id || f.available <= 0 || active}
                onClick={() => request(f.facility_id)}
              >
                {busy === f.facility_id ? "…" : t("parking.request")}
              </button>
              <button className="btn btn-ghost" onClick={() => navTo(f)}>
                {t("parking.navigate")}
              </button>
            </div>
          </div>
        ))
      )}
    </div>
  );
}
