// Design tokens — the SINGLE source of colour truth for the dashboard.
//
// Rule: no colour literals anywhere outside this file. Every component, map
// layer, chart, chip and Calcite override pulls its colours from here. The
// theme-agnostic severity / flow / jam ramps below re-export the helpers in
// ./palette.ts (Okabe–Ito, colour-blind safe) so legend and data never drift,
// and the Calcite-aligned SEMANTIC tokens give the ArcGIS + Calcite dark shell
// a coherent surface palette.

import {
  SEVERITY_COLOUR,
  severityColour,
  severityRank,
  jamColour,
  gateColour,
  sourceStateColour,
} from "./palette";

// ---------------------------------------------------------------------------
// Re-exports — palette ramps stay the canonical, colour-blind-safe source.
// ---------------------------------------------------------------------------
export {
  SEVERITY_COLOUR,
  severityColour,
  severityRank,
  jamColour,
  gateColour,
  sourceStateColour,
};

// ---------------------------------------------------------------------------
// Okabe–Ito palette (colour-blind safe). The atomic literals live ONLY here.
// ---------------------------------------------------------------------------
export const OKABE_ITO = {
  black: "#000000",
  orange: "#E69F00",
  skyBlue: "#56B4E9",
  bluishGreen: "#009E73",
  yellow: "#F0E442",
  blue: "#0072B2",
  vermillion: "#D55E00",
  reddishPurple: "#CC79A7",
  grey: "#999999",
} as const;

// ---------------------------------------------------------------------------
// Semantic status tokens (theme-agnostic; used by map renderers + chips).
// ---------------------------------------------------------------------------
export const STATUS = {
  ok: OKABE_ITO.bluishGreen,
  good: OKABE_ITO.bluishGreen,
  info: OKABE_ITO.skyBlue,
  warning: OKABE_ITO.orange,
  warn: OKABE_ITO.orange,
  critical: OKABE_ITO.vermillion,
  bad: OKABE_ITO.vermillion,
  unknown: OKABE_ITO.grey,
} as const;

/** Throughput / jam ramp — green → amber → red (CB-safe). */
export const FLOW = {
  good: OKABE_ITO.bluishGreen,
  warn: OKABE_ITO.orange,
  bad: OKABE_ITO.vermillion,
} as const;

// ---------------------------------------------------------------------------
// Calcite-aligned DARK shell surface tokens. These mirror the Calcite dark
// design-token CSS custom properties so any plain DOM we render inside the
// Calcite shell (map overlays, legends, custom panels) matches the chrome.
// Keeping them here means a future theme switch is a one-file change.
// ---------------------------------------------------------------------------
export const SHELL = {
  // Backgrounds (Calcite dark "foreground" ramp).
  appBackground: "#1e1e1e", // calcite-color-background
  foreground1: "#2b2b2b", // calcite-color-foreground-1 (panels/cards)
  foreground2: "#353535", // calcite-color-foreground-2 (hover)
  foreground3: "#404040", // calcite-color-foreground-3 (press)
  // Text.
  text1: "#ffffff", // calcite-color-text-1
  text2: "#d4d4d4", // calcite-color-text-2
  text3: "#acacac", // calcite-color-text-3
  textInverse: "#151515",
  // Lines / borders.
  border1: "#4a4a4a", // calcite-color-border-1
  border2: "#404040",
  border3: "#353535",
  // Brand.
  brand: "#00a0ff", // calcite-color-brand (dark)
  brandHover: "#009aff",
  brandPress: "#0087e0",
} as const;

// ---------------------------------------------------------------------------
// Map layer tokens — symbology pulled from the ramps above so the ArcGIS
// GraphicsLayers and the legend reference one definition each.
// ---------------------------------------------------------------------------
export const MAP_TOKENS = {
  /** Corridor polyline outline halo. */
  corridorHalo: "rgba(0,0,0,0.55)",
  /** Truck dot fill + stroke. */
  truckFill: OKABE_ITO.blue,
  truckStroke: "#ffffff",
  truckTrail: OKABE_ITO.skyBlue,
  /** Gate marker stroke. */
  gateStroke: "#ffffff",
  /** Zone fills by kind. */
  zoneRestrictedFill: OKABE_ITO.vermillion,
  zoneNoParkingFill: OKABE_ITO.skyBlue,
  zoneRestrictedOutline: OKABE_ITO.vermillion,
  zoneNoParkingOutline: OKABE_ITO.skyBlue,
  /** Parking facility status colours. */
  parkingAvailable: OKABE_ITO.bluishGreen,
  parkingFilling: OKABE_ITO.orange,
  parkingFull: OKABE_ITO.vermillion,
  /** Heatmap colour stops (low → high jam). */
  heatStops: [
    { ratio: 0, color: "rgba(0,158,115,0)" },
    { ratio: 0.3, color: OKABE_ITO.bluishGreen },
    { ratio: 0.6, color: OKABE_ITO.orange },
    { ratio: 1, color: OKABE_ITO.vermillion },
  ] as const,
} as const;

/** Parking facility status → colour. */
export function parkingStatusColour(status?: string | null): string {
  switch (status) {
    case "AVAILABLE":
      return MAP_TOKENS.parkingAvailable;
    case "FILLING":
      return MAP_TOKENS.parkingFilling;
    case "FULL":
      return MAP_TOKENS.parkingFull;
    default:
      return STATUS.unknown;
  }
}

/** Zone kind → fill/outline colour. */
export function zoneColour(kind?: string | null): string {
  return kind === "restricted"
    ? MAP_TOKENS.zoneRestrictedFill
    : MAP_TOKENS.zoneNoParkingFill;
}
