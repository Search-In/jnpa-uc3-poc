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
  Boxes,
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

const leaf = (to: string, i18nKey: string, icon: LucideIcon): NavLeaf => ({
  kind: "leaf",
  to,
  i18nKey,
  icon,
});

export const NAV_SECTIONS: NavSection[] = [
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
      leaf("/parking", "nav.parking", SquareParking),
      leaf("/gate-customs", "nav.gateCustoms", ShieldCheck),
      leaf("/alerts", "nav.alerts", BellRing),
    ],
  },
  {
    id: "analytics",
    i18nKey: "navSection.analytics",
    emoji: "📊",
    items: [
      leaf("/intelligence", "nav.intelligence", ScanSearch),
      leaf("/follow-the-box", "nav.followBox", Box),
      leaf("/cfs-ecy", "nav.cfsEcy", Boxes),
      // Geo-fencing Manager + Geo-fence Events are merged into one Geo Analytics
      // screen, so the sidebar shows a SINGLE entry (no duplicate operational
      // pages). Both /geofencing and /geofence-events routes remain valid for
      // deep links; the merged screen opens at the matching default tab.
      leaf("/geofencing", "navGroup.geo", MapIcon),
      leaf("/fastag", "nav.fastag", CreditCard),
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
      leaf("/workflows", "nav.workflows", Workflow),
      leaf("/health", "nav.health", HeartPulse),
      leaf("/what-if", "nav.whatIf", FlaskConical),
      leaf("/demo", "nav.demo", SlidersHorizontal),
    ],
  },
];

/** Flat list of every leaf route in IA order — used for quick links / lookups. */
export const NAV_LEAVES: NavLeaf[] = NAV_SECTIONS.flatMap((s) =>
  s.items.flatMap((i) => (i.kind === "group" ? i.children : [i])),
);
