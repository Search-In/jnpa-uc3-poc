"""/api/sv — SecureVision AI video analytics + face recognition, proxied.

ADDITIVE. Nothing in this module changes an existing JNPA endpoint, and no
existing module imports it.

**Why a proxy at all.** SecureVision authenticates at ``/api/auth/login`` and
``/api/auth/me`` — the same relative paths this application's own sign-in uses
(gateway/routers/auth.py). The SPA calls relative ``/api/*``, so a browser-direct
integration would collide with JNPA authentication. Everything therefore reaches
the browser under this ``/api/sv`` namespace, authorised by the EXISTING JNPA
RBAC (gateway/auth.py ``_POLICY``), while the SecureVision service credential
stays in this process and never touches a browser.

Surface:

    GET    /api/sv/health                              integration posture
    GET    /api/sv/analyses                            uploads from THIS process
    POST   /api/sv/analytics/video/upload              clip -> analysis_id
    DELETE /api/sv/analytics/video/{analysis_id}       drop one cached analysis
    GET    /api/sv/analytics/incident/{code}           i01|i02|i07|i09|i12|all
    POST   /api/sv/analytics/video/{id}/stream-ticket  mint a stream credential
    GET    /api/sv/analytics/video/{id}/stream         MJPEG relay (ticketed)
    GET    /api/sv/media/{path}                        evidence/snapshot proxy
    GET    /api/sv/faces, /faces/events, /faces/status, /faces/{pk}[/photo]
    POST   /api/sv/faces, PATCH/DELETE /api/sv/faces/{pk}

Two deliberate omissions:

  * **SecureVision user management** (``/api/auth/users``). This integration uses
    ONE service account; per-user vendor accounts would mean two account
    lifecycles to keep in sync and a vendor role (``security_guard``) with no JNPA
    equivalent. Authorisation is decided by JNPA ``_POLICY``, so the vendor's user
    API buys nothing here. Not implemented, by decision — not by oversight.
  * **Bulk ``DELETE /api/analytics/video``.** It wipes every analysis
    irreversibly with no server-side confirmation. It has no place behind an API
    a browser can reach.

A SecureVision outage degrades these routes to a clearly-typed error; it never
affects any other JNPA surface.
"""
from __future__ import annotations

import os
from time import perf_counter
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Body,
    File,
    Form,
    HTTPException,
    Path as PathParam,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse

from integrations.securevision import (
    SecureVisionAnalysisExpired,
    SecureVisionAuthError,
    SecureVisionClient,
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
from services.securevision import analyses, cameras, normalize, tickets
from services.securevision.history import VideoAnalysisHistory

from ..auth import CONTROL_ROOM, Role
from ..dpdp import audit_identity_access, enforce_dpdp
from ..logging import get_logger
from ..metrics import REQUESTS
from ..upload_limits import MAX_UPLOAD_BYTES

log = get_logger("gateway.securevision")

router = APIRouter(prefix="/api/sv", tags=["securevision"])

#: Incident codes this router will forward. Anything else is refused here, so a
#: caller can never steer a path segment at the vendor.
_INCIDENT_CODES = ("i01", "i02", "i07", "i09", "i12", "all")

#: Roles allowed to enrol/edit/remove site personnel. Mirrors the existing
#: /api/identity policy (gateway/auth.py) — biometric-adjacent administration is
#: customs + admin, never the whole control room.
_FACE_ADMIN_ROLES = frozenset({Role.CUSTOMS.value, Role.DTCCC_ADMIN.value})

#: Roles allowed to upload a clip or delete an analysis (a GPU-cost action).
_ANALYSIS_WRITE_ROLES = CONTROL_ROOM | {Role.CUSTOMS.value}

#: Accepted clip container types. Checked against BOTH the declared content-type
#: and the file's magic bytes — a declared type alone is caller-controlled.
_VIDEO_CONTENT_TYPES = (
    "video/mp4", "video/quicktime", "video/x-msvideo", "video/x-matroska",
    "video/mpeg", "video/webm", "application/octet-stream",
)
_IMAGE_CONTENT_TYPES = ("image/jpeg", "image/png", "image/webp")

_client: Optional[SecureVisionClient] = None


def get_client() -> SecureVisionClient:
    """The process-wide client (its cached login token is the point of reuse)."""
    global _client
    if _client is None:
        _client = SecureVisionClient()
    return _client


def reset_client() -> None:
    """Drop the cached client (tests)."""
    global _client
    _client = None


_history: Optional[VideoAnalysisHistory] = None


def get_history(request: Request) -> VideoAnalysisHistory:
    """The Video Analytics history service, bound to the gateway's RDS DSN.

    Durable (core.video_analysis) with the in-process registry as the degraded
    rung — see services/securevision/history.py.
    """
    global _history
    if _history is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _history = VideoAnalysisHistory(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _history


def reset_history() -> None:
    """Drop the cached history service (tests)."""
    global _history
    _history = None


# --------------------------------------------------------------------- guards
def _actor(request: Request) -> str:
    """Best-effort caller identity for audit records (mirrors identity.py)."""
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        return f"{principal.role}:{principal.sub}"
    return request.client.host if request.client else "anonymous"


def _require_roles(request: Request, allowed, action: str) -> str:
    """Enforce a role set INSIDE the router.

    The middleware already gates ``/api/sv`` as a whole; this narrows specific
    actions further (enrolment, uploads). When enforcement is off (the demo
    profile has no principal at all) this is a no-op, exactly like the other
    routers that do in-router role checks.
    """
    principal = getattr(request.state, "principal", None)
    if principal is None:
        return "anonymous"
    role = getattr(principal, "role", None)
    if role not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "forbidden", "action": action,
                    "detail": f"{action} requires one of: {', '.join(sorted(allowed))}"},
        )
    return getattr(principal, "sub", "operator")


def _fail(exc: SecureVisionError) -> HTTPException:
    """Map a typed client failure onto an HTTP answer with a machine-readable
    ``error`` code — the same ``{detail: {error, detail}}`` envelope the rest of
    the gateway uses, so web/src/lib/api.ts's apiError() reads it unchanged.

    Vendor stack traces never reach a user; the message is the typed reason only.
    """
    if isinstance(exc, SecureVisionNotConfigured):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "securevision_not_configured",
                    "detail": "SecureVision credentials are not configured on the gateway."})
    if isinstance(exc, SecureVisionTimeout):
        return HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"error": "securevision_timeout", "detail": str(exc)})
    if isinstance(exc, SecureVisionUnavailable):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "securevision_unavailable", "detail": str(exc)})
    if isinstance(exc, SecureVisionAuthError):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "securevision_auth_failed",
                    "detail": "The SecureVision service credential was rejected."})
    if isinstance(exc, SecureVisionForbidden):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "securevision_forbidden",
                    "detail": "The SecureVision service account lacks the required role."})
    if isinstance(exc, SecureVisionAnalysisExpired):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "analysis_expired",
                    "detail": "SecureVision no longer holds this analysis's frames. "
                              "Re-run the analysis to stream it again."})
    if isinstance(exc, SecureVisionConflict):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "person_already_enrolled", "detail": str(exc)})
    if isinstance(exc, SecureVisionNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "securevision_not_found", "detail": str(exc)})
    if isinstance(exc, SecureVisionUnprocessable):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "securevision_unprocessable", "detail": str(exc)})
    if isinstance(exc, SecureVisionModelUnavailable):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "face_model_unavailable", "detail": str(exc)})
    if isinstance(exc, SecureVisionInvalidResponse):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "securevision_invalid_response", "detail": str(exc)})
    if isinstance(exc, SecureVisionHTTPError):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "securevision_http_error",
                    "upstream_status": exc.status_code,
                    "detail": exc.reason or "SecureVision returned an error."})
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                         detail={"error": "securevision_error", "detail": str(exc)})


async def _read_upload(file: UploadFile, *, allowed_types, kind: str) -> bytes:
    """Read + validate one uploaded file.

    The declared content-type is caller-controlled, so it is checked AND the
    payload's magic bytes are sniffed. Size is bounded by the shared ceiling
    (gateway/upload_limits.py) before anything is relayed to the vendor.
    """
    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "empty_file"})
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"error": "file_too_large", "max_bytes": MAX_UPLOAD_BYTES,
                    "detail": f"The {kind} exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit."})
    declared = (file.content_type or "").lower().split(";")[0].strip()
    if declared and declared not in allowed_types:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                            detail={"error": "unsupported_media_type",
                                    "content_type": declared,
                                    "allowed": list(allowed_types)})
    if kind == "video" and not _looks_like_video(content):
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                            detail={"error": "not_a_video",
                                    "detail": "The uploaded file is not a recognised video container."})
    if kind == "photo" and not _looks_like_image(content):
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                            detail={"error": "not_an_image",
                                    "detail": "The uploaded file is not a recognised image."})
    return content


def _looks_like_video(blob: bytes) -> bool:
    """Magic-byte sniff for the containers YOLO ingestion accepts."""
    head = blob[:16]
    if len(blob) >= 12 and blob[4:8] == b"ftyp":       # MP4 / MOV / M4V
        return True
    if head.startswith(b"\x1aE\xdf\xa3"):               # Matroska / WebM
        return True
    if head.startswith(b"RIFF") and blob[8:12] == b"AVI ":
        return True
    if head.startswith(b"\x00\x00\x01\xba") or head.startswith(b"\x00\x00\x01\xb3"):
        return True                                     # MPEG program/sequence
    return False


def _looks_like_image(blob: bytes) -> bool:
    head = blob[:12]
    return (head.startswith(b"\xff\xd8\xff")            # JPEG
            or head.startswith(b"\x89PNG\r\n\x1a\n")    # PNG
            or (head.startswith(b"RIFF") and blob[8:12] == b"WEBP"))


# --------------------------------------------------------------------- health
@router.get("/health", summary="SecureVision integration posture")
async def sv_health() -> Dict[str, Any]:
    """Configuration + reachability. Never raises: a health endpoint that 500s
    when the thing it reports on is down tells the operator nothing."""
    client = get_client()
    payload: Dict[str, Any] = {
        "integration": "securevision",
        "configured": client.configured,
        "base_url": client.base_url,
        "camera_map_configured": bool(cameras.camera_map()),
        "camera_map_entries": len(cameras.camera_map()),
        # Recorded posture, surfaced so the UI never implies otherwise. Upload
        # METADATA is now durable (core.video_analysis); detection results and
        # any person/face payload remain unstored.
        "persistence": "ANALYSIS_METADATA",
        "analyses_in_session": len(analyses.recent(limit=analyses.MAX_ANALYSES)),
        "stream_tickets_outstanding": tickets.outstanding(),
        "mode": "UPLOAD_CLIP_ANALYTICS",
    }
    if not client.configured:
        payload.update(status="NOT_CONFIGURED", reachable=False)
        return payload
    try:
        user = await client.me()
        payload.update(status="LIVE", reachable=True,
                       service_account=user.username, service_role=user.role)
        REQUESTS.labels("securevision", "ok").inc()
    except SecureVisionError as exc:
        payload.update(status="UNAVAILABLE", reachable=False,
                       error=type(exc).__name__, detail=str(exc))
        REQUESTS.labels("securevision", "error").inc()
    try:
        model = await client.face_status()
        payload["face_model"] = {
            "model_ready": model.model_ready,
            "model_name": model.model_name,
            "provider": model.provider,
            "similarity_threshold": model.similarity_threshold,
            "gallery_loaded": model.gallery_loaded,
            "authorized_in_db": model.authorized_in_db,
        }
    except SecureVisionError:
        payload["face_model"] = None
    return payload


@router.get("/cameras", summary="SecureVision -> JNPA camera mapping")
async def sv_cameras() -> Dict[str, Any]:
    """The explicit mapping, so an operator can see what is configured and the
    UI can offer only cameras it can attribute correctly."""
    mapping = cameras.camera_map()
    return {
        "configured": bool(mapping),
        "count": len(mapping),
        "cameras": [{"securevision_code": sv, "jnpa_camera_id": jnpa}
                    for sv, jnpa in sorted(mapping.items())],
    }


# ------------------------------------------------------------------ analyses
@router.get("/analyses", summary="Video Analytics history (newest first, paginated)")
async def sv_analyses(request: Request,
                      limit: int = Query(default=50, ge=1, le=200),
                      offset: int = Query(default=0, ge=0),
                      camera_id: Optional[str] = Query(default=None,
                                                       description="filter to one JNPA camera"),
                      ) -> Dict[str, Any]:
    """The durable history of clips analysed through this deployment.

    Backed by ``core.video_analysis`` (migration 0143), so it survives gateway,
    container and worker restarts. The response keeps its original keys
    (``analyses``/``count``/``persisted``/``note``) and adds pagination
    (``total``/``limit``/``offset``) plus ``degraded``, which is true when the
    durable store could not be read and the process cache answered instead —
    an unreadable archive is never reported as an empty one.

    Detection RESULTS are not stored: they are fetched from SecureVision per
    analysis, and person/face payloads are persisted nowhere.
    """
    history = get_history(request)
    payload = await history.recent(limit=limit, offset=offset,
                                   jnpa_camera_id=camera_id)
    REQUESTS.labels("securevision", "ok").inc()
    return payload


@router.post("/analytics/video/upload", status_code=status.HTTP_201_CREATED,
             summary="Upload one clip for a single YOLOv11 detection pass")
async def sv_upload_video(
    request: Request,
    file: UploadFile = File(...),
    camera_code: Optional[str] = Form(default=None),
    jnpa_camera_id: Optional[str] = Form(default=None),
) -> Dict[str, Any]:
    """Relay a clip to SecureVision and remember the resulting ``analysis_id``.

    The camera may be given either as the vendor's own ``camera_code`` or as a
    JNPA camera id, which is resolved through the explicit mapping. An
    unresolvable JNPA id is refused rather than sent as-is: SecureVision would
    silently load zero zones and I-07 would answer nothing, which reads as "no
    intrusion" when it actually means "misconfigured".
    """
    _require_roles(request, _ANALYSIS_WRITE_ROLES, "SecureVision video upload")
    resolved = (camera_code or "").strip()
    if not resolved and jnpa_camera_id:
        resolved = cameras.to_securevision(jnpa_camera_id) or ""
        if not resolved:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "camera_mapping_unavailable",
                        "jnpa_camera_id": jnpa_camera_id,
                        "detail": "No SecureVision camera is mapped to this JNPA camera. "
                                  "Configure SECUREVISION_CAMERA_MAP."})
    if not resolved:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "camera_code_required"})

    content = await _read_upload(file, allowed_types=_VIDEO_CONTENT_TYPES,
                                 kind="video")
    client = get_client()
    # Wall-clock cost of the detection pass — an operational figure the history
    # keeps so a slow camera/clip is visible after the fact.
    _t0 = perf_counter()
    try:
        result = await client.upload_video(
            content, filename=file.filename,
            content_type=file.content_type, camera_code=resolved)
    except SecureVisionError as exc:
        REQUESTS.labels("securevision", "error").inc()
        raise _fail(exc) from exc

    if not result.analysis_id:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail={"error": "securevision_invalid_response",
                                    "detail": "Upload answered without an analysis_id."})
    entry = await get_history(request).record(
        result.analysis_id,
        securevision_camera_code=result.camera_code or resolved,
        jnpa_camera_id=cameras.to_jnpa(result.camera_code or resolved),
        filename=file.filename,
        frames_sampled=result.frames_sampled,
        detection_pass_count=result.detection_pass_count,
        zones_loaded=result.zones_loaded,
        uploaded_by=_actor(request),
        status="COMPLETED",
        processing_ms=int((perf_counter() - _t0) * 1000),
        source=normalize.SOURCE,
    )
    REQUESTS.labels("securevision", "ok").inc()
    log.info("securevision_upload_ok", analysis_id=result.analysis_id,
             camera_code=resolved, bytes=len(content),
             zones_loaded=result.zones_loaded)
    return {
        **entry,
        "camera": cameras.describe(result.camera_code or resolved),
        # zones_loaded == 0 means zone-based I-07 cannot fire. Surfaced as a
        # first-class warning so the operator is not left reading an empty
        # result as "nobody was there".
        "zone_warning": (result.zones_loaded or 0) == 0,
        "source": normalize.SOURCE,
    }


@router.delete("/analytics/video/{analysis_id}",
               status_code=status.HTTP_204_NO_CONTENT,
               summary="Delete one cached SecureVision analysis")
async def sv_delete_analysis(
    request: Request,
    analysis_id: str = PathParam(..., min_length=4, max_length=64),
) -> Response:
    _require_roles(request, _ANALYSIS_WRITE_ROLES, "SecureVision analysis delete")
    client = get_client()
    try:
        await client.delete_analysis(analysis_id)
    except SecureVisionNotFound:
        # Already gone upstream — converge our own view rather than erroring.
        pass
    except SecureVisionError as exc:
        raise _fail(exc) from exc
    # Soft-delete: the vendor-side analysis is gone, but the record that it
    # existed and was deleted stays in the history.
    await get_history(request).forget(analysis_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------- incidents
@router.get("/analytics/incident/{code}",
            summary="One SecureVision incident analyzer (i01|i02|i07|i09|i12|all)")
async def sv_incident(
    request: Request,
    code: str = PathParam(...),
    analysis_id: str = Query(..., min_length=4, max_length=64),
    strong: bool = Query(default=False,
                         description="Slower, more accurate description pass. "
                                     "Ignored for i07, which has no such option."),
) -> Dict[str, Any]:
    """Run/replay one analyzer against an existing analysis and normalise it.

    Repeat calls are free upstream (results are deduped/cached per analysis), so
    this is safe to call per tab-switch without a client-side cache of its own.
    """
    key = (code or "").strip().lower()
    if key not in _INCIDENT_CODES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "unknown_incident_code", "code": code,
                                    "known": list(_INCIDENT_CODES)})
    client = get_client()
    try:
        if key == "i07":
            raw = await client.incident_i07(analysis_id)
            result = normalize.normalize_i07(raw, analysis_id=analysis_id)
            # I-07 carries person names and face similarities. Record the access
            # against the same DPDP audit sink the driver-identity surface uses.
            _audit_person_access(request, result)
        elif key == "all":
            raw = await client.incident_all(analysis_id, strong=strong)
            result = normalize.normalize_combined(raw, analysis_id=analysis_id)
        else:
            raw = await client.incident(analysis_id, key, strong=strong)
            result = normalize.normalize_incident(raw, code=key,
                                                  analysis_id=analysis_id)
    except SecureVisionError as exc:
        REQUESTS.labels("securevision", "error").inc()
        raise _fail(exc) from exc
    REQUESTS.labels("securevision", "ok").inc()
    return result


def _audit_person_access(request: Request, result: Dict[str, Any]) -> None:
    """DPDP audit for each identified person in an I-07 answer.

    Only identified people are recorded — an UNVERIFIED or unnamed detection has
    no data subject to attribute the access to. Uses the existing audit sink
    (gateway/dpdp.py) so biometric-adjacent access stays traceable in one place.
    """
    actor = _actor(request)
    for person in result.get("persons", []) or []:
        person_id = person.get("person_id")
        if not person_id:
            continue
        try:
            # AUDIT_REVIEW is the allow-listed purpose for a control-room
            # review of a prior identity decision (gateway/dpdp.py) — which is
            # exactly what reading an I-07 verdict is.
            audit_identity_access(
                actor=actor,
                driver_id=str(person_id),
                purpose="AUDIT_REVIEW",
                is_synthetic=False,
                decision=str(person.get("person_status") or "UNVERIFIED"),
            )
        except Exception as exc:  # noqa: BLE001 — auditing must not break a read
            log.warning("securevision_dpdp_audit_failed", error=str(exc))


# -------------------------------------------------------------------- stream
@router.post("/analytics/video/{analysis_id}/stream-ticket",
             summary="Mint a short-lived ticket for the MJPEG replay")
async def sv_stream_ticket(
    request: Request,
    analysis_id: str = PathParam(..., min_length=4, max_length=64),
) -> Dict[str, Any]:
    """A browser ``<img>`` cannot send an Authorization header, and the replay is
    ``multipart/x-mixed-replace`` which only an ``<img>`` renders natively. This
    authenticated call mints an opaque, expiring credential scoped to ONE
    analysis, which the stream route below accepts in the query string — the
    same shape the WebSocket handshake already uses for the same reason."""
    _require_roles(request, _ANALYSIS_WRITE_ROLES, "SecureVision stream")
    issued = tickets.issue(analysis_id, actor=_actor(request))
    log.info("securevision_stream_ticket_issued", analysis_id=analysis_id)
    return {**issued, "stream_url":
            f"/api/sv/analytics/video/{analysis_id}/stream?ticket={issued['ticket']}"}


@router.get("/analytics/video/{analysis_id}/stream",
            summary="Annotated MJPEG replay (ticket-authenticated)")
async def sv_stream(
    analysis_id: str = PathParam(..., min_length=4, max_length=64),
    ticket: str = Query(..., min_length=16, max_length=128),
    fps: int = Query(default=5, ge=1, le=30),
    min_conf: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    loop: bool = Query(default=True),
) -> StreamingResponse:
    """Relay the vendor's annotated replay to the browser.

    This route is reachable without a bearer (see gateway/auth.py
    ``_PUBLIC_PATTERNS``) precisely because an ``<img>`` cannot send one — the
    ticket is the credential, and it is minted only by the authenticated,
    RBAC-checked call above.
    """
    record = tickets.redeem(ticket, analysis_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail={"error": "invalid_stream_ticket",
                                    "detail": "The stream ticket is unknown, expired, "
                                              "or issued for a different analysis."})
    client = get_client()
    # The context manager owns the upstream connection for the life of the
    # response, so it is entered here and exited when the generator finishes.
    try:
        ctx = client.stream_analysis(analysis_id, fps=fps, min_conf=min_conf,
                                     loop=loop)
        content_type, chunks = await ctx.__aenter__()
    except SecureVisionError as exc:
        log.info("securevision_stream_rejected", analysis_id=analysis_id,
                 error=type(exc).__name__)
        raise _fail(exc) from exc

    async def relay():
        try:
            async for chunk in chunks:
                yield chunk
        finally:
            await ctx.__aexit__(None, None, None)

    log.info("securevision_stream_open", analysis_id=analysis_id, fps=fps)
    return StreamingResponse(
        relay(),
        media_type=content_type,
        headers={
            # An MJPEG replay must never be cached or buffered on the way out.
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------- media
@router.get("/media/{object_path:path}",
            summary="SecureVision evidence/snapshot image proxy")
async def sv_media(object_path: str) -> Response:
    """Fetch one vendor media object with the service token attached.

    Authenticated like any other /api/sv route: these frames can contain
    identifiable people, so unlike /api/evidence this proxy is NOT public. The
    frontend loads them with an authenticated fetch and renders the blob
    (web/src/hooks/useAuthedImage.ts).
    """
    if not object_path or ".." in object_path or object_path.startswith("/"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "not_found"})
    client = get_client()
    try:
        resp = await client._request(  # noqa: SLF001 — media is a raw passthrough
            "GET", f"/media/{object_path}", expect_json=False)
    except SecureVisionError as exc:
        raise _fail(exc) from exc
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "private, max-age=300"},
    )


# --------------------------------------------------------------------- faces
@router.get("/faces/events", summary="SecureVision face-recognition event log")
async def sv_face_events(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    authorized: Optional[bool] = Query(default=None),
) -> Dict[str, Any]:
    client = get_client()
    try:
        rows = await client.face_events(limit=limit, authorized=authorized)
    except SecureVisionError as exc:
        raise _fail(exc) from exc
    events = [normalize.normalize_face_event(row) for row in rows]
    actor = _actor(request)
    for event in events:
        if event.get("person_id"):
            try:
                audit_identity_access(
                    actor=actor, driver_id=str(event["person_id"]),
                    purpose="AUDIT_REVIEW", is_synthetic=False,
                    decision=str(event.get("person_status") or "UNVERIFIED"))
            except Exception as exc:  # noqa: BLE001
                log.warning("securevision_dpdp_audit_failed", error=str(exc))
    return {"events": events, "count": len(events), "source": normalize.SOURCE}


@router.get("/faces/status", summary="SecureVision face model & gallery diagnostics")
async def sv_face_status() -> Dict[str, Any]:
    client = get_client()
    if not client.configured:
        return {"configured": False, "status": "NOT_CONFIGURED", "model_ready": False,
                "source": normalize.SOURCE}
    try:
        status_obj = await client.face_status()
    except SecureVisionError as exc:
        raise _fail(exc) from exc
    return {
        "configured": True,
        "status": "READY" if status_obj.model_ready else "NOT_READY",
        "model_ready": bool(status_obj.model_ready),
        "model_name": status_obj.model_name,
        "provider": status_obj.provider,
        "similarity_threshold": status_obj.similarity_threshold,
        "downscale": status_obj.downscale,
        "authorized_in_db": status_obj.authorized_in_db,
        "gallery_loaded": status_obj.gallery_loaded,
        # Enrolled people's names are personal data; the count is what an
        # operations dashboard needs, so the roster itself is not echoed here.
        "authorized_names_count": len(status_obj.authorized_names or []),
        "source": normalize.SOURCE,
    }


@router.get("/faces", summary="Enrolled site personnel")
async def sv_list_faces(request: Request) -> Dict[str, Any]:
    client = get_client()
    try:
        people = await client.list_faces()
    except SecureVisionError as exc:
        raise _fail(exc) from exc
    return {"persons": [normalize.normalize_face_person(p) for p in people],
            "count": len(people), "source": normalize.SOURCE}


@router.post("/faces", status_code=status.HTTP_201_CREATED,
             summary="Enrol one site person")
async def sv_enroll_face(
    request: Request,
    person_id: str = Form(..., min_length=1, max_length=64),
    name: str = Form(..., min_length=1, max_length=120),
    role: Optional[str] = Form(default=None),
    department: Optional[str] = Form(default=None),
    files: List[UploadFile] = File(...),
) -> Dict[str, Any]:
    """Enrol a person into the SecureVision gallery (site personnel only).

    This is a SEPARATE population from the driver identity gallery behind
    /api/identity — that system is untouched and remains authoritative for
    drivers. No dual-write happens here.

    The photo is relayed to the vendor and NOT stored by this gateway; there is
    no JNPA-side biometric copy to retain, which is the narrowest posture
    available while the DPDP retention decision is outstanding.
    """
    actor = _require_roles(request, _FACE_ADMIN_ROLES, "site-personnel enrolment")
    # Enrolling a photograph of a real person IS real biometric processing, and
    # this deployment's documented posture disables that unless it has been
    # explicitly consent-gated (ALLOW_REAL_BIOMETRICS). Routing SecureVision
    # around that guard would quietly weaken a control the rest of the platform
    # enforces, so it goes through the same gate: 403 with a clear reason until
    # the deployment opts in.
    enforce_dpdp(purpose="ENROLMENT", is_synthetic=False)
    photos = []
    for upload in files:
        blob = await _read_upload(upload, allowed_types=_IMAGE_CONTENT_TYPES,
                                  kind="photo")
        photos.append((upload.filename or "face.jpg", blob,
                       upload.content_type or "image/jpeg"))
    if not photos:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "photo_required"})
    client = get_client()
    try:
        person = await client.enroll_face(person_id=person_id, name=name,
                                          role=role, department=department,
                                          photos=photos)
    except SecureVisionError as exc:
        raise _fail(exc) from exc
    try:
        audit_identity_access(actor=_actor(request), driver_id=person_id,
                              purpose="ENROLMENT", is_synthetic=False,
                              decision="ENROLLED")
    except Exception as exc:  # noqa: BLE001
        log.warning("securevision_dpdp_audit_failed", error=str(exc))
    log.info("securevision_face_enrolled", person_id=person_id, actor=actor)
    return normalize.normalize_face_person(person)


@router.get("/faces/{person_pk}", summary="One enrolled site person")
async def sv_get_face(person_pk: int = PathParam(..., ge=1)) -> Dict[str, Any]:
    client = get_client()
    try:
        person = await client.get_face(person_pk)
    except SecureVisionError as exc:
        raise _fail(exc) from exc
    return normalize.normalize_face_person(person)


@router.patch("/faces/{person_pk}", summary="Update one enrolled site person")
async def sv_update_face(
    request: Request,
    person_pk: int = PathParam(..., ge=1),
    body: Dict[str, Any] = Body(default_factory=dict),
) -> Dict[str, Any]:
    _require_roles(request, _FACE_ADMIN_ROLES, "site-personnel update")
    allowed = {"name", "role", "department", "is_active"}
    patch = {k: v for k, v in (body or {}).items() if k in allowed}
    if not patch:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "no_updatable_fields",
                                    "allowed": sorted(allowed)})
    client = get_client()
    try:
        person = await client.update_face(person_pk, patch)
    except SecureVisionError as exc:
        raise _fail(exc) from exc
    return normalize.normalize_face_person(person)


@router.delete("/faces/{person_pk}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Remove one enrolled site person")
async def sv_delete_face(request: Request,
                         person_pk: int = PathParam(..., ge=1)) -> Response:
    _require_roles(request, _FACE_ADMIN_ROLES, "site-personnel removal")
    client = get_client()
    try:
        await client.delete_face(person_pk)
    except SecureVisionError as exc:
        raise _fail(exc) from exc
    try:
        audit_identity_access(actor=_actor(request), driver_id=str(person_pk),
                              purpose="ENROLMENT", is_synthetic=False,
                              decision="DELETED")
    except Exception as exc:  # noqa: BLE001
        log.warning("securevision_dpdp_audit_failed", error=str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/faces/{person_pk}/photo", summary="Enrolment photo (binary)")
async def sv_face_photo(person_pk: int = PathParam(..., ge=1)) -> Response:
    client = get_client()
    try:
        blob, content_type = await client.face_photo(person_pk)
    except SecureVisionError as exc:
        raise _fail(exc) from exc
    return Response(content=blob, media_type=content_type,
                    headers={"Cache-Control": "private, max-age=300"})


__all__ = ["router", "get_client", "reset_client"]
