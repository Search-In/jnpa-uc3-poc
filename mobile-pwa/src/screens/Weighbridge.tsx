// D-10 — Weighbridge Locator.  GAP-SCR-05.
//
// This screen does not locate weighbridges, and that is deliberate.
//
// The word "weighbridge" appears in NONE of the 449 corpus files. The only
// weighbridge identifiers anywhere in the database are two ids in a single
// `core.weighbridge_reroute` row that is itself flagged `simulated = true`. A
// locator that dropped pins on a map would be dropping pins we invented, handed
// to a driver who would then drive to them.
//
// What the corpus DOES evidence is the WEIGHING: gate documents carry a
// verified gross mass against a terminal. So the screen answers the question
// actually behind "where do I weigh" — where weighing is recorded, and what
// weights were recorded there — and states the absence at the top rather than
// burying it.

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";
import { SkeletonCard } from "@/components/Skeleton";
import { IconPin } from "@/components/icons";

type WeighingPoint = {
  terminal: string;
  terminal_name?: string | null;
  vgm_documents: number;
  min_kg?: number | string | null;
  max_kg?: number | string | null;
  latest_doc_ts?: string | null;
};

type Absent = { type: string; why: string; would_need: string };

const kg = (v: number | string | null | undefined): string =>
  v == null ? "—" : `${Math.round(Number(v)).toLocaleString("en-IN")} kg`;

export default function Weighbridge() {
  const { t } = useTranslation();
  const [points, setPoints] = useState<WeighingPoint[]>([]);
  const [absent, setAbsent] = useState<Absent[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    api
      .weighingPoints()
      .then((d) => {
        setPoints(d.weighing_points || []);
        setAbsent(d.absent || []);
      })
      .catch(() =>
        setErr(
          t("weighbridge.loadFailed", {
            defaultValue: "Couldn't load weighing records. Check your connection.",
          }),
        ),
      )
      .finally(() => setLoaded(true));
  }, [t]);

  if (!loaded) return <SkeletonCard />;

  return (
    <div className="screen">
      <h2 className="screen-title">
        {t("weighbridge.title", { defaultValue: "Weighing" })}
      </h2>

      {err && <div className="notice notice-warn">{err}</div>}

      {/* The absence goes FIRST. A driver must not scroll past a list of
          terminals believing it is a list of weighbridges. */}
      <div className="notice notice-warn">
        <strong>
          {t("weighbridge.noneSupplied", {
            defaultValue: "No weighbridge locations were supplied.",
          })}
        </strong>
        <p>
          {t("weighbridge.noneSupplied.detail", {
            defaultValue:
              "JNPA's data does not list any weighbridge, its position or its hours. " +
              "Showing you pins would mean inventing them. Below is where weighing " +
              "was actually recorded on gate documents.",
          })}
        </p>
      </div>

      <div className="card-list">
        {points.map((p) => (
          <div className="card" key={p.terminal}>
            <div className="card-head">
              <IconPin />
              <strong>{p.terminal_name || p.terminal}</strong>
              <span className="badge">{p.terminal}</span>
            </div>
            <div className="card-meta">
              <span>
                {p.vgm_documents}{" "}
                {t("weighbridge.vgmDocs", { defaultValue: "weighed documents" })}
              </span>
              <span>
                {kg(p.min_kg)} – {kg(p.max_kg)}
              </span>
            </div>
            {p.latest_doc_ts && (
              <div className="hint">
                {t("weighbridge.latest", { defaultValue: "Most recent" })}:{" "}
                {new Date(p.latest_doc_ts).toLocaleString("en-IN")}
              </div>
            )}
          </div>
        ))}
      </div>

      {points.length === 0 && (
        <div className="notice notice-info">
          {t("weighbridge.noWeighing", {
            defaultValue:
              "No gate document in the data carries a verified gross mass.",
          })}
        </div>
      )}

      {absent.map((a) => (
        <div className="notice notice-info" key={a.type} style={{ marginTop: 12 }}>
          <strong>{t("weighbridge.wouldNeed", { defaultValue: "To show this properly we would need" })}</strong>
          <p>{a.would_need}</p>
        </div>
      ))}
    </div>
  );
}
