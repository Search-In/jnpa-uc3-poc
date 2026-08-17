// The port-wide focus: which vessel call / container / truck every panel is
// currently talking about.
//
// WHY THIS EXISTS. Before it, no app in the estate had shared entity selection.
// UC-1 ran four independent local vessel pickers, UC-2's only panel-to-panel
// reaction was one depot highlight, and UC-3's omnibox knew nothing about
// vessels. Selecting a call in one panel therefore told no other panel anything,
// and an evaluator had to retype the same key on every screen.
//
// CANONICAL SOURCE. This file is copied verbatim into poc_1, PoC_2/apps/web and
// suite/dtccc. Keep the three copies identical; the URL grammar is a contract
// between separately-deployed origins, so a drift here silently breaks the
// cross-app hand-off rather than failing loudly.
//
// THREE LAYERS, most durable first:
//   1. URL grammar  — survives reload, is the demo deep link, works offline.
//   2. This store   — fans out to every panel in THIS app.
//   3. Remote bus   — the UC-3 gateway WebSocket `focus` frame, which is the only
//                     channel that crosses origins (the three apps are on
//                     different hosts in production, so BroadcastChannel cannot
//                     carry it). Wired in by the socket layer via
//                     applyRemote/onPublish so this module stays dependency-free.

import { useSyncExternalStore } from "react";

export type FocusOrigin = "UC-1" | "UC-2" | "UC-3" | "SUITE";

/**
 * Both vessel key families are carried SEPARATELY and on purpose.
 *
 * `poc_1/src/types/domain.ts` states that a live AIS `Vessel` (keyed by MMSI)
 * and a `VesselCall` (keyed by VCN/IMO) are not joinable and must never be
 * merged. This shape respects that: it never derives one from the other, and a
 * consumer that has only an MMSI simply has no `vcn` to offer.
 */
export interface PortFocus {
  /** Vessel Call Number, e.g. "INNSA1NS0S0552". The strongest call key. */
  vcn?: string;
  /** Short VIA, e.g. "S0552". Terminals prefix it ("NTPS0633"); store it bare. */
  viaNo?: string;
  /** IMO number, e.g. "9523017". Identifies the hull, not the call. */
  imoNo?: string;
  /** Vessel name, e.g. "XIN HANG ZHOU". Case and spacing vary between sources. */
  vesselName?: string;
  /** ISO 6346 container number, e.g. "DPWU9011100". */
  containerNo?: string;
  /** Truck registration, e.g. "MH46H6948". */
  vehicleNo?: string;
  /** Import General Manifest number, e.g. "1194313". */
  igmNo?: string;
  /**
   * Date window, inclusive on both ends, as `YYYY-MM-DD` in IST.
   *
   * Carried on the focus rather than held per-panel because the corpus is a set
   * of disjoint time-slices — containers only exist 06–12 Jun, terminal KPIs
   * only 20–26 Jul — so a screen showing "all time" next to one showing a week
   * is comparing different worlds. One window, set once, honoured everywhere.
   *
   * `toDate` is INCLUSIVE of the whole day; the backend turns it into a
   * half-open bound so a single-day window is a real 24 hours.
   */
  fromDate?: string;
  toDate?: string;
  /** IST ISO-8601 instant that pins the replay clock (a point, not a range). */
  asOf?: string;
  /** Which app raised this focus. */
  origin: FocusOrigin;
  /** Bumped on every change so repeat selections still re-trigger consumers. */
  nonce: number;
}

/** The identity fields, i.e. everything except the bookkeeping. */
export type FocusKeys = Omit<PortFocus, "origin" | "nonce">;

export const EMPTY_FOCUS: PortFocus = { origin: "UC-3", nonce: 0 };

/** URL parameter names. Short, stable, and shared across all four surfaces. */
const PARAM: Record<keyof FocusKeys, string> = {
  vcn: "vcn",
  viaNo: "via",
  imoNo: "imo",
  vesselName: "vessel",
  containerNo: "container",
  vehicleNo: "vehicle",
  igmNo: "igm",
  // Same names the API uses, so a focus link and an API call read alike.
  fromDate: "from_date",
  toDate: "to_date",
  asOf: "asOf",
};

const KEYS = Object.keys(PARAM) as (keyof FocusKeys)[];

/** Every query-parameter name the focus owns, for callers that must clear them. */
export const FOCUS_PARAM_NAMES: readonly string[] = Object.values(PARAM);

/** True when no identity field is set — i.e. nothing is focused. */
export function isEmptyFocus(f: PortFocus | null | undefined): boolean {
  if (!f) return true;
  return KEYS.every((k) => !f[k]);
}

/** Value-equality over the identity fields only (nonce/origin are ignored). */
export function sameFocus(a: PortFocus, b: PortFocus): boolean {
  return KEYS.every((k) => (a[k] ?? "") === (b[k] ?? ""));
}

function normalise(raw: Partial<FocusKeys>): Partial<FocusKeys> {
  const out: Partial<FocusKeys> = {};
  for (const k of KEYS) {
    const v = raw[k];
    if (v === undefined || v === null) continue;
    const s = String(v).trim();
    if (!s) continue;
    // Identifiers are upper-cased so a hand-typed key matches a stored one.
    // Dates, the replay instant and the human-facing vessel name keep their
    // casing — upper-casing an ISO date is meaningless and would corrupt a
    // timestamp's `T`/`Z`.
    out[k] = VERBATIM_KEYS.has(k) ? s : s.toUpperCase();
  }
  return out;
}

/** Fields stored exactly as given, never upper-cased. */
const VERBATIM_KEYS = new Set<keyof FocusKeys>(["asOf", "vesselName", "fromDate", "toDate"]);

/** True when the focus carries a usable date window. */
export function hasWindow(f: Partial<FocusKeys>): boolean {
  return Boolean(f.fromDate || f.toDate);
}

/**
 * The focus window as API query parameters, ready to spread into a request.
 *
 * Every list endpoint that accepts a window uses these exact names, so a caller
 * never has to remember which screen maps to which parameter.
 */
export function windowParams(f: Partial<FocusKeys>): Record<string, string> {
  const p: Record<string, string> = {};
  if (f.fromDate) p.from_date = f.fromDate;
  if (f.toDate) p.to_date = f.toDate;
  return p;
}

// ---------------------------------------------------------------- the store --

let state: PortFocus = EMPTY_FOCUS;
const listeners = new Set<() => void>();
const publishers = new Set<(f: PortFocus) => void>();

function emit() {
  for (const l of listeners) l();
}

/** Broadcast to same-origin tabs. Absent in tests and old browsers — optional. */
const channel: BroadcastChannel | null =
  typeof BroadcastChannel !== "undefined" ? new BroadcastChannel("jnpa.focus") : null;

function commit(next: PortFocus, { announce }: { announce: boolean }) {
  state = next;
  emit();
  if (!announce) return;
  channel?.postMessage(next);
  for (const p of publishers) p(next);
}

export const focusStore = {
  /** Replace the focus outright. Selecting a vessel this way clears the box. */
  set(keys: Partial<FocusKeys>, origin: FocusOrigin = "UC-3"): PortFocus {
    const next: PortFocus = { ...normalise(keys), origin, nonce: state.nonce + 1 };
    commit(next, { announce: true });
    return next;
  },

  /** Narrow within the current focus — e.g. pick a container on a focused call. */
  refine(keys: Partial<FocusKeys>, origin: FocusOrigin = "UC-3"): PortFocus {
    const merged: PortFocus = {
      ...state,
      ...normalise(keys),
      origin,
      nonce: state.nonce + 1,
    };
    commit(merged, { announce: true });
    return merged;
  },

  clear(origin: FocusOrigin = "UC-3"): void {
    commit({ origin, nonce: state.nonce + 1 }, { announce: true });
  },

  /**
   * Apply a focus that arrived from another tab or another app. Deliberately
   * does NOT re-announce: echoing it back would loop between the two apps.
   * Identical focus is dropped so a round-trip cannot spin the nonce forever.
   */
  applyRemote(incoming: PortFocus): void {
    if (sameFocus(state, incoming)) return;
    commit({ ...incoming, nonce: state.nonce + 1 }, { announce: false });
  },

  /** Register a transport (the WebSocket layer) to carry focus off this origin. */
  onPublish(fn: (f: PortFocus) => void): () => void {
    publishers.add(fn);
    return () => publishers.delete(fn);
  },

  get(): PortFocus {
    return state;
  },

  subscribe(l: () => void): () => void {
    listeners.add(l);
    return () => listeners.delete(l);
  },
};

channel?.addEventListener("message", (e: MessageEvent) => {
  const f = e.data as PortFocus | undefined;
  if (f && typeof f === "object" && "nonce" in f) focusStore.applyRemote(f);
});

/** Subscribe a component to the current focus. */
export function usePortFocus(): PortFocus {
  return useSyncExternalStore(focusStore.subscribe, focusStore.get, focusStore.get);
}

// ------------------------------------------------------------- URL grammar --

/** Serialise a focus into query parameters, omitting anything unset. */
export function focusToParams(f: Partial<FocusKeys>): URLSearchParams {
  const p = new URLSearchParams();
  const n = normalise(f);
  for (const k of KEYS) {
    const v = n[k];
    if (v) p.set(PARAM[k], v);
  }
  return p;
}

/** Read a focus out of query parameters. Returns only the identity fields. */
export function focusFromParams(
  params: URLSearchParams | Record<string, string | undefined>,
): Partial<FocusKeys> {
  const read = (name: string): string | undefined =>
    params instanceof URLSearchParams ? params.get(name) ?? undefined : params[name];
  const raw: Partial<FocusKeys> = {};
  for (const k of KEYS) {
    const v = read(PARAM[k]);
    if (v) raw[k] = v;
  }
  return normalise(raw);
}

/** `?vcn=…&container=…` for a deep link into any of the four surfaces. */
export function focusQueryString(f: Partial<FocusKeys>): string {
  const qs = focusToParams(f).toString();
  return qs ? `?${qs}` : "";
}

// --------------------------------------------------------- key recognition --

/**
 * Recognise the unambiguous vessel-call keys in a raw search string.
 *
 * Deliberately conservative. A bare 7-digit number is genuinely ambiguous — it
 * could be an IMO (9523017) or an EIR number (4339869, also 7 digits) — so it is
 * NOT claimed here; the existing gate-document detection keeps it, and the
 * thread resolver disambiguates server-side. An IMO is only recognised when the
 * caller says so explicitly ("IMO 9523017").
 */
export function detectVesselKey(raw: string): Partial<FocusKeys> | null {
  const q = raw.trim().toUpperCase();
  if (!q) return null;
  // Full VCN: INNSA1 + 2-char terminal + 0 + series letter + 4 digits.
  if (/^INNSA1[A-Z]{2}\d[A-Z]\d{4}$/.test(q)) {
    return { vcn: q, viaNo: q.slice(-5) };
  }
  // VIA, bare (S0552) or with the terminal's vessel prefix (NTPS0633, APLS0595).
  const via = /^([A-Z]{0,3})([QRS]\d{4})$/.exec(q);
  if (via) return { viaNo: via[2] };
  // Explicit IMO only.
  const imo = /^IMO[\s:-]?(\d{7})$/.exec(q);
  if (imo) return { imoNo: imo[1] };
  return null;
}
