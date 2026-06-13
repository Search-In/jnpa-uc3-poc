import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, Chip, Row, Spinner } from "@/components/ui";
import { clearPairing, deviceIdToCode } from "@/lib/device";
import { enablePush, type PushState } from "@/lib/pwa";
import type { TruckEnvelope, VahanEnvelope } from "@/lib/types";

// Profile / Vehicle — pulls the VahanRecord through the gateway's orchestrated
// chain (LIVE_PRIMARY / LIVE_FALLBACK / CACHED / PROVISIONAL) for the truck's
// plate. Also hosts the WebPush enable toggle and the unpair action.

const PUSH_LABEL: Record<PushState, string> = {
  subscribed: "Push enabled",
  denied: "Notifications blocked",
  unsupported: "Push unsupported",
  "not-configured": "Push not configured (using live feed)",
  error: "Push unavailable (using live feed)",
};

export default function Profile({ deviceId, plate }: { deviceId: string; plate?: string | null }) {
  const [vahan, setVahan] = useState<VahanEnvelope | null>(null);
  const [resolvedPlate, setResolvedPlate] = useState<string | null>(plate ?? null);
  const [loading, setLoading] = useState(true);
  const [push, setPush] = useState<PushState | null>(null);
  const [pushBusy, setPushBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      let p = plate ?? null;
      if (!p) {
        // Resolve the plate from the live device snapshot first.
        try {
          const env: TruckEnvelope = await api.truck(deviceId);
          p = env.record.plate ?? null;
        } catch {
          /* device not yet known */
        }
      }
      if (alive) setResolvedPlate(p);
      if (p) {
        try {
          const v = await api.vahanRc(p);
          if (alive) setVahan(v);
        } catch {
          /* vahan unreachable */
        }
      }
      if (alive) setLoading(false);
    })();
    return () => {
      alive = false;
    };
  }, [deviceId, plate]);

  const onEnablePush = async () => {
    setPushBusy(true);
    const state = await enablePush(deviceId);
    setPush(state);
    setPushBusy(false);
  };

  const rc = vahan?.record ?? {};
  const path = vahan?.decision_path;
  const provisional = path === "PROVISIONAL" || vahan?.provisional;

  return (
    <>
      <Card title="Driver / Device">
        <Row k="Device ID" v={deviceId} />
        <Row k="Pairing code" v={deviceIdToCode(deviceId)} />
        <Row k="Plate" v={resolvedPlate ?? "—"} />
      </Card>

      <Card title="Vehicle (Vahan)">
        {loading ? (
          <div className="muted" style={{ fontSize: 13 }}>
            <Spinner /> Resolving RC via gateway…
          </div>
        ) : vahan ? (
          <>
            <div style={{ marginBottom: 10 }}>
              <Chip status={provisional ? "warn" : path?.startsWith("LIVE") ? "ok" : "warn"}>
                {path}
              </Chip>
            </div>
            <Row k="Owner" v={rc.owner_name_masked || rc.owner_name || "—"} />
            <Row k="Maker / model" v={[rc.maker, rc.model].filter(Boolean).join(" ") || "—"} />
            <Row k="Class" v={rc.vehicle_class || rc.vehicle_category || "—"} />
            <Row k="Fuel" v={rc.fuel_type || "—"} />
            <Row k="RC status" v={rc.rc_status || (provisional ? "PROVISIONAL" : "—")} />
            <Row k="Insurance upto" v={rc.insurance_upto || rc.insurance_validity || "—"} />
            <Row k="Fitness upto" v={rc.fitness_upto || "—"} />
            {provisional ? (
              <div className="banner warn" style={{ marginTop: 10 }}>
                Admitted provisionally — present documents within the 24 h cure window.
              </div>
            ) : null}
          </>
        ) : (
          <div className="muted" style={{ fontSize: 13 }}>
            No Vahan record available for this vehicle.
          </div>
        )}
      </Card>

      <Card title="Notifications">
        <button className="btn" disabled={pushBusy || push === "subscribed"} onClick={onEnablePush}>
          {pushBusy ? "Enabling…" : push === "subscribed" ? "Push enabled ✓" : "Enable re-route push"}
        </button>
        {push ? (
          <div className="muted" style={{ fontSize: 12, marginTop: 8, textAlign: "center" }}>
            {PUSH_LABEL[push]}
          </div>
        ) : (
          <div className="muted" style={{ fontSize: 12, marginTop: 8, textAlign: "center" }}>
            Re-routes also arrive live over the in-app feed.
          </div>
        )}
      </Card>

      <Card title="Session">
        <button
          className="btn ghost"
          onClick={() => {
            clearPairing();
            location.reload();
          }}
        >
          Unpair this device
        </button>
      </Card>
    </>
  );
}
