"""Typed SecureVision failure vocabulary.

Every failure the client can produce is one of these, so the gateway router can
map vendor failures onto HTTP answers in ONE place and callers never have to
know about httpx internals. Mirrors :mod:`integrations.ulip.exceptions`, with
three additions that carry real product meaning rather than a bare status code:

  * :class:`SecureVisionAnalysisExpired` (409) — the vendor evicted the sampled
    frames for an analysis (e.g. it restarted). The incident RESULTS survive;
    only the replay stream is gone. The UI must offer "re-run analysis" instead
    of a broken image, so this cannot be flattened into a generic HTTP error.
  * :class:`SecureVisionUnprocessable` (422) — the upload/enrolment was refused
    on its content (no usable face, unreadable clip). A user-fixable problem.
  * :class:`SecureVisionModelUnavailable` (503) — the face model is not loaded
    upstream. Not our outage, and retrying immediately cannot help.
"""
from __future__ import annotations

from typing import Optional


class SecureVisionError(Exception):
    """Base class for every SecureVision client failure."""


class SecureVisionNotConfigured(SecureVisionError):
    """No SecureVision credential configured (SECUREVISION_USERNAME +
    SECUREVISION_PASSWORD) — the integration is disabled, not broken."""


class SecureVisionTimeout(SecureVisionError):
    """The request exceeded the configured timeout budget (after retries)."""


class SecureVisionUnavailable(SecureVisionError):
    """Network-level failure — DNS, refused connection, TLS, dropped socket."""


class SecureVisionAuthError(SecureVisionError):
    """Authentication failed — /api/auth/login was rejected, or an issued token
    was refused with 401 even after one forced re-login. Retrying with the same
    credentials cannot help."""


class SecureVisionForbidden(SecureVisionError):
    """403 — the SecureVision service account lacks the role the endpoint needs
    (e.g. face enrolment requires super_admin/security_manager/shift_supervisor).
    A configuration problem on the vendor side, not a caller problem."""


class SecureVisionNotFound(SecureVisionError):
    """404 — unknown analysis_id / person / photo."""


class SecureVisionAnalysisExpired(SecureVisionError):
    """409 — the analysis is no longer warm in the vendor's memory, so its
    sampled frames cannot be replayed. Incident results are unaffected."""


class SecureVisionUnprocessable(SecureVisionError):
    """422 — the uploaded content was rejected (no usable face in the photo,
    undecodable clip). ``reason`` carries the vendor's explanation when given."""

    def __init__(self, reason: Optional[str] = None) -> None:
        self.reason = reason
        super().__init__(reason or "SecureVision could not process the upload")


class SecureVisionConflict(SecureVisionError):
    """409 on a non-analysis resource — currently only a duplicate
    ``person_id`` on face enrolment."""

    def __init__(self, reason: Optional[str] = None) -> None:
        self.reason = reason
        super().__init__(reason or "SecureVision reports a conflicting resource")


class SecureVisionModelUnavailable(SecureVisionError):
    """503 — the face-recognition model is not loaded upstream."""


class SecureVisionHTTPError(SecureVisionError):
    """Any other non-2xx response. ``reason`` carries the vendor's ``detail``
    when the body exposes one."""

    def __init__(self, status_code: int, reason: Optional[str] = None) -> None:
        self.status_code = status_code
        self.reason = reason
        super().__init__(
            f"SecureVision HTTP {status_code}: {reason or 'no reason given'}")

    @property
    def is_auth_error(self) -> bool:
        return self.status_code in (401, 403)


class SecureVisionInvalidResponse(SecureVisionError):
    """A 2xx response whose body is not valid JSON, or does not match the
    tolerant schema for the endpoint."""


__all__ = [
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
]
