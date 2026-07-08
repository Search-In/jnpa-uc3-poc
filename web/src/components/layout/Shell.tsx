// DTCCC application shell (FINAL PHASE redesign).
//
// Replaces the Esri Calcite chrome with a custom, operator-facing command-centre
// layout built in Tailwind so the portal reads as control-room software, not a
// developer dashboard:
//   ┌──────────────────────────────────────────────────────────┐
//   │ Header  logo · title             sim · lang · bell · reset │  (light)
//   ├───────────┬──────────────────────────────────────────────┤
//   │ Sidebar   │  routed screen content                        │
//   │ (dark)    │                                               │
//   │ grouped   │                                               │
//   └───────────┴──────────────────────────────────────────────┘
//
// Nav is grouped into OPERATIONS / ANALYTICS / ADMINISTRATION (see navConfig).
// Items are real <a href> NavLinks (role="link") so SPA routing AND the
// getByRole("link", { name }) e2e contract both hold. Collapsible sub-groups
// default OPEN so their child links stay in the DOM for that test.
//
// The root carries `calcite-mode-light` so the Calcite alert drawer (still used
// by HeaderActions) inherits light-mode tokens without a CalciteShell ancestor.

import { useState, type ReactNode } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ChevronDown, Play, RotateCcw, Waypoints } from "lucide-react";
import { SUPPORTED_LANGS, LANG_LABELS, type LangCode } from "@/i18n";
import i18n from "@/i18n";
import { HeaderActions } from "@/components/layout/HeaderActions";
import { GlobalSearch } from "@/components/layout/GlobalSearch";
import { canSeeScreen } from "@/lib/auth";
import { DATA_MODE } from "@/data";
import { cn } from "@/lib/utils";
import {
  NAV_SECTIONS,
  type NavGroup,
  type NavItem,
  type NavLeaf,
  type NavSection,
} from "@/components/layout/navConfig";

export interface ShellProps {
  children: ReactNode;
  /** Optional "Reset to baseline" handler (wired by screens/header logic). */
  onResetBaseline?: () => void;
  /** Disable the reset button (e.g. when no scenario is active). */
  resetDisabled?: boolean;
}

export function Shell({ children, onResetBaseline, resetDisabled }: ShellProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const currentLang = (i18n.resolvedLanguage ?? "en") as LangCode;

  return (
    <div
      className="calcite-mode-light flex h-full flex-col overflow-hidden bg-background text-foreground"
    >
      {/* ---- Header ---------------------------------------------------- */}
      <header className="z-20 flex h-14 shrink-0 items-center gap-3 border-b border-border bg-card px-4 shadow-sm">
        <button
          type="button"
          onClick={() => navigate("/command-center")}
          className="flex items-center gap-2.5 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
        >
          <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary text-primary-foreground shadow-sm">
            <Waypoints className="h-5 w-5" strokeWidth={2.2} />
          </span>
          <span className="hidden flex-col items-start leading-tight sm:flex">
            <span className="text-[15px] font-bold tracking-tight text-slate-900">
              {t("app.brandTitle")}
            </span>
            <span className="text-[11px] font-medium text-muted-foreground">
              {t("app.brandSubtitle")}
            </span>
          </span>
        </button>

        <GlobalSearch />

        <div className="ml-auto flex items-center gap-2 sm:gap-3">
          <button
            type="button"
            onClick={() => navigate("/simulator")}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-[13px] font-semibold text-primary-foreground shadow-sm transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
          >
            <Play className="h-3.5 w-3.5" fill="currentColor" strokeWidth={0} />
            <span className="hidden sm:inline">{t("nav.simulator")}</span>
          </button>

          <label className="sr-only" htmlFor="lang-select">
            {t("common.language")}
          </label>
          <select
            id="lang-select"
            value={currentLang}
            onChange={(e) => void i18n.changeLanguage(e.target.value as LangCode)}
            className="h-9 rounded-md border border-border bg-background px-2 text-[13px] font-medium text-foreground outline-none transition-colors hover:bg-muted focus-visible:ring-2 focus-visible:ring-primary/40"
          >
            {SUPPORTED_LANGS.map((code) => (
              <option key={code} value={code}>
                {LANG_LABELS[code]}
              </option>
            ))}
          </select>

          <HeaderActions />

          <button
            type="button"
            disabled={resetDisabled}
            onClick={() => onResetBaseline?.()}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-[13px] font-medium text-foreground transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            <span className="hidden md:inline">{t("common.resetToBaseline")}</span>
          </button>
        </div>
      </header>

      {/* ---- Body: sidebar + content ----------------------------------- */}
      <div className="flex min-h-0 flex-1">
        <Sidebar />
        <main className="min-h-0 min-w-0 flex-1 overflow-hidden">{children}</main>
      </div>
    </div>
  );
}

// --- Left navigation rail -----------------------------------------------------

// Enterprise sidebar design system — ONE dark surface (`--sidebar` below) with
// state changes expressed as white opacity overlays (not different colours), a
// single restrained `primary` accent, and standardized metrics: 36px item
// height, 18px icons @2 stroke, 13px labels, uppercase 10px section headers.
const SB = {
  idleText: "text-slate-400",
  idleIcon: "text-slate-500",
  hover: "hover:bg-white/[0.05] hover:text-slate-100",
  activeBg: "bg-white/[0.08]",
} as const;

function Sidebar() {
  const { t } = useTranslation();
  return (
    <nav
      aria-label={t("app.title")}
      className="hidden w-[248px] shrink-0 flex-col overflow-y-auto border-r border-white/[0.06] bg-[#0e131c] pb-2 pt-1 text-slate-400 md:flex [scrollbar-color:rgb(51_65_85)_transparent] [scrollbar-width:thin]"
    >
      {NAV_SECTIONS.map((section) => (
        <NavSectionBlock key={section.id} section={section} />
      ))}

      <div className="mt-auto space-y-1.5 border-t border-white/[0.06] px-4 pb-1 pt-3">
        <SourceBadge />
        <p className="text-[10.5px] leading-tight text-slate-500">{t("app.corridor")}</p>
      </div>
    </nav>
  );
}

function NavSectionBlock({ section }: { section: NavSection }) {
  const { t } = useTranslation();
  const visibleItems = section.items.filter(itemVisible);
  if (visibleItems.length === 0) return null;
  return (
    <div className="px-2.5">
      <div className="px-2.5 pb-1 pt-4 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500/80">
        {t(section.i18nKey)}
      </div>
      <div className="space-y-px">
        {visibleItems.map((item) =>
          item.kind === "group" ? (
            <NavGroupBlock key={item.id} group={item} />
          ) : (
            <NavLeafLink key={item.to} leaf={item} />
          ),
        )}
      </div>
    </div>
  );
}

function NavGroupBlock({ group }: { group: NavGroup }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(true); // default open → child links stay in DOM
  const Icon = group.icon;
  const children = group.children.filter((c) => canSeeScreen(c.to));
  if (children.length === 0) return null;
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className={cn(
          "flex h-9 w-full items-center gap-3 rounded-md px-2.5 text-[13px] font-medium transition-colors",
          SB.idleText,
          SB.hover,
        )}
      >
        <Icon className={cn("h-[18px] w-[18px] shrink-0", SB.idleIcon)} strokeWidth={2} />
        <span>{t(group.i18nKey)}</span>
        <ChevronDown
          className={cn(
            "ml-auto h-3.5 w-3.5 shrink-0 text-slate-500 transition-transform",
            !open && "-rotate-90",
          )}
        />
      </button>
      {open && (
        <div className="mb-0.5 ml-[19px] mt-px space-y-px border-l border-white/[0.06] pl-1.5">
          {children.map((c) => (
            <NavLeafLink key={c.to} leaf={c} nested />
          ))}
        </div>
      )}
    </div>
  );
}

function NavLeafLink({ leaf, nested }: { leaf: NavLeaf; nested?: boolean }) {
  const { t } = useTranslation();
  const Icon = leaf.icon;
  return (
    <NavLink
      to={leaf.to}
      className={({ isActive }) =>
        cn(
          "group relative flex h-9 items-center gap-3 rounded-md pr-3 text-[13px] transition-colors",
          nested ? "pl-2" : "pl-2.5",
          isActive
            ? cn(SB.activeBg, "font-semibold text-white")
            : cn("font-medium", SB.idleText, SB.hover),
        )
      }
    >
      {({ isActive }) => (
        <>
          {/* One restrained accent (the shared `primary` token) — a thin left bar,
              not a bright fill. Same accent as content-area buttons/tabs. */}
          <span
            aria-hidden
            className={cn(
              "absolute inset-y-1.5 left-0 w-[3px] rounded-r-full bg-primary transition-opacity",
              isActive ? "opacity-100" : "opacity-0",
            )}
          />
          <Icon
            className={cn(
              "h-[18px] w-[18px] shrink-0",
              isActive ? "text-primary" : cn(SB.idleIcon, "group-hover:text-slate-300"),
            )}
            strokeWidth={2}
          />
          <span className="truncate">{t(leaf.i18nKey)}</span>
        </>
      )}
    </NavLink>
  );
}

function itemVisible(item: NavItem): boolean {
  if (item.kind === "leaf") return canSeeScreen(item.to);
  return item.children.some((c) => canSeeScreen(c.to));
}

/** Small "data source" badge in the sidebar footer (LIVE = gateway, SIM = mock). */
function SourceBadge() {
  const live = DATA_MODE === "live";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider",
        live ? "bg-emerald-500/15 text-emerald-400" : "bg-amber-500/15 text-amber-400",
      )}
    >
      <span
        className={cn("h-1.5 w-1.5 rounded-full", live ? "bg-emerald-400" : "bg-amber-400")}
      />
      {live ? "RDS · LIVE" : "SIM"}
    </span>
  );
}

export default Shell;
