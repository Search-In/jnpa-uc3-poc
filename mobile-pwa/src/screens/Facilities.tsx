// D-11 — Facilities & Utilities (driver side).  GAP-SCR-06.
//
// The same directory the control room sees (T-09), re-cut for someone holding a
// phone in a cab: nearest first when the device gives us a position, and the
// stated absences shown rather than hidden.
//
// Showing the absences matters more here than on the desk screen. A driver who
// opens "Facilities" looking for a rest stop and finds a list of container
// terminals will assume the app is broken. Telling them plainly that amenities
// were never supplied is the difference between a limitation and a fault.

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";
import { SkeletonCard } from "@/components/Skeleton";
import { IconPin, IconNavigate } from "@/components/icons";

type Facility = {
  facility_id: string;
  type: string;
  name: string;
  operator?: string | null;
  site_code?: string | null;
  lat?: number | null;
  lon?: number | null;
  capacity?: number | null;
  berth_count?: number | null;
  dwell_hours?: string | number | null;
  source_table: string;
  source_files?: string;
};

type Absent = { type: string; why: string; would_need: string };

const TYPE_LABEL: Record<string, string> = {
  TERMINAL: "Terminal",
  CFS: "CFS",
  ICD: "ICD",
  RAIL_SIDING: "Rail siding",
  CPP: "Parking",
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

export default function Facilities() {
  const { t } = useTranslation();
  const [rows, setRows] = useState<Facility[]>([]);
  const [absent, setAbsent] = useState<Absent[]>([]);
  const [pos, setPos] = useState<{ lat: number; lon: number } | null>(null);
  const [filter, setFilter] = useState<string>("");
  const [err, setErr] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    api
      .facilities()
      .then((d) => {
        setRows(d.facilities || []);
        setAbsent(d.absent || []);
      })
      .catch(() =>
        setErr(
          t("facilities.loadFailed", {
            defaultValue: "Couldn't load facilities. Check your connection.",
          }),
        ),
      )
      .finally(() => setLoaded(true));

    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition(
      (p) => setPos({ lat: p.coords.latitude, lon: p.coords.longitude }),
      // Position is a nicety: without it the list still works, just unsorted by
      // distance. Never block the screen on it.
      () => undefined,
      { enableHighAccuracy: false, timeout: 6000, maximumAge: 300000 },
    );
  }, [t]);

  const shown = useMemo(() => {
    const sel = filter ? rows.filter((r) => r.type === filter) : rows;
    if (!pos) return sel;
    // Only rows with coordinates can be ranked. The rest keep their order and
    // sit after, rather than being dropped or given a fabricated distance.
    const withPos = sel.filter((r) => r.lat != null && r.lon != null);
    const without = sel.filter((r) => r.lat == null || r.lon == null);
    withPos.sort(
      (a, b) =>
        haversineKm(pos, { lat: a.lat!, lon: a.lon! }) -
        haversineKm(pos, { lat: b.lat!, lon: b.lon! }),
    );
    return [...withPos, ...without];
  }, [rows, filter, pos]);

  const types = useMemo(() => {
    const c: Record<string, number> = {};
    rows.forEach((r) => (c[r.type] = (c[r.type] || 0) + 1));
    return c;
  }, [rows]);

  if (!loaded) return <SkeletonCard />;

  return (
    <div className="screen">
      <h2 className="screen-title">
        {t("facilities.title", { defaultValue: "Facilities & Utilities" })}
      </h2>

      {err && <div className="notice notice-warn">{err}</div>}

      <div className="chip-row">
        <button
          type="button"
          className={`chip ${filter === "" ? "chip-on" : ""}`}
          onClick={() => setFilter("")}
        >
          {t("facilities.all", { defaultValue: "All" })} ({rows.length})
        </button>
        {Object.entries(types).map(([k, n]) => (
          <button
            key={k}
            type="button"
            className={`chip ${filter === k ? "chip-on" : ""}`}
            onClick={() => setFilter(k)}
          >
            {TYPE_LABEL[k] ?? k} ({n})
          </button>
        ))}
      </div>

      {!pos && (
        <p className="hint">
          {t("facilities.noPosition", {
            defaultValue:
              "Location is off, so these are not sorted by distance.",
          })}
        </p>
      )}

      <div className="card-list">
        {shown.map((f, i) => {
          const km =
            pos && f.lat != null && f.lon != null
              ? haversineKm(pos, { lat: f.lat, lon: f.lon })
              : null;
          return (
            <div className="card" key={`${f.type}-${f.facility_id}-${i}`}>
              <div className="card-head">
                <IconPin />
                <strong>{f.name}</strong>
                <span className="badge">{TYPE_LABEL[f.type] ?? f.type}</span>
              </div>
              <div className="card-meta">
                {f.operator ? <span>{f.operator}</span> : null}
                {f.berth_count ? (
                  <span>
                    {f.berth_count} {t("facilities.berths", { defaultValue: "berths" })}
                  </span>
                ) : null}
                {f.capacity ? (
                  <span>
                    {f.capacity} {t("facilities.slots", { defaultValue: "slots" })}
                  </span>
                ) : null}
                {km != null ? <span>{km.toFixed(1)} km</span> : null}
              </div>
              {f.lat != null && f.lon != null && (
                <a
                  className="btn btn-ghost"
                  href={`https://www.google.com/maps/dir/?api=1&destination=${f.lat},${f.lon}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  <IconNavigate />
                  {t("facilities.navigate", { defaultValue: "Navigate" })}
                </a>
              )}
              {f.lat == null && (
                <div className="hint">
                  {t("facilities.noCoords", {
                    defaultValue: "No coordinates were supplied for this facility.",
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Stated absences. A driver who cannot find a rest stop needs to know it
          was never in the data, not wonder whether the app failed. */}
      {absent.length > 0 && (
        <div className="notice notice-info" style={{ marginTop: 12 }}>
          <strong>
            {t("facilities.notSupplied", { defaultValue: "Not in the supplied data" })}
          </strong>
          <ul>
            {absent.map((a) => (
              <li key={a.type}>
                <b>{a.type}</b> — {a.why}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
