import { useEffect } from "react";
import { api } from "@/lib/api";
import { appendAdvisories } from "@/lib/store";
import { useRealtime } from "@/hooks/RealtimeContext";
import { Empty } from "@/components/ui";
import { fmtRelative, gateShort } from "@/lib/format";
import type { Advisory } from "@/lib/types";

// Inbox — advisories, alerts and challans. Sources, newest first:
//   * live re-route advisories + alerts from the realtime feed (RealtimeContext);
//   * the gateway's recent alert history (pulled once on mount); and
//   * the 24 h IndexedDB cache (renders offline).
// Marks everything read on view so the bottom-nav badge clears.

const ICON: Record<string, string> = { reroute: "↻", challan: "₹", alert: "!" };

export default function Inbox() {
  const { advisories, markInboxRead } = useRealtime();

  useEffect(() => {
    // Pull recent alert/challan history so the inbox isn't empty on a cold open.
    api
      .alerts({ since: "PT24H", limit: 100 })
      .then((r) => {
        const rows: Advisory[] = (r.alerts || []).map((a: any) => {
          const kind = a.kind || "ALERT";
          const isChallan =
            String(kind).toUpperCase().includes("CHALLAN") ||
            a.severity === "REPORT_TO_POLICE";
          return {
            id: `alert:${a.id || a.ts}`,
            type: isChallan ? "challan" : "alert",
            ts: a.ts || new Date().toISOString(),
            title: isChallan ? "Challan / enforcement notice" : `Alert — ${kind}`,
            body: a.payload?.message || a.payload?.detail,
            severity: a.severity || "info",
            kind,
            gate_id: a.gate_id ?? null,
            plate: a.plate ?? null,
          };
        });
        if (rows.length) appendAdvisories(rows);
      })
      .catch(() => undefined);
    markInboxRead();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!advisories.length) {
    return <Empty>No advisories, alerts or challans in the last 24 hours.</Empty>;
  }

  return (
    <div className="card tight">
      {advisories.map((a) => (
        <div key={a.id} className={`inbox-item ${a.type}`}>
          <div className="icn">{ICON[a.type] ?? "•"}</div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="ttl">{a.title}</div>
            {a.body ? <div className="bd">{a.body}</div> : null}
            <div className="meta">
              {a.type === "reroute" && a.gate_id ? `→ Gate ${gateShort(a.gate_id)} · ` : ""}
              {a.plate ? `${a.plate} · ` : ""}
              {fmtRelative(a.ts)}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
