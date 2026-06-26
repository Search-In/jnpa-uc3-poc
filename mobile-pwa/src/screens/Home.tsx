import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Card, Chip, Row, Spinner } from "@/components/ui";
import { useDriverSession, type DriverStatus } from "@/hooks/DriverSession";

// Home — the context landing screen shown immediately after login. It is NOT the
// trip dashboard: its only job is to confirm WHO is signed in (driver context
// from the global session) and offer the entry points into the working modules.
// All data here comes from the one-time session load — nothing is refetched.

const STATUS_CHIP: Record<DriverStatus, "ok" | "warn" | "down"> = {
  ACTIVE: "ok",
  PENDING: "warn",
  REENROLL: "warn",
  REJECTED: "down",
  UNVERIFIED: "warn",
};

export default function Home() {
  const { t } = useTranslation();
  const { session, loading } = useDriverSession();
  const navigate = useNavigate();

  const statusLabel = t(`home.status.${session.status}`);
  const verified = session.status === "ACTIVE";

  const actions: { to: string; label: string; icon: string; primary?: boolean }[] = [
    { to: "/trip", label: t("home.startTrip"), icon: "🛣", primary: true },
    { to: "/enrol", label: t("home.goToEnrol"), icon: "🪪" },
    { to: "/inbox", label: t("home.alerts"), icon: "✉" },
    { to: "/profile", label: t("home.vehicleInfo"), icon: "🚛" },
  ];

  return (
    <div className="home">
      <Card className="home-hero">
        <div className="home-welcome">
          <span className="home-avatar" aria-hidden>
            {(session.name || "D").trim().charAt(0).toUpperCase()}
          </span>
          <div>
            <div className="home-eyebrow">{t("home.welcome")}</div>
            <h2 className="home-name">
              {loading && !session.name ? <Spinner /> : session.name || t("home.driver")}
            </h2>
          </div>
        </div>

        <div className="home-status">
          <Chip status={STATUS_CHIP[session.status]}>{statusLabel}</Chip>
        </div>

        <div className="home-context">
          <Row k={t("home.driverId")} v={session.driverId || t("common.noData")} />
          <Row k={t("home.vehicle")} v={session.vehicle || t("common.noData")} />
          <Row
            k={t("home.lastVerified")}
            v={verified ? t("home.today") : t("common.noData")}
          />
        </div>

        {session.status !== "ACTIVE" && (
          <button className="btn primary" style={{ marginTop: 14 }} onClick={() => navigate("/enrol")}>
            {session.status === "UNVERIFIED" ? t("home.completeEnrol") : t("home.viewEnrol")}
          </button>
        )}
      </Card>

      <div className="home-actions">
        {actions.map((a) => (
          <button
            key={a.to}
            className={`home-action ${a.primary ? "primary" : ""}`}
            onClick={() => navigate(a.to)}
          >
            <span className="home-action-icon" aria-hidden>
              {a.icon}
            </span>
            <span className="home-action-label">{a.label}</span>
            <span className="home-action-chevron" aria-hidden>
              ›
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
