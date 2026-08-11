"""Normalisation of SecureVision vendor payloads into the shapes our screens read.

One module owns every vendor-specific decision, so no screen ever parses raw
SecureVision JSON and no two screens can drift in how they read it:

  * **Media URLs.** The vendor answers ``/media/...`` (servable, but only with a
    bearer token) and ``/data/snapshots/...`` (its own filesystem, not servable
    at all). Only the former is rewritten onto our authenticated media proxy;
    the latter is dropped rather than handed to a browser that would 404 on it.
  * **Timestamps.** ``timestamp`` is *seconds into the analysed clip*. We keep it
    as ``clip_offset_s`` and, when the gateway knows when the clip was uploaded,
    derive an absolute ``detected_at``. We never present a clip offset as a
    wall-clock instant.
  * **Cameras.** Every camera code goes through the explicit mapping layer, which
    answers "not mapped" rather than guessing (services/securevision/cameras.py).
  * **Container numbers.** The vendor's ``container_valid`` is kept as the
    vendor's claim, and cross-checked against our own shared ISO-6346 validator.
    Agreement is reported (MATCH / REVIEW); the vendor's value is never silently
    overridden.
  * **Person verdicts.** AUTHORIZED / UNAUTHORIZED / UNVERIFIED are preserved
    exactly. An unreadable or missing verdict degrades to UNVERIFIED — never to
    UNAUTHORIZED, which would accuse someone on the strength of missing data.

Everything returned is a plain JSON-safe dict, tagged ``source: "SECUREVISION"``
so the UI can attribute it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from jnpa_shared.iso6346 import is_valid_container_no

from integrations.securevision import (
    PERSON_AUTHORIZED,
    PERSON_STATUSES,
    PERSON_UNVERIFIED,
    SvCombinedReport,
    SvFaceEvent,
    SvFacePerson,
    SvI07Response,
    SvIncident,
)
from integrations.securevision.schemas import INCIDENT_TITLES

from . import analyses, cameras

#: Attribution tag carried by every normalised record. The UI renders it as a
#: "SecureVision" source badge so vendor data is never mistaken for JNPA data.
SOURCE = "SECUREVISION"

#: Prefix the vendor serves analysis media under, and our proxy prefix for it.
VENDOR_MEDIA_PREFIX = "/media/"
PROXY_MEDIA_PREFIX = "/api/sv/media/"

#: Container-validation agreement verdicts.
AGREE_MATCH = "MATCH"
AGREE_REVIEW = "REVIEW"
AGREE_UNKNOWN = "UNKNOWN"


# ------------------------------------------------------------------ helpers
def media_url(raw: Optional[str]) -> Optional[str]:
    """Rewrite a vendor media path onto our proxy, or drop it.

    Accepts ``/media/...`` and absolute URLs ending in that path. Anything else
    — notably ``/data/snapshots/...``, which is the vendor's private filesystem
    — returns None: exposing a path the browser cannot fetch would produce a
    broken image and leak the vendor's internal layout.
    """
    if not raw or not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    idx = value.find(VENDOR_MEDIA_PREFIX)
    if idx == -1:
        return None
    tail = value[idx + len(VENDOR_MEDIA_PREFIX):].lstrip("/")
    if not tail or ".." in tail:
        return None
    return f"{PROXY_MEDIA_PREFIX}{tail}"


def _pct(value: Optional[float]) -> Optional[float]:
    """Confidence as a percentage. The vendor reports 0..1 in incident
    envelopes and 0..100 in ``analytic_confidence_pct``; both are accepted."""
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return round(num * 100.0, 1) if num <= 1.0 else round(num, 1)


def _absolute_time(uploaded_at: Optional[str],
                   clip_offset_s: Optional[float]) -> Optional[str]:
    """Wall-clock instant for a clip-relative offset, or None when the upload
    time is unknown (e.g. an analysis produced by a different gateway process)."""
    if not uploaded_at or clip_offset_s is None:
        return None
    try:
        base = datetime.fromisoformat(uploaded_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    try:
        return (base + timedelta(seconds=float(clip_offset_s))).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _evidence(items) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in items or []:
        url = media_url(getattr(item, "url", None))
        if not url:
            continue
        out.append({
            "region_type": getattr(item, "region_type", None),
            "url": url,
            "crop_score": getattr(item, "crop_score", None),
            "track_id": getattr(item, "track_id", None),
        })
    return out


def _incident_code(incident_type: Optional[str]) -> Optional[str]:
    """``"I-01"`` -> ``"i01"``. Used to key UI components off a stable code."""
    if not incident_type:
        return None
    return incident_type.replace("-", "").strip().lower() or None


# ------------------------------------------------------- container agreement
def container_agreement(number: Optional[str],
                        vendor_valid: Optional[bool]) -> Dict[str, Any]:
    """Cross-check a vendor container read against our own ISO-6346 validator.

    Both verdicts are reported. When they disagree the row is flagged REVIEW —
    a human decides, and the vendor's answer is left intact. Silently preferring
    either side would hide a genuine read failure.
    """
    normalized = (number or "").strip().upper().replace(" ", "")
    if not normalized:
        return {"number": None, "vendor_valid": vendor_valid, "jnpa_valid": None,
                "agreement": AGREE_UNKNOWN}
    jnpa_valid = is_valid_container_no(normalized)
    if vendor_valid is None:
        agreement = AGREE_UNKNOWN
    else:
        agreement = AGREE_MATCH if bool(vendor_valid) == jnpa_valid else AGREE_REVIEW
    return {
        "number": normalized,
        "vendor_valid": vendor_valid,
        "jnpa_valid": jnpa_valid,
        "agreement": agreement,
    }


# --------------------------------------------------------------- incidents
def normalize_incident(incident: SvIncident, *, code: Optional[str] = None,
                       analysis_id: Optional[str] = None) -> Dict[str, Any]:
    """One single-envelope incident (I-01 / I-02 / I-09 / I-12, or one element
    of the combined report) in the shape every screen consumes."""
    facts: Dict[str, Any] = incident.facts or {}
    resolved_code = (code or _incident_code(incident.incident_type) or "").lower()
    aid = incident.analysis_id or analysis_id
    offset = incident.timestamp
    camera_code = incident.camera_code or facts.get("camera_code")

    out: Dict[str, Any] = {
        "source": SOURCE,
        "analysis_id": aid,
        "incident_code": resolved_code or None,
        "incident_type": incident.incident_type,
        "title": incident.title or INCIDENT_TITLES.get(resolved_code),
        "fired": bool(incident.fired) if incident.fired is not None else True,
        "status": incident.status,
        "validation_status": incident.validation_status,
        "confidence": incident.confidence,
        "confidence_pct": _pct(incident.confidence),
        "ocr_confidence": incident.ocr_confidence,
        "ocr_confidence_pct": _pct(incident.ocr_confidence),
        "track_id": incident.track_id,
        "clip_offset_s": offset,
        "detected_at": _absolute_time(analyses.uploaded_at(aid) if aid else None,
                                      offset),
        "camera": cameras.describe(camera_code),
        "image_url": media_url(incident.image_url),
        "evidence": _evidence(incident.evidence or incident.evidence_images),
        "description": incident.description,
        # The narrative is written by an external vision/LLM provider. Carried
        # through so the UI can badge it; never presented as verified fact.
        "ai_generated": bool(incident.ai_generated),
        "vision_provider": incident.vision_provider,
        "processing_time_ms": incident.processing_time_ms,
        "facts": facts,
    }

    if resolved_code == "i01":
        out["plate"] = {
            "plate": facts.get("plate"),
            "plate_valid": facts.get("plate_valid"),
            "vehicle_type": facts.get("vehicle_type"),
            "vehicle_color": facts.get("vehicle_color"),
            "validation": facts.get("validation") or incident.validation_status,
            "ocr_confidence": facts.get("ocr_confidence", incident.ocr_confidence),
        }
    elif resolved_code == "i02":
        counts = facts.get("counts")
        rows = [
            {"vehicle_class": row.get("class"), "count": row.get("count")}
            for row in counts if isinstance(row, dict)
        ] if isinstance(counts, list) else []
        out["counts"] = rows
        out["total_count"] = sum(int(r["count"]) for r in rows
                                 if isinstance(r.get("count"), (int, float)))
    elif resolved_code == "i09":
        out["container"] = {
            **container_agreement(facts.get("container_number"),
                                  facts.get("container_valid")),
            "container_detected": facts.get("container_detected"),
            "validation": facts.get("validation"),
            # The same payload carries the towing vehicle's plate — this is the
            # container <-> vehicle join the Vehicle 360 needs.
            "plate": facts.get("plate"),
            "plate_detected": facts.get("plate_detected"),
        }
    elif resolved_code == "i12":
        out["tamper"] = {
            "tamper_state": facts.get("tamper_state"),
            "analytic_confidence_pct": _pct(facts.get("analytic_confidence_pct")),
        }
    return out


def normalize_person_status(raw: Optional[str],
                            authorized: Optional[bool]) -> str:
    """The person verdict, degrading safely.

    An unrecognised or missing verdict becomes UNVERIFIED, never UNAUTHORIZED:
    "we could not tell" and "this person is not allowed here" are different
    statements, and only one of them is an accusation.
    """
    value = (raw or "").strip().upper()
    if value in PERSON_STATUSES:
        return value
    if authorized is True:
        return PERSON_AUTHORIZED
    return PERSON_UNVERIFIED


def normalize_i07(response: SvI07Response, *,
                  analysis_id: Optional[str] = None) -> Dict[str, Any]:
    """I-07 — one verdict per detected person.

    Person names and identifiers are personal data. They are passed through for
    the restricted-zone security purpose only; the caller is responsible for the
    DPDP audit record (see gateway/routers/securevision.py).
    """
    aid = response.analysis_id or analysis_id
    uploaded = analyses.uploaded_at(aid) if aid else None
    persons: List[Dict[str, Any]] = []
    for person in response.persons or []:
        facts: Dict[str, Any] = person.facts or {}
        status = normalize_person_status(
            facts.get("person_status") or facts.get("identity_status"),
            facts.get("authorized"),
        )
        persons.append({
            "source": SOURCE,
            "analysis_id": aid,
            "incident_code": "i07",
            "incident_type": person.incident_type or "I-07",
            "title": person.title or INCIDENT_TITLES["i07"],
            "status": person.status,
            "validation_status": person.validation_status,
            "confidence": person.confidence,
            "confidence_pct": _pct(person.confidence),
            "track_id": person.track_id,
            "camera": cameras.describe(person.camera_code
                                       or facts.get("camera_code")),
            "image_url": media_url(person.image_url),
            "person_status": status,
            "authorized": facts.get("authorized"),
            "person_name": facts.get("person_name"),
            "person_id": facts.get("person_id"),
            "face_similarity": facts.get("face_similarity"),
            "dwell_seconds": facts.get("dwell_seconds"),
            # SecureVision zones are the vendor's own; they are NOT joined to
            # core.zone (no zone API exists to join them by).
            "zone": facts.get("zone"),
            "zone_source": SOURCE,
            "description": person.description,
            "detected_at": _absolute_time(uploaded, None),
            "facts": facts,
        })
    return {
        "source": SOURCE,
        "analysis_id": aid,
        "incident_code": "i07",
        "incident_type": response.incident_type or "I-07",
        "fired": bool(response.fired),
        "count": response.count if response.count is not None else len(persons),
        "camera": cameras.describe(response.camera_code),
        "persons": persons,
    }


def normalize_combined(report: SvCombinedReport, *,
                       analysis_id: Optional[str] = None) -> Dict[str, Any]:
    """The combined report: every analyzer that fired plus the AI narrative."""
    aid = report.analysis_id or analysis_id
    return {
        "source": SOURCE,
        "analysis_id": aid,
        "camera": cameras.describe(report.camera_code),
        "incidents": [normalize_incident(item, analysis_id=aid)
                      for item in (report.incidents or [])],
        "combined_description": report.combined_description,
        # Explicit and separate from the text, so a UI cannot render the
        # narrative without also being able to render its provenance.
        "ai_generated": bool(report.ai_generated),
        "narrative_provenance": "AI_GENERATED" if report.ai_generated else "NONE",
    }


# -------------------------------------------------------------------- faces
def normalize_face_person(person: SvFacePerson) -> Dict[str, Any]:
    """One enrolled person. ``snapshot_path`` (the vendor's filesystem) is
    deliberately dropped in favour of our own authenticated photo proxy."""
    return {
        "source": SOURCE,
        "id": person.id,
        "person_id": person.person_id,
        "name": person.name,
        "role": person.role,
        "department": person.department,
        "is_active": person.is_active,
        "created_at": person.created_at,
        "photo_url": f"/api/sv/faces/{person.id}/photo" if person.id is not None else None,
    }


def normalize_face_event(event: SvFaceEvent) -> Dict[str, Any]:
    """One face-recognition event.

    The vendor's ``snapshot_path`` is a private filesystem path and there is no
    documented endpoint that serves it, so no snapshot URL is offered — the flag
    tells the UI to render "no snapshot available" instead of a broken image.
    """
    return {
        "source": SOURCE,
        "id": event.id,
        "camera": cameras.describe(event.camera_id),
        "person_id": event.person_id,
        "name": event.name,
        "authorized": event.authorized,
        "person_status": normalize_person_status(None, event.authorized),
        "confidence": event.confidence,
        "confidence_pct": _pct(event.confidence),
        "incident_id": event.incident_id,
        "latitude": event.latitude,
        "longitude": event.longitude,
        "created_at": event.created_at,
        "snapshot_available": False,
    }


__all__ = [
    "SOURCE",
    "AGREE_MATCH",
    "AGREE_REVIEW",
    "AGREE_UNKNOWN",
    "media_url",
    "container_agreement",
    "normalize_incident",
    "normalize_person_status",
    "normalize_i07",
    "normalize_combined",
    "normalize_face_person",
    "normalize_face_event",
]
