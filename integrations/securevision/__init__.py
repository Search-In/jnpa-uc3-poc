"""SecureVision integration — AI video-surveillance platform (YOLOv11 incident
detection, annotated MJPEG replay, face recognition).

Layering mirrors :mod:`integrations.ulip` exactly:
  client.py     — the ONLY layer that speaks HTTP to SecureVision (service-account
                  login, cached token, one forced re-login on 401, timeouts,
                  bounded retries, typed errors, credentials never logged)
  schemas.py    — tolerant pydantic view over the vendor's answer shapes
  exceptions.py — typed failure vocabulary, including the three failures that
                  carry product meaning (409 analysis expired, 422 unprocessable,
                  503 model not loaded)

The credentials (SECUREVISION_USERNAME / SECUREVISION_PASSWORD) are
BACKEND-ONLY: read from the process environment, sent only from the gateway to
SecureVision, and never exposed to the frontend (no VITE_ variable, no browser
call, nothing in localStorage).

This is NOT optional hardening. SecureVision authenticates at /api/auth/login and
/api/auth/me — the same relative paths this application's own sign-in uses — so a
browser-direct call would collide with JNPA authentication. Everything reaches
the browser through the gateway's /api/sv/* namespace instead
(gateway/routers/securevision.py), authorised by the EXISTING JNPA RBAC.

Consumed by :mod:`services.securevision` (normalisation + camera mapping) and
gateway/routers/securevision.py. A SecureVision outage degrades the /api/sv/*
surfaces to a clearly-tagged unavailable state — it never breaks an existing
JNPA screen.
"""
from __future__ import annotations

from .client import (
    CONFLICT_ANALYSIS_EXPIRED,
    CONFLICT_DUPLICATE,
    DEFAULT_BASE_URL,
    SecureVisionClient,
)
from .exceptions import (
    SecureVisionAnalysisExpired,
    SecureVisionAuthError,
    SecureVisionConflict,
    SecureVisionError,
    SecureVisionForbidden,
    SecureVisionHTTPError,
    SecureVisionInvalidResponse,
    SecureVisionModelUnavailable,
    SecureVisionNotConfigured,
    SecureVisionNotFound,
    SecureVisionTimeout,
    SecureVisionUnavailable,
    SecureVisionUnprocessable,
)
from .schemas import (
    INCIDENT_PATHS,
    INCIDENT_TITLES,
    PERSON_AUTHORIZED,
    PERSON_STATUSES,
    PERSON_UNAUTHORIZED,
    PERSON_UNVERIFIED,
    SvCombinedReport,
    SvEvidenceImage,
    SvFaceEvent,
    SvFacePerson,
    SvFaceStatus,
    SvI07Person,
    SvI07Response,
    SvIncident,
    SvLoginResponse,
    SvUploadResult,
    SvUser,
)

__all__ = [
    "SecureVisionClient",
    "DEFAULT_BASE_URL",
    "CONFLICT_ANALYSIS_EXPIRED",
    "CONFLICT_DUPLICATE",
    "SecureVisionError",
    "SecureVisionNotConfigured",
    "SecureVisionTimeout",
    "SecureVisionUnavailable",
    "SecureVisionAuthError",
    "SecureVisionForbidden",
    "SecureVisionNotFound",
    "SecureVisionAnalysisExpired",
    "SecureVisionUnprocessable",
    "SecureVisionConflict",
    "SecureVisionModelUnavailable",
    "SecureVisionHTTPError",
    "SecureVisionInvalidResponse",
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
