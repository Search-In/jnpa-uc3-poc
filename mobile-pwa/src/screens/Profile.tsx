import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";
import { Card, Chip, Row, Spinner } from "@/components/ui";
import { clearPairing } from "@/lib/device";
import { enablePush, type PushState } from "@/lib/pwa";
import { useDriverSession } from "@/hooks/DriverSession";
import { verifiedLabel } from "@/lib/driverLang";
import { IconShield, IconLogout, IconBell } from "@/components/icons";
import i18n, { SUPPORTED_LANGS, LANG_LABELS } from "@/i18n";
import type { DriverProfile, TruckEnvelope, VahanEnvelope } from "@/lib/types";

// ISO timestamp -> DD-MM-YYYY (the driver-facing approval date). "—" when absent.
function fmtDate(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getDate())}-${p(d.getMonth() + 1)}-${d.getFullYear()}`;
}

// Enrollment/vehicle status -> Chip tone.
function statusTone(s?: string | null): "ok" | "warn" | "down" {
  const v = (s || "").toUpperCase();
  if (v === "ACTIVE") return "ok";
  if (v === "PENDING" || v === "REENROLL" || v === "MAINTENANCE") return "warn";
  return "down";
}

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
  const { session } = useDriverSession();
  const [vahan, setVahan] = useState<VahanEnvelope | null>(null);
  const [resolvedPlate, setResolvedPlate] = useState<string | null>(plate ?? null);
  const [loading, setLoading] = useState(true);
  const [push, setPush] = useState<PushState | null>(null);
  const [pushBusy, setPushBusy] = useState(false);
  const [profile, setProfile] = useState<DriverProfile | null>(null);
  const [profileLoading, setProfileLoading] = useState(true);

  // Load the driver's OWN approved profile. The gateway resolves it from the
  // DRIVER token's device binding; deviceId is passed only as an auth-disabled
  // dev fallback and is ignored server-side for a real DRIVER token.
  useEffect(() => {
    let alive = true;
    setProfileLoading(true);
    (async () => {
      try {
        const p = await api.driverProfile(deviceId);
        if (alive) setProfile(p);
      } catch {
        if (alive) setProfile(null); // not yet approved / no active assignment
      } finally {
        if (alive) setProfileLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [deviceId]);

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

  const driverName = session.name || t("home.driver", { defaultValue: "Driver" });
  const verified = session.status === "ACTIVE";

  return (
    <>
      {/* Driver profile header */}
      <div className="prof-header">
        <span className="prof-avatar">{driverName.trim().charAt(0).toUpperCase()}</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="prof-name">{driverName}</div>
          <div className="prof-plate selectable">
            {resolvedPlate || t("common.noData", { defaultValue: "—" })}
          </div>
          <div className="prof-badges">
            <span className={`prof-badge ${verified ? "ok" : "muted"}`}>
              <IconShield size={13} />{" "}
              {verified
                ? t("home.status.ACTIVE", { defaultValue: "Verified" })
                : t("home.status.UNVERIFIED", { defaultValue: "Not enrolled" })}
            </span>
          </div>
        </div>
      </div>

      {/* Driver Profile — the approved driver + assigned vehicle + enrollment.
          Sourced from GET /api/driver/profile (own identity only). */}
      {profileLoading ? (
        <Card title={t("driverProfile.title", { defaultValue: "Driver Profile" })}>
          <div className="muted" style={{ fontSize: 13 }}>
            <Spinner /> {t("driverProfile.loading", { defaultValue: "Loading profile…" })}
          </div>
        </Card>
      ) : profile ? (
        <>
          <Card title={t("driverProfile.driverInfo", { defaultValue: "Driver Information" })}>
            <Row
              k={t("driverProfile.name", { defaultValue: "Name" })}
              v={profile.driver.name || t("common.noData", { defaultValue: "—" })}
            />
            <Row
              k={t("driverProfile.driverId", { defaultValue: "Driver ID" })}
              v={<span className="selectable">{profile.driver.id || "—"}</span>}
            />
            <Row
              k={t("driverProfile.mobile", { defaultValue: "Mobile" })}
              v={profile.driver.mobile || "—"}
            />
            <Row
              k={t("driverProfile.licence", { defaultValue: "Licence" })}
              v={profile.driver.licence || "—"}
            />
            <Row
              k={t("driverProfile.emergency", { defaultValue: "Emergency Contact" })}
              v={profile.driver.emergency_contact || "—"}
            />
            <Row
              k={t("driverProfile.status", { defaultValue: "Status" })}
              v={<Chip status={statusTone(profile.driver.status)}>{profile.driver.status || "—"}</Chip>}
            />
          </Card>

          <Card title={t("driverProfile.vehicleInfo", { defaultValue: "Assigned Vehicle" })}>
            <Row
              k={t("driverProfile.vehicleId", { defaultValue: "Vehicle ID" })}
              v={<span className="selectable">{profile.vehicle.vehicle_id || "—"}</span>}
            />
            <Row
              k={t("driverProfile.vehicleNumber", { defaultValue: "Vehicle Number" })}
              v={profile.vehicle.vehicle_number || "—"}
            />
            <Row
              k={t("driverProfile.vehicleType", { defaultValue: "Type" })}
              v={profile.vehicle.vehicle_type || "—"}
            />
            {profile.vehicle.chassis_number ? (
              <Row
                k={t("driverProfile.chassis", { defaultValue: "Chassis Number" })}
                v={profile.vehicle.chassis_number}
              />
            ) : null}
            {profile.vehicle.rfid_fastag_id ? (
              <Row
                k={t("driverProfile.rfid", { defaultValue: "RFID / FASTag ID" })}
                v={profile.vehicle.rfid_fastag_id}
              />
            ) : null}
            <Row
              k={t("driverProfile.vehicleStatus", { defaultValue: "Vehicle Status" })}
              v={
                <Chip status={statusTone(profile.vehicle.status)}>
                  {profile.vehicle.status || "—"}
                </Chip>
              }
            />
          </Card>

          <Card title={t("driverProfile.enrollmentInfo", { defaultValue: "Enrollment Status" })}>
            <Row
              k={t("driverProfile.approvalStatus", { defaultValue: "Approval Status" })}
              v={
                <Chip status={statusTone(profile.enrollment.status)}>
                  {profile.enrollment.status || "—"}
                </Chip>
              }
            />
            <Row
              k={t("driverProfile.approvedDate", { defaultValue: "Approved Date" })}
              v={fmtDate(profile.enrollment.approved_at)}
            />
            {profile.enrollment.approved_by ? (
              <Row
                k={t("driverProfile.approvedBy", { defaultValue: "Approved By" })}
                v={profile.enrollment.approved_by}
              />
            ) : null}
          </Card>
        </>
      ) : (
        <Card title={t("driverProfile.title", { defaultValue: "Driver Profile" })}>
          <div className="muted" style={{ fontSize: 13 }}>
            {t("driverProfile.none", {
              defaultValue:
                "No approved profile is linked to this vehicle yet. Complete enrollment and wait for admin approval.",
            })}
          </div>
        </Card>
      )}

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
              <Chip status={verifiedLabel(path).ok ? "ok" : "warn"}>
                {verifiedLabel(path).label}
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
          <IconBell size={17} />{" "}
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
            // Sign out: clear the device pairing + DRIVER token locally. The JWT
            // is short-lived and self-expires; there is no server-side session to
            // revoke (the token is stateless and device-scoped).
            clearPairing();
            location.reload();
          }}
        >
          <IconLogout size={17} /> {t("profile.logout", { defaultValue: "Log out from device" })}
        </button>
      </Card>
    </>
  );
}
