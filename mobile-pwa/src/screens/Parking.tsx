import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";
import { SkeletonCard } from "@/components/Skeleton";
import { IconParking, IconNavigate, IconPin } from "@/components/icons";

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
  const [loaded, setLoaded] = useState(false);

  const load = () => {
    api
      .parkingAvailability()
      .then((d) => setFacilities(d.facilities || []))
      .catch(() =>
        setErr(t("parking.loadFailed", { defaultValue: "Couldn't load parking. Check your connection." })),
      )
      .finally(() => setLoaded(true));
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
    } catch {
      setErr(t("parking.failed"));
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
    } catch {
      setErr(t("parking.failed"));
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
    <div>
      <h2 style={{ fontSize: 20, fontWeight: 800, margin: "2px 0 12px" }}>{t("parking.title")}</h2>

      {confirm && (
        <div className="pk-confirm">
          <div className="pk-confirm-badge">
            <IconParking size={22} />
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 800, fontSize: 15 }}>{t("parking.confirmed")}</div>
            <div style={{ fontSize: 13.5, opacity: 0.9 }}>
              {t("parking.slot")} <b>{confirm.slot}</b>
            </div>
          </div>
          <button className="pk-release" disabled={busy === "release"} onClick={release}>
            {busy === "release" ? "…" : t("parking.release")}
          </button>
        </div>
      )}

      {err && <div className="banner warn">{err}</div>}

      {!loaded && withDistance.length === 0 ? (
        <>
          <SkeletonCard lines={2} />
          <SkeletonCard lines={2} />
        </>
      ) : withDistance.length === 0 ? (
        <div className="empty">
          <div style={{ marginBottom: 8, color: "var(--muted)" }}>
            <IconParking size={38} />
          </div>
          {t("parking.none", { defaultValue: "No parking areas available nearby right now." })}
        </div>
      ) : (
        withDistance.map((f) => {
          const pct = f.capacity > 0 ? Math.round((f.available / f.capacity) * 100) : 0;
          const tone = f.available <= 0 ? "down" : pct < 20 ? "warn" : "ok";
          return (
            <div key={f.facility_id} className="pk-card">
              <div className="pk-card-head">
                <span className="pk-card-ico">
                  <IconParking size={22} />
                </span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="pk-name">{f.name || f.facility_id}</div>
                  <div className="pk-meta">
                    {f.km != null ? (
                      <>
                        <IconPin size={13} /> {f.km.toFixed(1)} km
                      </>
                    ) : null}
                  </div>
                </div>
                <div className={`pk-avail ${tone}`}>
                  <span className="pk-avail-n">{f.available}</span>
                  <span className="pk-avail-l">{t("parking.free")}</span>
                </div>
              </div>

              {/* availability bar */}
              <div className="pk-bar">
                <span className={`pk-bar-fill ${tone}`} style={{ width: `${Math.max(4, pct)}%` }} />
              </div>

              <div className="pk-actions">
                <button
                  className="btn primary"
                  disabled={busy === f.facility_id || f.available <= 0 || active}
                  onClick={() => request(f.facility_id)}
                >
                  {busy === f.facility_id ? "…" : t("parking.request")}
                </button>
                <button className="btn" onClick={() => navTo(f)}>
                  <IconNavigate size={17} /> {t("parking.navigate")}
                </button>
              </div>
            </div>
          );
        })
      )}
    </div>
  );
}
