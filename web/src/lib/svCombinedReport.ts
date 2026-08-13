// Presentation model for the SecureVision "Combined incident report".
//
// The vendor answers the combined endpoint with TWO different kinds of content:
//
//   * `incidents[]` — the structured detections (already normalised by the
//     gateway: plate, counts, container, tamper). These are FACTS the console
//     can label and colour.
//   * `combined_description` — ONE language-model paragraph that restates those
//     detections in prose, and is the only place a risk level / priority /
//     recommended action ever appears. It has no schema.
//
// Rendering the paragraph as a single block of text is what makes the report
// unscannable, so this module turns the response into a small, typed view model:
// structured facts come from `incidents[]`, and the narrative is SPLIT (never
// rewritten) into verbatim segments that are routed to the section they talk
// about. Nothing here invents, summarises or re-words anything — every string a
// section shows is either a value the API returned or a contiguous fragment of
// the narrative with markdown emphasis stripped and whitespace collapsed. The
// untouched narrative is always carried through in `narrative` so the UI can
// keep offering the original text.
//
// No React/DOM here, so it is unit-testable with vitest (see
// svCombinedReport.test.ts) — the same split as lib/securevision.ts.

import type { Tone } from "@/components/ui/dtccc";
import {
  containerAgreementLabel,
  containerAgreementTone,
  fmtConfidence,
  tamperTone,
  type SvCombinedReport,
  type SvIncident,
} from "./securevision";

// ------------------------------------------------------------------ view model

export type SvReportSectionKey = "vehicle" | "person" | "camera";

/** One labelled value lifted from a structured detection field. */
export interface SvReportFact {
  label: string;
  value: string;
  /** Set when the value should read as a status chip rather than plain text. */
  tone?: Tone;
  mono?: boolean;
  hint?: string;
}

export interface SvReportSection {
  key: SvReportSectionKey;
  title: string;
  /** Values taken from `incidents[]`. */
  facts: SvReportFact[];
  /** Verbatim narrative fragments that talk about this section. */
  notes: string[];
}

export interface SvRiskValue {
  /** Verbatim value as the narrative stated it. */
  value: string;
  tone: Tone;
}

export interface SvRiskAndAction {
  risk: SvRiskValue | null;
  priority: SvRiskValue | null;
  /** Verbatim recommended action sentence. */
  action: string | null;
}

export interface SvStructuredReport {
  /** The opening of the narrative, verbatim. Null when there is no narrative. */
  summary: string | null;
  /** Only sections that actually have data — empty ones are omitted. */
  sections: SvReportSection[];
  riskAction: SvRiskAndAction | null;
  /** The narrative exactly as the API returned it. */
  narrative: string | null;
  aiGenerated: boolean;
  incidents: SvIncident[];
  /** Narrative fragments that fit no section — surfaced, never dropped. */
  other: string[];
}

export const SV_SECTION_TITLES: Record<SvReportSectionKey, string> = {
  vehicle: "Vehicle & container",
  person: "Restricted zone / person",
  camera: "Camera health / tamper",
};

// -------------------------------------------------------------- narrative text

/** Markdown emphasis a model sometimes emits around labels. */
const EMPHASIS = /[*_`]+/g;

/** The narrative with emphasis markers dropped and whitespace collapsed.
 *  Used as the reference text every rendered fragment must be found in. */
export function normalizeNarrative(text: string | null | undefined): string {
  if (!text) return "";
  return text.replace(EMPHASIS, "").replace(/\s+/g, " ").trim();
}

/**
 * Split a narrative into the smallest fragments that still read as statements.
 *
 * Lines, bullets, sentences and semicolon clauses — every returned string is a
 * contiguous slice of the input, so a caller can display one without asserting
 * anything the model did not write.
 */
export function splitNarrative(text: string | null | undefined): string[] {
  if (!text) return [];
  const out: string[] = [];
  for (const line of text.replace(EMPHASIS, "").split(/\r?\n+/)) {
    const stripped = line.replace(/^\s*(?:[-•*–—>]|\d+[.)])\s+/, "").trim();
    if (!stripped) continue;
    for (const part of stripped.split(/(?<=[.!?])\s+(?=[A-Z0-9("])|;\s+/)) {
      const segment = part.replace(/\s+/g, " ").trim();
      if (segment.length > 1) out.push(segment);
    }
  }
  return out;
}

export type SvSegmentTopic = SvReportSectionKey | "risk" | "other";

/** Keyword sets per topic. Order is the tie-break when scores are equal. */
const TOPIC_PATTERNS: ReadonlyArray<readonly [SvSegmentTopic, readonly RegExp[]]> = [
  [
    "risk",
    [
      /\brisk\b/i,
      /\bpriorit(y|ies)\b/i,
      /\brecommend(ed|s|ation)?\b/i,
      /\bescalat/i,
      /\bseverity\b/i,
      /\baction\b/i,
    ],
  ],
  [
    "camera",
    [
      /\bcameras?\b/i,
      /\btamper/i,
      /\bblur/i,
      /\bobstruct/i,
      /\bocclu/i,
      /\bblack\s?(screen|frame|out)\b/i,
      /\bfroze(n)?\b|\bfreez/i,
      /\bfog(g|gy|ging)?\b/i,
      /\bexposure\b/i,
      /\bglare\b/i,
      /\blens\b/i,
      /\bdefocus/i,
      /\bfeed\b/i,
      /\boperational\b/i,
    ],
  ],
  [
    "person",
    [
      /\bpersons?\b|\bpeople\b/i,
      /\bindividuals?\b/i,
      /\bworkers?\b|\bpersonnel\b|\blabou?rers?\b/i,
      /\bunauthoris|\bunauthoriz/i,
      /\bauthoris|\bauthoriz/i,
      /\bunverified\b/i,
      /\brestricted\b/i,
      /\bmachinery\b/i,
      /\bzones?\b/i,
      /\bintrusion\b/i,
      /\bfaces?\b|\bidentit/i,
      /\bpedestrian/i,
    ],
  ],
  [
    "vehicle",
    [
      /\bplates?\b/i,
      /\btrailers?\b/i,
      /\btrucks?\b|\blorr(y|ies)\b/i,
      /\bvehicles?\b/i,
      /\bcontainers?\b/i,
      /\biso[\s-]?6346\b/i,
      /\bocr\b/i,
      /\bunreadable\b|\billegible\b|\bpartially\s+read/i,
      /\bclassif/i,
      /\bcount(ed|s|ing)?\b/i,
      /\bcars?\b|\bbus(es)?\b|\bvans?\b|\bmotorcycles?\b|\bbikes?\b/i,
      /\bchassis\b|\baxles?\b/i,
    ],
  ],
];

/** Which section a narrative fragment belongs to, by keyword weight. */
export function classifySegment(segment: string): SvSegmentTopic {
  let best: SvSegmentTopic = "other";
  let bestScore = 0;
  for (const [topic, patterns] of TOPIC_PATTERNS) {
    const score = patterns.reduce((n, re) => (re.test(segment) ? n + 1 : n), 0);
    if (score > bestScore) {
      bestScore = score;
      best = topic;
    }
  }
  return best;
}

// ------------------------------------------------------------- risk and action

/** Tone for a risk/priority WORD. Presentation only — the value is untouched. */
export function riskTone(value: string | null | undefined): Tone {
  const v = (value ?? "").toLowerCase();
  if (!v) return "neutral";
  if (/\b(critical|severe|high|urgent|immediate|p1)\b/.test(v)) return "critical";
  if (/\b(medium|moderate|elevated|caution|p2)\b/.test(v)) return "warn";
  if (/\b(low|minimal|negligible|none|nil|normal|routine|clear|p3|p4)\b/.test(v)) return "ok";
  return "neutral";
}

/** Trim a captured value to a single short phrase without altering its words. */
function tidyValue(raw: string | undefined): string | null {
  if (!raw) return null;
  const value = raw
    .replace(EMPHASIS, "")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/[.,;:]+$/, "");
  if (!value || value.length > 64) return null;
  return value;
}

/**
 * Pull the risk level, priority and recommended action out of the narrative.
 *
 * Labelled forms ("Risk Level: HIGH") are read first; the unlabelled phrasings
 * a model falls back to ("overall risk is medium") are matched second. Every
 * returned string is the model's own wording — nothing is normalised to a
 * vocabulary this console prefers, and an absent field stays null rather than
 * defaulting to a value nobody asserted.
 */
export function extractRiskAndAction(text: string | null | undefined): SvRiskAndAction {
  const source = normalizeNarrative(text);
  if (!source) return { risk: null, priority: null, action: null };

  const riskRaw =
    tidyValue(/\brisk(?:\s*level)?\s*[:=]\s*([^.;\n]+)/i.exec(source)?.[1]) ??
    tidyValue(
      /\brisk(?:\s*level)?\s+(?:is|was|assessed(?:\s+as)?|rated|considered)\s+(?:to\s+be\s+)?([A-Za-z-]+)/i.exec(
        source,
      )?.[1],
    ) ??
    tidyValue(
      /\b(critical|severe|high|elevated|medium|moderate|low|minimal|negligible)[\s-]+(?:level\s+)?risk\b/i.exec(
        source,
      )?.[1],
    );

  const priorityRaw =
    tidyValue(/\bpriority\s*[:=]\s*([^.;\n]+)/i.exec(source)?.[1]) ??
    tidyValue(/\bpriority\s+(?:is|was|set\s+to)\s+([A-Za-z0-9-]+)/i.exec(source)?.[1]) ??
    tidyValue(
      /\b(critical|urgent|high|medium|moderate|low|routine|p[1-4])[\s-]+priority\b/i.exec(
        source,
      )?.[1],
    );

  const actionRaw =
    /\brecommended\s+actions?\s*[:=]\s*(.+?)(?:(?<=[.!?])\s+(?=[A-Z])|$)/i.exec(source)?.[1] ??
    /\b(?:recommended\s+action\s+is|it\s+is\s+recommended)\s+(?:that\s+|to\s+)?(.+?)(?:(?<=[.!?])\s+(?=[A-Z])|$)/i.exec(
      source,
    )?.[1] ??
    null;
  const action = actionRaw ? actionRaw.replace(/\s+/g, " ").trim() || null : null;

  return {
    risk: riskRaw ? { value: riskRaw, tone: riskTone(riskRaw) } : null,
    priority: priorityRaw ? { value: priorityRaw, tone: riskTone(priorityRaw) } : null,
    action,
  };
}

// ------------------------------------------------------------ structured facts

function pick(incidents: SvIncident[], code: string): SvIncident | null {
  return incidents.find((i) => i.incident_code === code) ?? null;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function vehicleFacts(incidents: SvIncident[]): SvReportFact[] {
  const facts: SvReportFact[] = [];
  const plate = pick(incidents, "i01");
  if (plate?.fired) {
    const read = str(plate.plate?.plate);
    if (read) {
      facts.push({ label: "Plate", value: read, mono: true });
      if (plate.plate?.plate_valid != null) {
        facts.push({
          label: "Plate valid",
          value: plate.plate.plate_valid ? "Valid" : "Invalid",
          tone: plate.plate.plate_valid ? "ok" : "critical",
        });
      }
    } else {
      // I-01 fired without returning a string: the read is unusable. Said
      // plainly rather than shown as a blank plate field.
      facts.push({
        label: "Plate",
        value: "Not readable",
        tone: "warn",
        hint: "SecureVision ran the plate analyzer but returned no plate string for this clip.",
      });
    }
    const ocr = num(plate.ocr_confidence) ?? num(plate.plate?.ocr_confidence);
    if (ocr != null) facts.push({ label: "OCR confidence", value: fmtConfidence(ocr) });
    const type = str(plate.plate?.vehicle_type);
    if (type) facts.push({ label: "Vehicle type", value: type });
    const colour = str(plate.plate?.vehicle_color);
    if (colour) facts.push({ label: "Vehicle colour", value: colour });
  }

  const count = pick(incidents, "i02");
  if (count?.fired) {
    if (count.total_count != null) {
      facts.push({ label: "Vehicles counted", value: String(count.total_count) });
    }
    const classes = (count.counts ?? [])
      .filter((c) => c.count != null)
      .map((c) => `${c.vehicle_class ?? "unknown"} ×${c.count}`);
    if (classes.length) facts.push({ label: "Classification", value: classes.join(", ") });
  }

  const container = pick(incidents, "i09");
  if (container?.fired) {
    const number = str(container.container?.number);
    if (number) facts.push({ label: "Container", value: number, mono: true });
    if (container.container?.agreement) {
      facts.push({
        label: "ISO 6346 cross-check",
        value: containerAgreementLabel(container.container.agreement),
        tone: containerAgreementTone(container.container.agreement),
      });
    }
    const towing = str(container.container?.plate);
    if (towing && towing !== str(plate?.plate?.plate)) {
      facts.push({ label: "Towing vehicle", value: towing, mono: true });
    }
  }
  return facts;
}

function personFacts(incidents: SvIncident[]): SvReportFact[] {
  const rows = incidents.filter((i) => i.incident_code === "i07");
  if (!rows.length) return [];
  const facts: SvReportFact[] = [];
  const fired = rows.filter((r) => r.fired !== false);
  if (!fired.length) return facts;

  const statuses = fired.map((r) =>
    (str(r.facts?.person_status) ?? str(r.facts?.identity_status) ?? "").toUpperCase(),
  );
  const tally = (value: string) => statuses.filter((s) => s === value).length;

  facts.push({ label: "Person detections", value: String(fired.length) });
  const unauthorized = tally("UNAUTHORIZED");
  if (unauthorized) {
    facts.push({ label: "Unauthorized", value: String(unauthorized), tone: "critical" });
  }
  const unverified = tally("UNVERIFIED");
  if (unverified) {
    facts.push({
      label: "Unverified",
      value: String(unverified),
      tone: "neutral",
      hint: "Identity could not be determined. Unverified is not an accusation.",
    });
  }
  const authorized = tally("AUTHORIZED");
  if (authorized) facts.push({ label: "Authorized", value: String(authorized), tone: "ok" });

  // Identity is shown only where the response already carries it.
  const names = Array.from(new Set(fired.map((r) => str(r.facts?.person_name)).filter(Boolean)));
  if (names.length) facts.push({ label: "Identified", value: names.join(", ") });

  const zones = Array.from(new Set(fired.map((r) => str(r.facts?.zone)).filter(Boolean)));
  if (zones.length) {
    facts.push({
      label: "Zone",
      value: zones.join(", "),
      hint: "SecureVision's own zone name — not joined to JNPA geo-fence zones.",
    });
  }
  return facts;
}

/** Camera-health keys the vendor may or may not send. Absent keys stay absent. */
const CAMERA_HEALTH_FIELDS: ReadonlyArray<readonly [RegExp, string, "issue" | "status"]> = [
  [/^(is_)?blur(red|ry|_detected)?$/i, "Blur", "issue"],
  [/^(is_)?obstruct(ed|ion)?$/i, "Obstruction", "issue"],
  [/^black_?(screen|frame|out)(_detected)?$/i, "Black screen", "issue"],
  [/^(is_)?frozen(_frame)?$|^frame_frozen$/i, "Frozen frame", "issue"],
  [/^fog(ging|ged|gy)?(_detected)?$/i, "Fogging", "issue"],
  [
    /^(bad_|poor_)?exposure(_issue|_problem)?$|^over_?exposed$|^under_?exposed$/i,
    "Exposure issue",
    "issue",
  ],
  [/^(is_)?tamper(ing|ed)?(_detected)?$/i, "Tampering", "issue"],
  [/^camera_(status|health|state)$|^(is_)?operational$/i, "Camera status", "status"],
];

function humanValue(value: unknown, kind: "issue" | "status"): SvReportFact | null {
  if (typeof value === "boolean") {
    if (kind === "status") {
      return {
        label: "",
        value: value ? "Operational" : "Not operational",
        tone: value ? "ok" : "critical",
      };
    }
    return {
      label: "",
      value: value ? "Detected" : "Not detected",
      tone: value ? "critical" : "ok",
    };
  }
  const text = str(value);
  if (text)
    return { label: "", value: text, tone: kind === "issue" ? tamperTone(text) : undefined };
  const n = num(value);
  if (n != null) return { label: "", value: String(n) };
  return null;
}

function cameraFacts(incidents: SvIncident[]): SvReportFact[] {
  const tamper = pick(incidents, "i12");
  if (!tamper) return [];
  const facts: SvReportFact[] = [];

  if (tamper.fired === false) {
    facts.push({ label: "AI tamper check", value: "No tamper condition", tone: "ok" });
  } else {
    const state = str(tamper.tamper?.tamper_state);
    if (state) {
      facts.push({ label: "AI tamper check", value: state, tone: tamperTone(state) });
    }
    const pct = num(tamper.tamper?.analytic_confidence_pct);
    if (pct != null) facts.push({ label: "Analytic confidence", value: `${pct}%` });
  }

  // Anything else the vendor reported about the feed, only where it exists.
  const seen = new Set(facts.map((f) => f.label));
  for (const [key, raw] of Object.entries(tamper.facts ?? {})) {
    const match = CAMERA_HEALTH_FIELDS.find(([re]) => re.test(key));
    if (!match) continue;
    const [, label, kind] = match;
    if (seen.has(label)) continue;
    const rendered = humanValue(raw, kind);
    if (!rendered) continue;
    seen.add(label);
    facts.push({ ...rendered, label });
  }
  return facts;
}

// --------------------------------------------------------------------- builder

/** The opening of the narrative, verbatim — never a re-written summary. */
export function narrativeSummary(segments: string[]): string | null {
  if (!segments.length) return null;
  const first = segments[0];
  if (first.length < 80 && segments[1] && first.length + segments[1].length <= 240) {
    return `${first} ${segments[1]}`;
  }
  return first;
}

/**
 * Build the operator-facing view model for one combined report.
 *
 * Sections with neither a structured fact nor a narrative fragment are omitted
 * entirely: an empty card would read as "checked, nothing found" when the truth
 * is that the analyzer never reported on it.
 */
export function buildCombinedReport(
  report: SvCombinedReport | null | undefined,
): SvStructuredReport {
  const incidents = report?.incidents ?? [];
  const narrative = report?.combined_description ?? null;
  const segments = splitNarrative(narrative);

  const notes: Record<SvReportSectionKey, string[]> = { vehicle: [], person: [], camera: [] };
  const other: string[] = [];
  const summary = narrativeSummary(segments);
  const riskAction = extractRiskAndAction(narrative);

  // The opening line is already shown as the summary; risk fragments are shown
  // by the risk block. Neither is repeated inside a section.
  const consumed = new Set<string>();
  if (summary) {
    consumed.add(segments[0]);
    if (summary !== segments[0] && segments[1]) consumed.add(segments[1]);
  }

  for (const segment of segments) {
    if (consumed.has(segment)) continue;
    const topic = classifySegment(segment);
    if (topic === "risk") {
      // Kept only when the risk block did not already state it.
      const stated =
        (riskAction.action && segment.includes(riskAction.action)) ||
        (riskAction.risk && new RegExp(`risk`, "i").test(segment)) ||
        (riskAction.priority && /priority/i.test(segment));
      if (!stated) other.push(segment);
      continue;
    }
    if (topic === "other") {
      other.push(segment);
      continue;
    }
    notes[topic].push(segment);
  }

  const built: Record<SvReportSectionKey, SvReportFact[]> = {
    vehicle: vehicleFacts(incidents),
    person: personFacts(incidents),
    camera: cameraFacts(incidents),
  };

  const sections: SvReportSection[] = (["vehicle", "person", "camera"] as const)
    .map((key) => ({
      key,
      title: SV_SECTION_TITLES[key],
      facts: built[key],
      notes: notes[key],
    }))
    .filter((section) => section.facts.length > 0 || section.notes.length > 0);

  const hasRisk = Boolean(riskAction.risk || riskAction.priority || riskAction.action);

  return {
    summary,
    sections,
    riskAction: hasRisk ? riskAction : null,
    narrative,
    aiGenerated: Boolean(report?.ai_generated),
    incidents,
    other,
  };
}
