// Information architecture for the DTCCC portal (FINAL PHASE redesign).
//
// The flat 13-item list is regrouped into three operator-facing sections —
// OPERATIONS / ANALYTICS / ADMINISTRATION — each holding leaf routes and, where
// the brief calls for it, a collapsible sub-group (Traffic Operations, Geo
// Analytics). Every screen from the previous build is preserved; only the
// grouping, labels and landing page change.
//
// Leaf `.to` values map 1:1 onto the existing routes/screens (no backend change).
// Labels resolve through i18n `nav.*`; the e2e nav test clicks links by their
// rendered label text, so those strings are the accessible-name contract.

import type { LucideIcon } from "lucide-react";
import {
  LayoutDashboard,
  Truck,
  Radio,
  Route,
  SquareParking,
  ShieldCheck,
  BellRing,
  ScanSearch,
  Map as MapIcon,
  Box,
  CreditCard,
  FileText,
  UserPlus,
  CarFront,
  HeartPulse,
  FlaskConical,
  SlidersHorizontal,
  Workflow,
  BarChart3,
  Anchor,
  Boxes,
  Ship,
} from "lucide-react";

export interface NavLeaf {
  kind: "leaf";
  to: string;
  i18nKey: string;
  icon: LucideIcon;
}

export interface NavGroup {
  kind: "group";
  id: string;
  i18nKey: string;
  icon: LucideIcon;
  children: NavLeaf[];
}

export type NavItem = NavLeaf | NavGroup;

export interface NavSection {
  id: string;
  i18nKey: string;
  emoji: string;
  items: NavItem[];
}

// Internal-only screens are hidden from the client navigation but stay routed
// (deep-link/bookmark still work) — Workflow Composer's rule actions are not yet
// executed by the backend, and Demo Console injects faults, so neither belongs
// in a client demo. Set VITE_SHOW_INTERNAL_SCREENS=true to show them again.
export const SHOW_INTERNAL_SCREENS = import.meta.env.VITE_SHOW_INTERNAL_SCREENS === "true";

const leaf = (to: string, i18nKey: string, icon: LucideIcon): NavLeaf => ({
  kind: "leaf",
  to,
  i18nKey,
  icon,
});

export const NAV_SECTIONS: NavSection[] = [
  // Section order mirrors the operator's workflow: what is happening NOW, then
  // the UC-3 truck & cargo journey, then analysis, then administration.
  {
    id: "operations",
    i18nKey: "navSection.operations",
    emoji: "🚦",
    items: [
      leaf("/command-center", "nav.commandCenter", LayoutDashboard),
      {
        kind: "group",
        id: "traffic",
        i18nKey: "navGroup.traffic",
        icon: Truck,
        children: [leaf("/live", "nav.live", Radio), leaf("/advisory", "nav.advisory", Route)],
      },
      leaf("/alerts", "nav.alerts", BellRing),
      leaf("/parking", "nav.parking", SquareParking),
    ],
  },
  {
    // The complete UC-3 operational journey, in the order it happens.
    id: "lifecycle",
    i18nKey: "navSection.lifecycle",
    emoji: "📦",
    items: [
      leaf("/uc3-lifecycle", "nav.uc3Lifecycle", Box),
      leaf("/gate-customs", "nav.gateCustoms", ShieldCheck),
      leaf("/truck-ops", "nav.truckOps", Truck),
      leaf("/truck-visit", "nav.truckVisit", FileText),
      leaf("/vehicle-registry", "nav.vehicleRegistry", Truck),
      leaf("/cfs-ecy", "nav.cfsEcy", Boxes),
      leaf("/shipping-lines", "nav.shippingLines", Ship),
      // Cargo What-If sits at the end of the lifecycle section: it asks "what
      // would change" about the journey the screens above show as it is.
      leaf("/cargo-whatif", "nav.cargoWhatIf", FlaskConical),
      leaf("/corridor-simulation", "nav.corridorSim", FlaskConical),
    ],
  },
  {
    id: "analytics",
    i18nKey: "navSection.analytics",
    emoji: "📊",
    items: [
      leaf("/intelligence", "nav.intelligence", ScanSearch),
      // Geo-fencing Manager + Geo-fence Events are merged into one Geo Analytics
      // screen, so the sidebar shows a SINGLE entry. Both /geofencing and
      // /geofence-events routes remain valid for deep links.
      leaf("/geofencing", "navGroup.geo", MapIcon),
      leaf("/fastag", "nav.fastag", CreditCard),
      leaf("/berthing", "nav.berthing", Anchor),
      leaf("/performance", "nav.performance", BarChart3),
      leaf("/reports", "nav.reports", FileText),
    ],
  },

  {
    id: "administration",
    i18nKey: "navSection.administration",
    emoji: "⚙",
    items: [
      leaf("/vehicles", "nav.vehicles", CarFront),
      leaf("/enrollments", "nav.enrollments", UserPlus),
      leaf("/health", "nav.health", HeartPulse),
      ...(SHOW_INTERNAL_SCREENS
        ? [
            leaf("/workflows", "nav.workflows", Workflow),
            leaf("/demo", "nav.demo", SlidersHorizontal),
          ]
        : []),
      leaf("/what-if", "nav.whatIf", FlaskConical),
    ],
  },
];

/** Flat list of every leaf route in IA order — used for quick links / lookups. */
export const NAV_LEAVES: NavLeaf[] = NAV_SECTIONS.flatMap((s) =>
  s.items.flatMap((i) => (i.kind === "group" ? i.children : [i])),
);
