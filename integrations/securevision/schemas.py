"""Tolerant pydantic view over the SecureVision answer shapes.

Every model is deliberately permissive (``extra="allow"``, everything optional
with a default) for the same reason the other vendor schemas in this package
are: a vendor adding a field must never turn a working screen into a 500. The
models exist to give the rest of the backend a *typed* object instead of raw
vendor JSON — not to police the vendor's contract.

Two shapes are documented, not one. Most incident endpoints answer a single
"fired / not fired" envelope (:class:`SvIncident`); I-07 answers a per-person
list (:class:`SvI07Response`) because a restricted-zone read is one verdict per
human, not one verdict per clip. They are kept as separate models rather than a
union with optional halves, because the UI genuinely renders them differently.

Nothing here rewrites URLs, maps camera codes or converts timestamps — those
are *our* business decisions and live in :mod:`services.securevision.normalize`.
This module only parses.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------- constants

#: Incident codes this integration knows how to request, mapped to the vendor's
#: path segment. Anything not in this map is refused before a call is made, so a
#: typo'd code can never reach the vendor as an arbitrary path.
INCIDENT_PATHS: Dict[str, str] = {
    "i01": "i01",
    "i02": "i02",
    "i07": "i07",
    "i09": "i09",
    "i12": "i12",
    "all": "all",
}

#: Human titles, used when the vendor omits ``title`` on a not-fired envelope.
INCIDENT_TITLES: Dict[str, str] = {
    "i01": "Trailer Plate Capture",
    "i02": "Vehicle Classification & Count",
    "i07": "Person in Restricted/Machinery Zone",
    "i09": "Container ISO 6346",
    "i12": "Camera Health / Tamper",
}

#: The THREE person verdicts. UNVERIFIED means the system could not get a clear
#: read and is explicitly declining to accuse anyone — it must never be
#: collapsed into UNAUTHORIZED anywhere in this codebase.
PERSON_AUTHORIZED = "AUTHORIZED"
PERSON_UNAUTHORIZED = "UNAUTHORIZED"
PERSON_UNVERIFIED = "UNVERIFIED"
PERSON_STATUSES = (PERSON_AUTHORIZED, PERSON_UNAUTHORIZED, PERSON_UNVERIFIED)


class _Tolerant(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# --------------------------------------------------------------------- auth
class SvUser(_Tolerant):
    id: Optional[int] = None
    username: Optional[str] = None
    email: Optional[str] = None
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    created_at: Optional[str] = None


class SvLoginResponse(_Tolerant):
    access_token: Optional[str] = None
    token_type: Optional[str] = None
    user: Optional[SvUser] = None


# ---------------------------------------------------------------- analytics
class SvUploadResult(_Tolerant):
    analysis_id: Optional[str] = None
    camera_code: Optional[str] = None
    frames_sampled: Optional[int] = None
    detection_pass_count: Optional[int] = None
    zones_loaded: Optional[int] = None


class SvEvidenceImage(_Tolerant):
    region_type: Optional[str] = None
    url: Optional[str] = None
    crop_score: Optional[float] = None
    track_id: Optional[int] = None


class SvIncident(_Tolerant):
    """The common envelope for I-01 / I-02 / I-09 / I-12 (and each element of
    the combined report). ``facts`` stays an open dict — its shape varies per
    incident type by design, and the per-type readers live in the service
    layer."""

    analysis_id: Optional[str] = None
    fired: Optional[bool] = None
    incident_type: Optional[str] = None
    title: Optional[str] = None
    status: Optional[str] = None
    confidence: Optional[float] = None
    validation_status: Optional[str] = None
    ocr_confidence: Optional[float] = None
    camera_code: Optional[str] = None
    # Seconds into the analysed clip, NOT a wall-clock instant. Named as the
    # vendor names it; the service layer derives an absolute time from it.
    timestamp: Optional[float] = None
    track_id: Optional[int] = None
    snapshot: Optional[str] = None
    image_url: Optional[str] = None
    evidence: List[SvEvidenceImage] = Field(default_factory=list)
    evidence_images: List[SvEvidenceImage] = Field(default_factory=list)
    vision: Optional[Any] = None
    facts: Dict[str, Any] = Field(default_factory=dict)
    reason: Optional[str] = None
    description: Optional[str] = None
    ai_generated: Optional[bool] = None
    processing_time_ms: Optional[float] = None
    vision_provider: Optional[str] = None
    prompt_version: Optional[str] = None


class SvI07Person(_Tolerant):
    incident_type: Optional[str] = None
    title: Optional[str] = None
    status: Optional[str] = None
    confidence: Optional[float] = None
    validation_status: Optional[str] = None
    camera_code: Optional[str] = None
    track_id: Optional[int] = None
    image_url: Optional[str] = None
    facts: Dict[str, Any] = Field(default_factory=dict)
    description: Optional[str] = None


class SvI07Response(_Tolerant):
    analysis_id: Optional[str] = None
    camera_code: Optional[str] = None
    incident_type: Optional[str] = None
    fired: Optional[bool] = None
    count: Optional[int] = None
    persons: List[SvI07Person] = Field(default_factory=list)


class SvCombinedReport(_Tolerant):
    analysis_id: Optional[str] = None
    camera_code: Optional[str] = None
    incidents: List[SvIncident] = Field(default_factory=list)
    #: AI-authored narrative. ALWAYS rendered behind an "AI-generated" badge —
    #: it is a language-model summary, not a machine-verified fact.
    combined_description: Optional[str] = None
    ai_generated: Optional[bool] = None


# --------------------------------------------------------------------- face
class SvFacePerson(_Tolerant):
    id: Optional[int] = None
    person_id: Optional[str] = None
    name: Optional[str] = None
    role: Optional[str] = None
    department: Optional[str] = None
    #: Vendor-side filesystem path. NEVER surfaced to the browser — the photo is
    #: served through our own /api/sv/faces/{pk}/photo proxy.
    snapshot_path: Optional[str] = None
    is_active: Optional[bool] = None
    created_at: Optional[str] = None


class SvFaceEvent(_Tolerant):
    id: Optional[int] = None
    camera_id: Optional[str] = None
    person_id: Optional[str] = None
    name: Optional[str] = None
    authorized: Optional[bool] = None
    confidence: Optional[float] = None
    snapshot_path: Optional[str] = None
    incident_id: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    created_at: Optional[str] = None


class SvFaceStatus(_Tolerant):
    # ``model_ready`` / ``model_name`` collide with pydantic's protected
    # ``model_`` namespace. The vendor names them, so we keep the names and
    # disable the guard for this model only.
    model_config = ConfigDict(extra="allow", populate_by_name=True,
                              protected_namespaces=())

    model_ready: Optional[bool] = None
    model_name: Optional[str] = None
    provider: Optional[str] = None
    similarity_threshold: Optional[float] = None
    downscale: Optional[float] = None
    authorized_in_db: Optional[int] = None
    gallery_loaded: Optional[int] = None
    authorized_names: List[str] = Field(default_factory=list)


__all__ = [
    "INCIDENT_PATHS",
    "INCIDENT_TITLES",
    "PERSON_AUTHORIZED",
    "PERSON_UNAUTHORIZED",
    "PERSON_UNVERIFIED",
    "PERSON_STATUSES",
    "SvUser",
    "SvLoginResponse",
    "SvUploadResult",
    "SvEvidenceImage",
    "SvIncident",
    "SvI07Person",
    "SvI07Response",
    "SvCombinedReport",
    "SvFacePerson",
    "SvFaceEvent",
    "SvFaceStatus",
]
