import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";
import { Card, Chip, Row, Spinner } from "@/components/ui";
import { clearPairing, deviceIdToCode } from "@/lib/device";
import { enablePush, type PushState } from "@/lib/pwa";
import i18n, { SUPPORTED_LANGS, LANG_LABELS } from "@/i18n";
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
  const { t } = useTranslation();
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
      <Card title={t("common.language")}>
        <label
          htmlFor="lang-select"
          className="muted"
          style={{ fontSize: 13, display: "block", marginBottom: 6 }}
        >
          {t("common.language")}
        </label>
        <select
          id="lang-select"
          className="lang-select"
          data-testid="lang-select"
          value={i18n.resolvedLanguage}
          onChange={(e) => void i18n.changeLanguage(e.target.value)}
          style={{ width: "100%", padding: 10, fontSize: 15 }}
        >
          {SUPPORTED_LANGS.map((code) => (
            <option key={code} value={code}>
              {LANG_LABELS[code]}
            </option>
          ))}
        </select>
      </Card>

      <Card title={t("profile.driverDevice")}>
        <Row k={t("profile.deviceId")} v={deviceId} />
        <Row k={t("profile.pairingCode")} v={deviceIdToCode(deviceId)} />
        <Row k={t("common.plate")} v={resolvedPlate ?? t("common.noData")} />
      </Card>

      <Card title={t("profile.vehicleVahan")}>
        {loading ? (
          <div className="muted" style={{ fontSize: 13 }}>
            <Spinner /> {t("profile.resolvingRc")}
          </div>
        ) : vahan ? (
          <>
            <div style={{ marginBottom: 10 }}>
              <Chip status={provisional ? "warn" : path?.startsWith("LIVE") ? "ok" : "warn"}>
                {path}
              </Chip>
            </div>
            <Row
              k={t("profile.owner")}
              v={rc.owner_name_masked || rc.owner_name || t("common.noData")}
            />
            {/* Maker/model is not part of the gateway VahanRecord contract, so the
                row was removed. Vehicle class (below) is the canonical descriptor. */}
            <Row
              k={t("profile.class")}
              v={rc.vehicle_class || rc.vehicle_category || t("common.noData")}
            />
            <Row k={t("profile.fuel")} v={rc.fuel_type || t("common.noData")} />
            <Row
              k={t("profile.rcStatus")}
              v={
                rc.blacklist_status ||
                rc.rc_status ||
                (provisional ? "PROVISIONAL" : t("common.noData"))
              }
            />
            {/* Canonical backend field is *_valid_to; legacy aliases kept for
                backward compatibility with older gateway responses. */}
            <Row
              k={t("profile.insuranceUpto")}
              v={
                rc.insurance_valid_to ||
                rc.insurance_upto ||
                rc.insurance_validity ||
                t("common.noData")
              }
            />
            <Row
              k={t("profile.fitnessUpto")}
              v={rc.fitness_valid_to || rc.fitness_upto || t("common.noData")}
            />
            {provisional ? (
              <div className="banner warn" style={{ marginTop: 10 }}>
                {t("profile.provisionalNote")}
              </div>
            ) : null}
          </>
        ) : (
          <div className="muted" style={{ fontSize: 13 }}>
            {t("profile.noVahan")}
          </div>
        )}
      </Card>

      <Card title={t("profile.notifications")}>
        <button className="btn" disabled={pushBusy || push === "subscribed"} onClick={onEnablePush}>
          {pushBusy
            ? t("profile.enabling")
            : push === "subscribed"
              ? t("profile.pushEnabled")
              : t("profile.enablePush")}
        </button>
        {push ? (
          <div className="muted" style={{ fontSize: 12, marginTop: 8, textAlign: "center" }}>
            {PUSH_LABEL[push]}
          </div>
        ) : (
          <div className="muted" style={{ fontSize: 12, marginTop: 8, textAlign: "center" }}>
            {t("profile.pushNote")}
          </div>
        )}
      </Card>

      <Card title={t("profile.session")}>
        <button
          className="btn ghost"
          onClick={() => {
            clearPairing();
            location.reload();
          }}
        >
          {t("profile.unpair")}
        </button>
      </Card>
    </>
  );
}
