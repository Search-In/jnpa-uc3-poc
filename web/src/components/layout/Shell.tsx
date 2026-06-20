// Calcite design-system application shell (UC1 parity).
//
// Drop-in replacement for the Tailwind Sidebar/Header chrome. Uses the Esri
// Calcite web components (dark mode) via @esri/calcite-components-react:
//   <calcite-shell>
//     <calcite-navigation slot="header">         ← top bar (title + lang + reset)
//     <calcite-shell-panel slot="panel-start">    ← left nav rail (6 routes)
//     {children}                                  ← routed screen content
//
// Nav items are React-Router <NavLink>s (real <a href> → implicit role="link")
// styled with Calcite dark design tokens. Using anchors keeps SPA routing AND
// the role="link" accessible-name contract the Playwright e2e nav test relies
// on (getByRole("link", { name })) — calcite-menu-item renders role="menuitem"
// instead, which would break that test.

import type { CSSProperties, ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  CalciteShell,
  CalciteShellPanel,
  CalciteNavigation,
  CalciteNavigationLogo,
  CalciteSelect,
  CalciteOption,
  CalciteButton,
  CalciteIcon,
} from "@esri/calcite-components-react";
import { SUPPORTED_LANGS, LANG_LABELS, type LangCode } from "@/i18n";
import i18n from "@/i18n";
import { SHELL } from "@/lib/tokens";

// Route table — labels resolved through t() at render time. The `i18nKey` maps
// into the `nav` namespace seeded in src/i18n/locales/*.json.
const ROUTES = [
  { to: "/live", i18nKey: "nav.live", icon: "activity-monitor" },
  { to: "/advisory", i18nKey: "nav.advisory", icon: "route-from" },
  { to: "/geofencing", i18nKey: "nav.geofencing", icon: "shapes" },
  { to: "/reports", i18nKey: "nav.reports", icon: "file-report" },
  { to: "/health", i18nKey: "nav.health", icon: "heart" },
  { to: "/what-if", i18nKey: "nav.whatIf", icon: "beaker" },
  { to: "/demo", i18nKey: "nav.demo", icon: "sliders-horizontal" },
] as const;

export interface ShellProps {
  children: ReactNode;
  /** Optional "Reset to baseline" handler (wired by screens/header logic). */
  onResetBaseline?: () => void;
  /** Disable the reset button (e.g. when no scenario is active). */
  resetDisabled?: boolean;
}

const navLinkBase: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "0.625rem",
  padding: "0.625rem 1rem",
  fontSize: "0.875rem",
  textDecoration: "none",
  color: SHELL.text2,
  borderInlineStart: "3px solid transparent",
};

function navLinkStyle(isActive: boolean): CSSProperties {
  return isActive
    ? {
        ...navLinkBase,
        color: SHELL.text1,
        background: SHELL.foreground2,
        borderInlineStartColor: SHELL.brand,
        fontWeight: 600,
      }
    : navLinkBase;
}

export function Shell({ children, onResetBaseline, resetDisabled }: ShellProps) {
  const { t } = useTranslation();

  function onLangChange(e: { target: { value: string } }) {
    void i18n.changeLanguage(e.target.value as LangCode);
  }

  const currentLang = (i18n.resolvedLanguage ?? "en") as LangCode;

  return (
    <CalciteShell className="calcite-mode-light" style={{ height: "100%" }}>
      {/* ---- Header ---- */}
      <CalciteNavigation slot="header">
        <CalciteNavigationLogo
          slot="logo"
          heading={t("app.shortTitle")}
          description={t("app.subtitle")}
          icon="road-sign"
        />

        <div
          slot="content-end"
          style={{
            display: "flex",
            alignItems: "center",
            gap: "0.75rem",
            paddingInlineEnd: "0.75rem",
          }}
        >
          <CalciteSelect
            label={t("common.language")}
            width="auto"
            value={currentLang}
            onCalciteSelectChange={onLangChange}
          >
            {SUPPORTED_LANGS.map((code) => (
              <CalciteOption key={code} value={code}>
                {LANG_LABELS[code]}
              </CalciteOption>
            ))}
          </CalciteSelect>

          <CalciteButton
            appearance="outline"
            kind="neutral"
            iconStart="reset"
            scale="s"
            disabled={resetDisabled || undefined}
            onClick={() => onResetBaseline?.()}
          >
            {t("common.resetToBaseline")}
          </CalciteButton>
        </div>
      </CalciteNavigation>

      {/* ---- Left nav rail ---- */}
      <CalciteShellPanel slot="panel-start" widthScale="m" collapsed={false} resizable={false}>
        <nav
          aria-label={t("app.title")}
          style={{ display: "flex", flexDirection: "column", paddingBlock: "0.5rem" }}
        >
          {ROUTES.map((r) => (
            <NavLink key={r.to} to={r.to} style={({ isActive }) => navLinkStyle(isActive)}>
              <CalciteIcon icon={r.icon} scale="s" />
              <span>{t(r.i18nKey)}</span>
            </NavLink>
          ))}
          <div
            style={{
              marginBlockStart: "auto",
              padding: "0.75rem 1rem",
              fontSize: "0.6875rem",
              color: SHELL.text3,
            }}
          >
            {t("app.corridor")}
          </div>
        </nav>
      </CalciteShellPanel>

      {/* ---- Routed content ---- */}
      {children}
    </CalciteShell>
  );
}

export default Shell;
