import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { useRealtime } from "@/hooks/RealtimeContext";
import { gateShort } from "@/lib/format";
import { Empty } from "@/components/ui";

// Re-route — full-screen confirmation shown when a /api/trucks/{id}/route push
// arrives (via WebPush, the WS reroute frame, or the polling fallback). "Accept"
// sends state=ACK back to the gateway (POST .../route/ack); "Not now" sends
// DECLINE. Either way the banner clears so the driver isn't blocked.

export default function Reroute() {
  const { t } = useTranslation();
  const { pendingReroute, ackReroute } = useRealtime();
  const navigate = useNavigate();
  const [busy, setBusy] = useState<"ACK" | "DECLINE" | null>(null);

  if (!pendingReroute) {
    return <Empty>{t("reroute.empty")}</Empty>;
  }

  const r = pendingReroute;
  const act = async (state: "ACK" | "DECLINE") => {
    setBusy(state);
    await ackReroute(state);
    setBusy(null);
    navigate("/trip");
  };

  return (
    <div
      className="reroute-screen"
      data-testid="reroute-screen"
      role="alertdialog"
      aria-live="assertive"
    >
      <div className="pulse">↻</div>
      <h2>{t("reroute.title")}</h2>
      <p className="lead">{r.reason || t("reroute.defaultReason")}</p>

      <div className="dest">
        <div
          className="muted"
          style={{ fontSize: 12, textTransform: "uppercase", letterSpacing: 0.6 }}
        >
          {t("reroute.proceedTo")}
        </div>
        <div className="g">{gateShort(r.gate_id) || t("reroute.newDestination")}</div>
        {r.route_km != null ? (
          <div className="muted" style={{ fontSize: 13 }}>
            {t("reroute.kmRerouted", { km: r.route_km.toFixed(1) })}
          </div>
        ) : null}
      </div>

      <div className="actions">
        <button
          className="btn success"
          data-testid="reroute-accept"
          disabled={busy !== null}
          onClick={() => act("ACK")}
        >
          {busy === "ACK" ? t("reroute.sendingAck") : t("reroute.accept")}
        </button>
        <button className="btn ghost" disabled={busy !== null} onClick={() => act("DECLINE")}>
          {busy === "DECLINE" ? "…" : t("reroute.decline")}
        </button>
      </div>
    </div>
  );
}
