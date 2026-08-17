// D-07 — Document Wallet.  GAP-SCR-04.  Unblocks flow F-07.
//
// What a driver at a gate actually needs: which document authorises this move,
// what it says the box is, and whether the scan exists. Not a document browser —
// the endpoint behind this is scoped through the job's ownership check, so a
// driver reaches only their own job's papers and a probe for another job id
// gets the same 404 as a job that does not exist.
//
// Two things it will not do.
//
// It does not show `driver_name` or `driver_licence`, even though the gate
// document carries both. This screen is reachable with a DRIVER token; showing
// the whole row would hand every driver the licence number of whoever last
// moved that container.
//
// And when there is no document it says so, in words. That is the COMMON case:
// JNPA's manifest files and gate-document files describe different containers,
// so most boxes have no gate paperwork at all. An empty list here would read as
// a failed load and send a driver looking for a problem that isn't theirs.

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams, useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { SkeletonCard } from "@/components/Skeleton";
import { IconFile, IconTruck } from "@/components/icons";

type Doc = {
  doc_id: number | string;
  doc_category?: string | null;
  doc_variant?: string | null;
  doc_ref?: string | null;
  pin_no?: string | null;
  doc_ts?: string | null;
  container_no?: string | null;
  iso_code?: string | null;
  load_status?: string | null;
  gross_weight_kg?: number | string | null;
  seal1?: string | null;
  seal2?: string | null;
  vehicle_no?: string | null;
  bat_no?: string | null;
  gate_no?: string | null;
  yard_position?: string | null;
  vessel_name?: string | null;
  voyage?: string | null;
  pod?: string | null;
  booking_no?: string | null;
  cfs?: string | null;
  truck_in_ts?: string | null;
  truck_out_ts?: string | null;
  image_file?: string | null;
  data_origin?: string | null;
};

type Wallet = {
  job_id: number;
  container_no?: string | null;
  vehicle_no?: string | null;
  documents: Doc[];
  count: number;
  matched_by_container: number;
  matched_by_vehicle: number;
  note?: string | null;
};

const field = (label: string, v: unknown) =>
  v == null || v === "" ? null : (
    <div className="kv" key={label}>
      <span className="kv-k">{label}</span>
      <span className="kv-v">{String(v)}</span>
    </div>
  );

export default function Documents() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { jobId } = useParams();
  const [w, setW] = useState<Wallet | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (!jobId) return;
    api
      .jobDocuments(jobId)
      .then(setW)
      .catch(() =>
        setErr(
          t("documents.loadFailed", {
            defaultValue: "Couldn't load documents. Check your connection.",
          }),
        ),
      )
      .finally(() => setLoaded(true));
  }, [jobId, t]);

  if (!loaded) return <SkeletonCard />;

  return (
    <div className="screen">
      <h2 className="screen-title">
        {t("documents.title", { defaultValue: "Documents" })}
      </h2>

      {err && <div className="notice notice-warn">{err}</div>}

      {w && (
        <div className="card">
          <div className="card-head">
            <IconTruck />
            <strong>{w.container_no || t("documents.noContainer", { defaultValue: "No container" })}</strong>
            {w.vehicle_no ? <span className="badge">{w.vehicle_no}</span> : null}
          </div>
          <div className="card-meta">
            <span>
              {w.count} {t("documents.docs", { defaultValue: "documents" })}
            </span>
            {w.matched_by_container > 0 && (
              <span>
                {w.matched_by_container}{" "}
                {t("documents.byContainer", { defaultValue: "for this container" })}
              </span>
            )}
            {/* Worth stating plainly: a document found via the TRUCK is not
                necessarily about the box currently on it. */}
            {w.matched_by_vehicle > 0 && (
              <span>
                {w.matched_by_vehicle}{" "}
                {t("documents.byVehicle", { defaultValue: "for this truck (other moves)" })}
              </span>
            )}
          </div>
        </div>
      )}

      {w?.note && <div className="notice notice-info">{w.note}</div>}

      <div className="card-list">
        {(w?.documents ?? []).map((d) => (
          <div className="card" key={String(d.doc_id)}>
            <div className="card-head">
              <IconFile />
              <strong>{d.doc_category || t("documents.document", { defaultValue: "Document" })}</strong>
              {d.doc_variant ? <span className="badge">{d.doc_variant}</span> : null}
              {d.data_origin && d.data_origin !== "REAL" ? (
                <span className="badge badge-warn">{d.data_origin}</span>
              ) : null}
            </div>

            <div className="kv-grid">
              {field(t("documents.ref", { defaultValue: "Reference" }), d.doc_ref)}
              {field("PIN", d.pin_no)}
              {field(t("documents.container", { defaultValue: "Container" }), d.container_no)}
              {field("ISO", d.iso_code)}
              {field(t("documents.load", { defaultValue: "Load" }), d.load_status)}
              {field(
                t("documents.vgm", { defaultValue: "Verified gross mass" }),
                d.gross_weight_kg == null
                  ? null
                  : `${Math.round(Number(d.gross_weight_kg)).toLocaleString("en-IN")} kg`,
              )}
              {field(t("documents.seal", { defaultValue: "Seal" }), d.seal1 || d.seal2)}
              {field(t("documents.gate", { defaultValue: "Gate" }), d.gate_no)}
              {field(t("documents.yard", { defaultValue: "Yard position" }), d.yard_position)}
              {field("BAT", d.bat_no)}
              {field(t("documents.vessel", { defaultValue: "Vessel" }), d.vessel_name)}
              {field(t("documents.voyage", { defaultValue: "Voyage" }), d.voyage)}
              {field("POD", d.pod)}
              {field(t("documents.booking", { defaultValue: "Booking" }), d.booking_no)}
              {field("CFS", d.cfs)}
              {field(
                t("documents.truckIn", { defaultValue: "Truck in" }),
                d.truck_in_ts ? new Date(d.truck_in_ts).toLocaleString("en-IN") : null,
              )}
              {field(
                t("documents.truckOut", { defaultValue: "Truck out" }),
                d.truck_out_ts ? new Date(d.truck_out_ts).toLocaleString("en-IN") : null,
              )}
            </div>

            {d.image_file ? (
              <div className="hint">
                {t("documents.scanHeld", { defaultValue: "Original scan on file" })}:{" "}
                <code>{d.image_file}</code>
              </div>
            ) : (
              <div className="hint">
                {t("documents.noScan", { defaultValue: "No scan of this document was supplied." })}
              </div>
            )}
          </div>
        ))}
      </div>

      <button className="btn btn-ghost" onClick={() => navigate("/jobs")}>
        {t("documents.backToJobs", { defaultValue: "Back to jobs" })}
      </button>
    </div>
  );
}
