// SecureVision types + pure presentation helpers.
//
// Every SecureVision surface in this app reads these types, never raw vendor
// JSON: the gateway (/api/sv/*) normalises the vendor's payloads once, and this
// module is the single client-side statement of that shape. No React/DOM here,
// so it is unit-testable with vitest (see securevision.test.ts) — the same split
// as lib/traffic.ts and lib/weather.ts.
//
// The SecureVision credential is BACKEND-ONLY. The browser never calls
// svapidev.phylon.in, never holds a vendor token, and never sees the vendor's
// /api/auth/* paths (which would collide with this app's own sign-in).

import type { Tone } from "@/components/ui/dtccc";
import { apiError } from "./api";

/** Attribution tag carried by every normalised SecureVision record. */
export const SV_SOURCE = "SecureVision";

/** The five incident analyzers, plus the combined report. */
export type SvIncidentCode = "i01" | "i02" | "i07" | "i09" | "i12" | "all";

/** The THREE person verdicts. UNVERIFIED is NOT a soft UNAUTHORIZED — it means
 *  the model could not get a clear read and is declining to accuse anyone. */
export type SvPersonStatus = "AUTHORIZED" | "UNAUTHORIZED" | "UNVERIFIED";

/** Container-number cross-check between SecureVision and our own ISO-6346
 *  validator (lib/iso6346.ts / jnpa_shared.iso6346). */
export type SvContainerAgreement = "MATCH" | "REVIEW" | "UNKNOWN";

/** Camera attribution. `mapped:false` is a real answer — the UI must say
 *  "Camera mapping unavailable" rather than invent a JNPA camera. */
export interface SvCamera {
  securevision_code: string | null;
  jnpa_camera_id: string | null;
  mapped: boolean;
  map_configured: boolean;
}

export interface SvEvidenceImage {
  region_type: string | null;
  url: string | null;
  crop_score: number | null;
  track_id: number | null;
}

export interface SvIncidentBase {
  source: string;
  analysis_id: string | null;
  incident_code: SvIncidentCode | null;
  incident_type: string | null;
  title: string | null;
  fired: boolean;
  status: string | null;
  validation_status: string | null;
  confidence: number | null;
  confidence_pct: number | null;
  ocr_confidence: number | null;
  ocr_confidence_pct: number | null;
  track_id: number | null;
  /** Seconds into the analysed clip — NOT a wall-clock instant. */
  clip_offset_s: number | null;
  /** Absolute time, derived from the upload time; null when unknown. */
  detected_at: string | null;
  camera: SvCamera;
  image_url: string | null;
  evidence: SvEvidenceImage[];
  description: string | null;
  ai_generated: boolean;
  vision_provider: string | null;
  processing_time_ms: number | null;
  facts: Record<string, unknown>;
}

export interface SvIncident extends SvIncidentBase {
  /** I-01 */
  plate?: {
    plate: string | null;
    plate_valid: boolean | null;
    vehicle_type: string | null;
    vehicle_color: string | null;
    validation: string | null;
    ocr_confidence: number | null;
  };
  /** I-02 */
  counts?: { vehicle_class: string | null; count: number | null }[];
  total_count?: number;
  /** I-09 */
  container?: {
    number: string | null;
    vendor_valid: boolean | null;
    jnpa_valid: boolean | null;
    agreement: SvContainerAgreement;
    container_detected: boolean | null;
    validation: string | null;
    plate: string | null;
    plate_detected: boolean | null;
  };
  /** I-12 */
  tamper?: { tamper_state: string | null; analytic_confidence_pct: number | null };
}

export interface SvPersonDetection {
  source: string;
  analysis_id: string | null;
  incident_code: "i07";
  incident_type: string | null;
  title: string | null;
  status: string | null;
  validation_status: string | null;
  confidence: number | null;
  confidence_pct: number | null;
  track_id: number | null;
  camera: SvCamera;
  image_url: string | null;
  person_status: SvPersonStatus;
  authorized: boolean | null;
  person_name: string | null;
  person_id: string | null;
  face_similarity: number | null;
  dwell_seconds: number | null;
  /** SecureVision's OWN zone name. Not joined to core.zone — no zone API exists. */
  zone: string | null;
  zone_source: string;
  description: string | null;
  detected_at: string | null;
  facts: Record<string, unknown>;
}

export interface SvPersonResult {
  source: string;
  analysis_id: string | null;
  incident_code: "i07";
  incident_type: string | null;
  fired: boolean;
  count: number;
  camera: SvCamera;
  persons: SvPersonDetection[];
}

export interface SvCombinedReport {
  source: string;
  analysis_id: string | null;
  camera: SvCamera;
  incidents: SvIncident[];
  /** Written by an external vision/LLM provider — always badge it. */
  combined_description: string | null;
  ai_generated: boolean;
  narrative_provenance: "AI_GENERATED" | "NONE";
}

export interface SvAnalysis {
  analysis_id: string;
  securevision_camera_code: string | null;
  jnpa_camera_id: string | null;
  camera_mapped: boolean;
  filename: string | null;
  frames_sampled: number | null;
  detection_pass_count: number | null;
  zones_loaded: number | null;
  uploaded_by: string | null;
  uploaded_at: string;
  /** Always false: nothing is written to RDS (see services/securevision). */
  persisted: boolean;
  camera?: SvCamera;
  /** zones_loaded === 0 -> zone-based I-07 cannot fire for this clip. */
  zone_warning?: boolean;
  source?: string;
}

export interface SvAnalysisList {
  analyses: SvAnalysis[];
  count: number;
  persisted: boolean;
  note: string;
}

export interface SvHealth {
  integration: string;
  configured: boolean;
  status: "LIVE" | "UNAVAILABLE" | "NOT_CONFIGURED";
  reachable?: boolean;
  base_url?: string;
  camera_map_configured: boolean;
  camera_map_entries: number;
  persistence: string;
  analyses_in_session: number;
  stream_tickets_outstanding: number;
  mode: string;
  service_account?: string;
  service_role?: string;
  error?: string;
  detail?: string;
  face_model?: {
    model_ready: boolean | null;
    model_name: string | null;
    provider: string | null;
    similarity_threshold: number | null;
    gallery_loaded: number | null;
    authorized_in_db: number | null;
  } | null;
}

export interface SvFaceModelStatus {
  configured: boolean;
  status: "READY" | "NOT_READY" | "NOT_CONFIGURED";
  model_ready: boolean;
  model_name?: string | null;
  provider?: string | null;
  similarity_threshold?: number | null;
  downscale?: number | null;
  authorized_in_db?: number | null;
  gallery_loaded?: number | null;
  authorized_names_count?: number;
  source?: string;
}

export interface SvPerson {
  source: string;
  id: number | null;
  person_id: string | null;
  name: string | null;
  role: string | null;
  department: string | null;
  is_active: boolean | null;
  created_at: string | null;
  /** Our own authenticated proxy path — never the vendor's filesystem path. */
  photo_url: string | null;
}

export interface SvFaceEvent {
  source: string;
  id: number | null;
  camera: SvCamera;
  person_id: string | null;
  name: string | null;
  authorized: boolean | null;
  person_status: SvPersonStatus;
  confidence: number | null;
  confidence_pct: number | null;
  incident_id: string | null;
  latitude: number | null;
  longitude: number | null;
  created_at: string | null;
  /** No documented endpoint serves event snapshots — render "no snapshot". */
  snapshot_available: boolean;
}

export interface SvStreamTicket {
  ticket: string;
  analysis_id: string;
  expires_in: number;
  stream_url: string;
}

export interface SvEnrollInput {
  person_id: string;
  name: string;
  role?: string;
  department?: string;
  photos: Blob[];
}

export interface SvCameraMapping {
  securevision_code: string;
  jnpa_camera_id: string;
}

// --------------------------------------------------------------- presentation

/** Tone for a person verdict.
 *
 *  UNVERIFIED is deliberately NOT critical: colouring "we could not tell" the
 *  same as "not allowed here" is how an unread face becomes an accusation. */
export function personVerdictTone(status: SvPersonStatus | null | undefined): Tone {
  if (status === "AUTHORIZED") return "ok";
  if (status === "UNAUTHORIZED") return "critical";
  return "neutral";
}

export function personVerdictLabel(status: SvPersonStatus | null | undefined): string {
  if (status === "AUTHORIZED") return "Authorized";
  if (status === "UNAUTHORIZED") return "Unauthorized";
  return "Unverified";
}

/** Longer copy for a tooltip, so the third state is never ambiguous. */
export function personVerdictHint(status: SvPersonStatus | null | undefined): string {
  if (status === "AUTHORIZED") return "Face matched an enrolled person.";
  if (status === "UNAUTHORIZED") return "A face was detected but matched nobody enrolled.";
  return "No clear face was visible, or the face model was not ready — identity was not determined.";
}

export function containerAgreementTone(agreement: SvContainerAgreement | null | undefined): Tone {
  if (agreement === "MATCH") return "ok";
  if (agreement === "REVIEW") return "warn";
  return "neutral";
}

export function containerAgreementLabel(
  agreement: SvContainerAgreement | null | undefined,
): string {
  if (agreement === "MATCH") return "Validation: MATCH";
  if (agreement === "REVIEW") return "Validation: REVIEW";
  return "Validation: UNKNOWN";
}

/** Any tamper verdict is worth attention; a clean read is not reported at all. */
export function tamperTone(state: string | null | undefined): Tone {
  if (!state) return "neutral";
  return state.toUpperCase() === "OK" ? "ok" : "critical";
}

export function validationTone(status: string | null | undefined): Tone {
  const v = (status ?? "").toUpperCase();
  if (v === "PASSED") return "ok";
  if (v === "FAILED") return "critical";
  return "neutral";
}

/** "93%" / "—". Accepts either a 0–1 confidence or an already-percent value. */
export function fmtConfidence(value: number | null | undefined): string {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  return `${Math.round(n <= 1 ? n * 100 : n)}%`;
}

/** "8.4 s" / "—" for a dwell time. */
export function fmtDwell(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(Number(seconds))) return "—";
  return `${Number(seconds).toFixed(1)} s`;
}

/** Clip-relative offset, labelled so it can never read as a wall-clock time. */
export function fmtClipOffset(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(Number(seconds))) return "—";
  return `+${Number(seconds).toFixed(1)}s into clip`;
}

/** What to show for a camera. An unmapped code shows the vendor's own code and
 *  says the mapping is missing — it never guesses a JNPA camera. */
export function cameraLabel(camera: SvCamera | null | undefined): string {
  if (!camera) return "—";
  if (camera.mapped && camera.jnpa_camera_id) return camera.jnpa_camera_id;
  return camera.securevision_code ?? "—";
}

export function cameraHint(camera: SvCamera | null | undefined): string | null {
  if (!camera || camera.mapped) return null;
  return camera.map_configured
    ? `Camera mapping unavailable — SecureVision "${camera.securevision_code ?? "?"}" is not mapped to a JNPA camera.`
    : "Camera mapping unavailable — SECUREVISION_CAMERA_MAP is not configured.";
}

export function incidentTitle(code: SvIncidentCode | null | undefined): string {
  switch (code) {
    case "i01":
      return "Trailer Plate Capture";
    case "i02":
      return "Vehicle Classification & Count";
    case "i07":
      return "Person in Restricted/Machinery Zone";
    case "i09":
      return "Container ISO 6346";
    case "i12":
      return "Camera Health / Tamper";
    case "all":
      return "Combined Incident Report";
    default:
      return "SecureVision incident";
  }
}

/** True when the failure is "the vendor evicted this analysis's frames" — the
 *  one error the UI must answer with a re-run action rather than a retry. */
export function isAnalysisExpired(err: unknown): boolean {
  const info = apiError(err);
  return info.code === "analysis_expired" || info.status === 409;
}

/** True when SecureVision simply is not wired up in this deployment. */
export function isNotConfigured(err: unknown): boolean {
  return apiError(err).code === "securevision_not_configured";
}

/** Operator-facing copy for a SecureVision failure.
 *
 *  Vendor stack traces and httpx internals never reach a user; each typed code
 *  gets a sentence that says what happened and what to do next. */
export function svErrorMessage(err: unknown): string {
  const info = apiError(err);
  switch (info.code) {
    case "securevision_not_configured":
      return "SecureVision is not configured on this deployment.";
    case "securevision_unavailable":
      return "SecureVision is unreachable. The rest of the console is unaffected.";
    case "securevision_timeout":
      return "SecureVision did not respond in time. Try again.";
    case "securevision_auth_failed":
      return "SecureVision rejected the service credential. Contact an administrator.";
    case "securevision_forbidden":
      return "The SecureVision service account lacks the required role for this action.";
    case "analysis_expired":
      return "Analysis frames expired — re-run analysis to view the replay.";
    case "person_already_enrolled":
      return "That person ID is already enrolled in SecureVision.";
    case "securevision_unprocessable":
      return info.detail || "SecureVision could not process the upload.";
    case "face_model_unavailable":
      return "The SecureVision face model is not loaded. Enrolment is unavailable.";
    case "securevision_not_found":
      return "SecureVision no longer has this record.";
    case "camera_mapping_unavailable":
      return "No SecureVision camera is mapped to that JNPA camera.";
    case "file_too_large":
      return "The clip is larger than this deployment's upload limit.";
    case "not_a_video":
      return "That file is not a recognised video.";
    case "not_an_image":
      return "That file is not a recognised image.";
    case "forbidden":
      return info.detail || "Your role does not permit this action.";
    default:
      return info.detail || "SecureVision request failed.";
  }
}
