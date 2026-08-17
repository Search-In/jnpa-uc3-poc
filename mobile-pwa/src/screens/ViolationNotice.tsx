// D-12 — Violation Notice.  GAP-SCR-07.  Unblocks flow F-08.
//
// Renders the `violation_enforced` frame the gateway already addresses to this
// driver's device. It was emitted and nothing on the driver side read it, so an
// enforcement action reached the control room and the driver learned nothing.
//
// THE DISCLOSURE IS NOT OPTIONAL FURNITURE.
//
// Every challan this system produces carries `badge: "SIMULATED"`,
// `is_legal_instrument: false` and an authority note, attached at the system of
// record (gateway/enforcement.py) precisely so no screen can forget it. A driver
// shown a case number and a rupee figure with no such marking would reasonably
// conclude they had been fined by a competent authority. So the badge renders
// above the amount, not below it, and the screen refuses to show a fine at all
// if the disclosure is missing from the payload — a notice we cannot mark is a
// notice we must not present.

import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { useRealtime } from "@/hooks/RealtimeContext";
import { IconBell, IconFile } from "@/components/icons";

const rupees = (n: unknown): string =>
  n == null || Number.isNaN(Number(n))
    ? "—"
    : `₹${Number(n).toLocaleString("en-IN")}`;

export default function ViolationNotice() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { violation: v, dismissViolation } = useRealtime();

  if (!v) {
    return (
      <div className="screen">
        <h2 className="screen-title">
          {t("violation.title", { defaultValue: "Enforcement notice" })}
        </h2>
        <div className="notice notice-info">
          {t("violation.none", {
            defaultValue: "You have no enforcement notice.",
          })}
        </div>
        <button className="btn btn-ghost" onClick={() => navigate("/home")}>
          {t("common.back", { defaultValue: "Back" })}
        </button>
      </div>
    );
  }

  // A notice whose provenance we cannot state is a notice we do not present as
  // a fine. This is the guard the per-screen-disclosure approach kept losing.
  const marked = v.badge != null || v.issuance_mode != null;

  return (
    <div className="screen">
      <h2 className="screen-title">
        {t("violation.title", { defaultValue: "Enforcement notice" })}
      </h2>

      {marked ? (
        <div className="notice notice-warn">
          <strong>
            {v.badge ?? v.issuance_mode}
            {v.is_legal_instrument === false
              ? ` — ${t("violation.notLegal", {
                  defaultValue: "not a legal instrument",
                })}`
              : ""}
          </strong>
          {v.authority_note && <p>{v.authority_note}</p>}
          {v.disclosure && <p>{v.disclosure}</p>}
          {v.assumption_ref && (
            <p className="hint">
              {t("violation.assumption", { defaultValue: "Assumption" })}: {v.assumption_ref}
            </p>
          )}
        </div>
      ) : (
        <div className="notice notice-warn">
          <strong>
            {t("violation.unmarked", {
              defaultValue: "This notice arrived without its issuance marking.",
            })}
          </strong>
          <p>
            {t("violation.unmarked.detail", {
              defaultValue:
                "The amount is withheld because we cannot state whether this is a " +
                "demonstration or a real instrument. Report this to the control room.",
            })}
          </p>
        </div>
      )}

      <div className="card">
        <div className="card-head">
          <IconBell />
          <strong>
            {t("violation.case", { defaultValue: "Case" })} {String(v.case_id)}
          </strong>
          {v.status ? <span className="badge">{v.status}</span> : null}
        </div>

        <div className="kv-grid">
          <div className="kv">
            <span className="kv-k">{t("violation.vehicle", { defaultValue: "Vehicle" })}</span>
            <span className="kv-v mono">{v.plate || v.vehicle || "—"}</span>
          </div>
          {v.challan_no && (
            <div className="kv">
              <span className="kv-k">{t("violation.challan", { defaultValue: "Challan" })}</span>
              <span className="kv-v mono">{v.challan_no}</span>
            </div>
          )}
          {v.ts && (
            <div className="kv">
              <span className="kv-k">{t("violation.issued", { defaultValue: "Issued" })}</span>
              <span className="kv-v">{new Date(v.ts).toLocaleString("en-IN")}</span>
            </div>
          )}
        </div>
      </div>

      {/* The breakdown, so the driver can see WHAT was recorded rather than
          only a total they cannot check. */}
      {Array.isArray(v.violations) && v.violations.length > 0 && (
        <div className="card">
          <div className="card-head">
            <IconFile />
            <strong>{t("violation.breakdown", { defaultValue: "What was recorded" })}</strong>
          </div>
          {v.violations.map((b, i) => (
            <div className="row between" key={`${b.code ?? i}`}>
              <span>
                {b.label || b.code}
                {b.count && b.count > 1 ? ` ×${b.count}` : ""}
              </span>
              <span className="mono">{marked ? rupees(b.fine) : "—"}</span>
            </div>
          ))}
          <div className="row between" style={{ fontWeight: 600 }}>
            <span>{t("violation.total", { defaultValue: "Total" })}</span>
            <span className="mono">{marked ? rupees(v.fine) : "—"}</span>
          </div>
        </div>
      )}

      {v.evidence_url && (
        <a className="btn btn-ghost" href={v.evidence_url} target="_blank" rel="noreferrer">
          {t("violation.evidence", { defaultValue: "View evidence" })}
        </a>
      )}

      <button
        className="btn btn-primary"
        onClick={() => {
          dismissViolation();
          navigate("/home");
        }}
      >
        {t("violation.acknowledge", { defaultValue: "Acknowledge" })}
      </button>
    </div>
  );
}
