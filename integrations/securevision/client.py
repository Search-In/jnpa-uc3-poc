"""SecureVision HTTP client — the ONLY layer that talks to the SecureVision AI
surveillance platform.

SecureVision fronts YOLOv11 video analytics (incident detection + an annotated
MJPEG replay stream) and a face-recognition gallery. Access requires an account
on the platform; there is no public self-registration.

Everything is env-driven — NO hardcoded credential, NO vendor URL in business
code (mirrors integrations.ulip.client):

    SECUREVISION_BASE_URL        base URL (default https://svapidev.phylon.in)
    SECUREVISION_USERNAME        service-account username   (BACKEND-ONLY)
    SECUREVISION_PASSWORD        service-account password   (BACKEND-ONLY)
    SECUREVISION_TIMEOUT_S       per-attempt budget for JSON calls  (default 15)
    SECUREVISION_UPLOAD_TIMEOUT_S  budget for video/photo uploads   (default 180)
    SECUREVISION_RETRIES         retries AFTER the first try        (default 2)
    SECUREVISION_TOKEN_TTL_S     login-token reuse window           (default 1800)

**Why this exists at all.** SecureVision authenticates at ``/api/auth/login`` and
``/api/auth/me`` — byte-identical paths to this application's OWN authentication
(gateway/routers/auth.py). The browser calls relative ``/api/*``, so a
browser-direct integration is not merely inadvisable here, it is impossible
without breaking sign-in. The service credential therefore lives in the gateway
process, is exchanged for a token here, and never reaches a browser.

Auth model: the client logs in once (``POST /api/auth/login``), caches the issued
bearer for SECUREVISION_TOKEN_TTL_S, and forces exactly ONE re-login when a call
answers 401 (an expired token); a second rejection surfaces as
:class:`~integrations.securevision.exceptions.SecureVisionAuthError` — retrying
rejected credentials cannot help.

Failure contract: every failure surfaces as a typed ``SecureVisionError``
subclass, including the three that carry product meaning (409 analysis expired,
422 unprocessable upload, 503 model not loaded). Timeouts, network errors and
5xx are retried with exponential backoff; other 4xx fail fast. No credential or
token ever appears in logs or exception messages (redacted everywhere).
"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Tuple

import httpx
from pydantic import ValidationError

from jnpa_shared.logging import get_logger

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
    SvCombinedReport,
    SvFaceEvent,
    SvFacePerson,
    SvFaceStatus,
    SvI07Response,
    SvIncident,
    SvLoginResponse,
    SvUploadResult,
    SvUser,
)

log = get_logger("integrations.securevision.client")

DEFAULT_BASE_URL = "https://svapidev.phylon.in"
LOGIN_PATH = "/api/auth/login"
ME_PATH = "/api/auth/me"

#: How a 409 should be read for a given call. The vendor uses one status code for
#: two unrelated situations, and the UI must react differently to each.
CONFLICT_ANALYSIS_EXPIRED = "analysis_expired"
CONFLICT_DUPLICATE = "duplicate"

#: Uploaded photo/clip filename fallback when the browser sends none.
_FALLBACK_FILENAME = "upload.bin"


def _as_float(value: Optional[str], default: float) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _as_int(value: Optional[str], default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _error_reason(resp: httpx.Response) -> Optional[str]:
    """FastAPI-style vendor error bodies carry ``detail`` (str or object)."""
    try:
        body = resp.json()
    except ValueError:
        return None
    if isinstance(body, str):
        return body
    if not isinstance(body, dict):
        return None
    for key in ("detail", "message", "error"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            for inner in ("detail", "message", "error"):
                nested = value.get(inner)
                if isinstance(nested, str) and nested.strip():
                    return nested
    return None


class SecureVisionClient:
    """Async client for the SecureVision platform.

    Stateless apart from configuration and the cached login token. An
    externally-owned ``httpx.AsyncClient`` may be injected (tests / a pooled
    gateway client); otherwise a short-lived client is created per call, exactly
    like :class:`integrations.ulip.UlipClient`.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout_s: Optional[float] = None,
        upload_timeout_s: Optional[float] = None,
        retries: Optional[int] = None,
        backoff_s: float = 0.25,
        token_ttl_s: Optional[float] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        env = os.environ
        self.base_url = (base_url or env.get("SECUREVISION_BASE_URL", "").strip()
                         or DEFAULT_BASE_URL).rstrip("/")
        self.username = (username if username is not None
                         else env.get("SECUREVISION_USERNAME", "")).strip()
        self.password = (password if password is not None
                         else env.get("SECUREVISION_PASSWORD", "")).strip()
        self.timeout_s = (timeout_s if timeout_s is not None
                          else _as_float(env.get("SECUREVISION_TIMEOUT_S"), 15.0))
        self.upload_timeout_s = (
            upload_timeout_s if upload_timeout_s is not None
            else _as_float(env.get("SECUREVISION_UPLOAD_TIMEOUT_S"), 180.0))
        self.retries = (retries if retries is not None
                        else max(0, _as_int(env.get("SECUREVISION_RETRIES"), 2)))
        self.backoff_s = backoff_s
        self.token_ttl_s = (token_ttl_s if token_ttl_s is not None
                            else _as_float(env.get("SECUREVISION_TOKEN_TTL_S"), 1800.0))
        self._http = http_client
        self._token: Optional[str] = None
        self._token_at: float = 0.0
        self._login_lock = asyncio.Lock()

    # ------------------------------------------------------------- posture
    @property
    def configured(self) -> bool:
        """True when a service credential is present. Without one the whole
        integration is *disabled* (every surface answers a clean "not
        configured"), never *broken*."""
        return bool(self.username and self.password)

    def redact(self, text: str) -> str:
        """No credential or issued token may surface in logs or exceptions."""
        for secret in (self.password, self._token):
            if secret:
                text = text.replace(secret, "***")
        return text

    # -------------------------------------------------------- auth / me
    async def me(self) -> SvUser:
        """The service account SecureVision believes it is talking to. Used by
        the health surface to prove the credential works end to end."""
        body = await self._json("GET", ME_PATH)
        return self._model(SvUser, body, "auth/me")

    # ------------------------------------------------------- video analytics
    async def upload_video(
        self,
        content: bytes,
        *,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        camera_code: str,
    ) -> SvUploadResult:
        """Upload one clip for a single decode + YOLOv11 detection pass.

        ``camera_code`` must match a camera SecureVision knows, or zone-based
        I-07 loads no zones at all (``zones_loaded: 0``) — the caller is
        expected to have resolved it through the camera-mapping layer first.
        """
        files = [("file", (filename or _FALLBACK_FILENAME, content,
                           content_type or "application/octet-stream"))]
        body = await self._json(
            "POST", "/api/analytics/video/upload",
            files=files, data={"camera_code": camera_code},
            timeout_s=self.upload_timeout_s,
        )
        return self._model(SvUploadResult, body, "analytics/upload")

    async def incident(self, analysis_id: str, code: str, *,
                       strong: bool = False) -> SvIncident:
        """One single-envelope incident (I-01 / I-02 / I-09 / I-12)."""
        body = await self._incident_body(analysis_id, code, strong=strong)
        incident = self._model(SvIncident, body, f"incident/{code}")
        # A not-fired envelope omits most fields; carry the request context so
        # downstream code never has to remember what it asked for.
        if not incident.analysis_id:
            incident.analysis_id = analysis_id
        return incident

    async def incident_i07(self, analysis_id: str) -> SvI07Response:
        """I-07 — one verdict per detected person, not one per clip. No
        ``strong`` parameter exists for this endpoint."""
        body = await self._incident_body(analysis_id, "i07", strong=None)
        result = self._model(SvI07Response, body, "incident/i07")
        if not result.analysis_id:
            result.analysis_id = analysis_id
        return result

    async def incident_all(self, analysis_id: str, *,
                           strong: bool = False) -> SvCombinedReport:
        """Combined report — every enabled analyzer plus an AI narrative."""
        body = await self._incident_body(analysis_id, "all", strong=strong)
        report = self._model(SvCombinedReport, body, "incident/all")
        if not report.analysis_id:
            report.analysis_id = analysis_id
        return report

    async def delete_analysis(self, analysis_id: str) -> None:
        """Drop ONE cached analysis. The bulk ``DELETE /api/analytics/video``
        is deliberately not implemented: it wipes every analysis irreversibly
        with no server-side confirmation, so it has no place behind an API this
        application exposes."""
        await self._request("DELETE", f"/api/analytics/video/{analysis_id}",
                            expect_json=False)

    @asynccontextmanager
    async def stream_analysis(
        self,
        analysis_id: str,
        *,
        fps: int = 5,
        min_conf: Optional[float] = None,
        loop: bool = True,
    ) -> AsyncIterator[Tuple[str, AsyncIterator[bytes]]]:
        """Open the annotated MJPEG replay and yield ``(content_type, chunks)``.

        The vendor re-draws cached detections; no new inference runs. The
        response never ends while ``loop=True``, so this deliberately opts out
        of the read timeout that every other call uses — the connect timeout
        still applies, so an unreachable vendor still fails fast.

        Raises :class:`SecureVisionAnalysisExpired` (409) when the sampled
        frames have been evicted — the caller must offer "re-run analysis"
        rather than render a broken image.
        """
        if not self.configured:
            raise SecureVisionNotConfigured(
                "SECUREVISION_USERNAME / SECUREVISION_PASSWORD are not set")
        params: Dict[str, Any] = {"fps": fps, "loop": str(bool(loop)).lower()}
        if min_conf is not None:
            params["min_conf"] = min_conf
        url = f"{self.base_url}/api/analytics/video/{analysis_id}/stream"
        # Connect fast, read forever: an MJPEG replay is an intentionally
        # unbounded response. An injected client (tests / a pooled caller) is
        # reused as-is and never closed here — we only own what we create.
        timeout = httpx.Timeout(connect=self.timeout_s, read=None,
                                write=self.timeout_s, pool=self.timeout_s)
        client = self._http or httpx.AsyncClient(timeout=timeout)
        owns = self._http is None
        try:
            token = await self._ensure_token(client)
            for attempt in (0, 1):
                request = client.build_request(
                    "GET", url, params=params,
                    headers={"Authorization": f"Bearer {token}"})
                try:
                    response = await client.send(request, stream=True)
                except httpx.TimeoutException as exc:
                    raise SecureVisionTimeout(
                        f"SecureVision stream timed out after {self.timeout_s}s") from exc
                except httpx.HTTPError as exc:
                    raise SecureVisionUnavailable(
                        f"SecureVision stream unreachable: {self.redact(str(exc))}") from exc
                if response.status_code == 401 and attempt == 0:
                    await response.aclose()
                    log.info("securevision_token_refused_reauth", path="stream")
                    self._token, self._token_at = None, 0.0
                    token = await self._ensure_token(client)
                    continue
                if response.status_code >= 400:
                    await response.aread()
                    reason = _error_reason(response)
                    await response.aclose()
                    self._raise_status(response.status_code, reason,
                                       conflict_as=CONFLICT_ANALYSIS_EXPIRED)
                try:
                    yield (response.headers.get("content-type",
                                                "multipart/x-mixed-replace"),
                           response.aiter_bytes())
                finally:
                    await response.aclose()
                return
        finally:
            if owns:
                await client.aclose()

    # ----------------------------------------------------------------- faces
    async def list_faces(self) -> List[SvFacePerson]:
        body = await self._json("GET", "/api/faces")
        return [self._model(SvFacePerson, row, "faces")
                for row in (body if isinstance(body, list) else [])]

    async def get_face(self, person_pk: int) -> SvFacePerson:
        body = await self._json("GET", f"/api/faces/{person_pk}")
        return self._model(SvFacePerson, body, "faces/{pk}")

    async def enroll_face(
        self,
        *,
        person_id: str,
        name: str,
        role: Optional[str] = None,
        department: Optional[str] = None,
        photos: Sequence[Tuple[str, bytes, str]],
    ) -> SvFacePerson:
        """Enrol one person. ``photos`` is a sequence of
        ``(filename, bytes, content_type)`` — repeating the ``file`` field is
        how the vendor builds a more robust averaged embedding."""
        data: Dict[str, str] = {"person_id": person_id, "name": name}
        if role:
            data["role"] = role
        if department:
            data["department"] = department
        files = [("file", (fn, blob, ct)) for fn, blob, ct in photos]
        body = await self._json("POST", "/api/faces", files=files, data=data,
                                timeout_s=self.upload_timeout_s,
                                conflict_as=CONFLICT_DUPLICATE)
        return self._model(SvFacePerson, body, "faces")

    async def update_face(self, person_pk: int,
                          patch: Dict[str, Any]) -> SvFacePerson:
        body = await self._json("PATCH", f"/api/faces/{person_pk}", json=patch,
                                conflict_as=CONFLICT_DUPLICATE)
        return self._model(SvFacePerson, body, "faces/{pk}")

    async def delete_face(self, person_pk: int) -> None:
        await self._request("DELETE", f"/api/faces/{person_pk}", expect_json=False)

    async def face_photo(self, person_pk: int) -> Tuple[bytes, str]:
        """The enrolment photo as ``(bytes, content_type)``. Binary, not JSON."""
        resp = await self._request("GET", f"/api/faces/{person_pk}/photo",
                                   expect_json=False)
        return resp.content, resp.headers.get("content-type", "image/jpeg")

    async def face_events(self, *, limit: int = 100,
                          authorized: Optional[bool] = None) -> List[SvFaceEvent]:
        params: Dict[str, Any] = {"limit": limit}
        if authorized is not None:
            params["authorized"] = str(bool(authorized)).lower()
        body = await self._json("GET", "/api/faces/events", params=params)
        return [self._model(SvFaceEvent, row, "faces/events")
                for row in (body if isinstance(body, list) else [])]

    async def face_status(self) -> SvFaceStatus:
        body = await self._json("GET", "/api/faces/status")
        return self._model(SvFaceStatus, body, "faces/status")

    # -------------------------------------------------------------- plumbing
    def _model(self, model, body: Any, what: str):
        try:
            return model.model_validate(body)
        except ValidationError as exc:  # pragma: no cover - tolerant models
            raise SecureVisionInvalidResponse(
                f"SecureVision {what} response failed validation: "
                f"{self.redact(str(exc))}") from exc

    async def _incident_body(self, analysis_id: str, code: str, *,
                             strong: Optional[bool]) -> Any:
        path_code = INCIDENT_PATHS.get(code.lower())
        if not path_code:
            # Never forward an unknown code as a path segment.
            raise SecureVisionNotFound(f"unknown incident code {code!r}")
        params: Dict[str, Any] = {"analysis_id": analysis_id}
        if strong is not None:
            params["strong"] = str(bool(strong)).lower()
        return await self._json("GET", f"/api/analytics/incident/{path_code}",
                                params=params)

    async def _json(self, method: str, path: str, **kwargs) -> Any:
        resp = await self._request(method, path, **kwargs)
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise SecureVisionInvalidResponse(
                f"SecureVision returned a non-JSON body for {path}") from exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Sequence[Any]] = None,
        timeout_s: Optional[float] = None,
        expect_json: bool = True,
        conflict_as: str = CONFLICT_ANALYSIS_EXPIRED,
    ) -> httpx.Response:
        """Issue one authenticated request with bounded retries.

        Retries timeouts / network errors / 5xx; 401 forces ONE re-login and
        then fails as an auth error; every other 4xx fails fast because
        retrying cannot change the answer.
        """
        if not self.configured:
            raise SecureVisionNotConfigured(
                "SECUREVISION_USERNAME / SECUREVISION_PASSWORD are not set")
        budget = timeout_s or self.timeout_s
        client = self._http or httpx.AsyncClient(timeout=budget)
        owns = self._http is None
        url = f"{self.base_url}{path}"
        last_exc: SecureVisionError = SecureVisionUnavailable(
            f"no attempt made against {path}")
        reauthed = False
        try:
            for attempt in range(self.retries + 1):
                if attempt:
                    await asyncio.sleep(self.backoff_s * (2 ** (attempt - 1)))
                token = await self._ensure_token(client)
                try:
                    resp = await client.request(
                        method, url, params=params, json=json, data=data,
                        files=files, timeout=budget,
                        headers={"Authorization": f"Bearer {token}"},
                    )
                except httpx.TimeoutException as exc:
                    last_exc = SecureVisionTimeout(
                        f"SecureVision timed out after {budget}s")
                    log.warning("securevision_timeout", path=path, attempt=attempt,
                                error=self.redact(str(exc)))
                    continue
                except httpx.HTTPError as exc:
                    last_exc = SecureVisionUnavailable(
                        f"SecureVision unreachable: {self.redact(str(exc))}")
                    log.warning("securevision_unreachable", path=path,
                                attempt=attempt, error=self.redact(str(exc)))
                    continue

                if resp.status_code < 300:
                    return resp

                if resp.status_code == 401 and not reauthed:
                    log.info("securevision_token_refused_reauth", path=path)
                    self._token, self._token_at = None, 0.0
                    reauthed = True
                    continue

                reason = _error_reason(resp)
                # 503 is documented as "the face model is not loaded", i.e. a
                # statement about the vendor's state rather than a transient
                # blip. Retrying it three times only delays the same answer.
                if resp.status_code == 503:
                    self._raise_status(503, reason, conflict_as=conflict_as)
                if resp.status_code >= 500:
                    last_exc = SecureVisionHTTPError(
                        resp.status_code,
                        self.redact(reason) if reason else None)
                    log.warning("securevision_5xx", path=path, attempt=attempt,
                                status=resp.status_code)
                    continue
                self._raise_status(resp.status_code, reason,
                                   conflict_as=conflict_as)
            raise last_exc
        finally:
            if owns:
                await client.aclose()

    def _raise_status(self, status: int, reason: Optional[str], *,
                      conflict_as: str) -> None:
        """Map one vendor status onto the typed vocabulary. Never returns."""
        safe = self.redact(reason) if reason else None
        if status == 401:
            raise SecureVisionAuthError(
                "SecureVision rejected the service credential (HTTP 401)")
        if status == 403:
            raise SecureVisionForbidden(safe or "SecureVision refused (HTTP 403)")
        if status == 404:
            raise SecureVisionNotFound(safe or "SecureVision resource not found")
        if status == 409:
            if conflict_as == CONFLICT_DUPLICATE:
                raise SecureVisionConflict(safe)
            raise SecureVisionAnalysisExpired(
                safe or "the analysis is no longer cached upstream")
        if status == 422:
            raise SecureVisionUnprocessable(safe)
        if status == 503:
            raise SecureVisionModelUnavailable(
                safe or "the SecureVision model is not loaded")
        raise SecureVisionHTTPError(status, safe)

    async def _ensure_token(self, client: httpx.AsyncClient) -> str:
        """The cached bearer, refreshed when stale. Serialised so a burst of
        concurrent calls after an expiry produces ONE login, not N."""
        now = time.monotonic()
        if self._token and (now - self._token_at) < self.token_ttl_s:
            return self._token
        async with self._login_lock:
            now = time.monotonic()
            if self._token and (now - self._token_at) < self.token_ttl_s:
                return self._token
            self._token = await self._login(client)
            self._token_at = now
            return self._token

    async def _login(self, client: httpx.AsyncClient) -> str:
        """POST /api/auth/login -> bearer token. Auth failures are terminal;
        transport failures surface as timeout/unavailable so the caller's retry
        budget still applies."""
        url = f"{self.base_url}{LOGIN_PATH}"
        try:
            resp = await client.post(
                url,
                json={"username": self.username, "password": self.password},
                timeout=self.timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise SecureVisionTimeout(
                f"SecureVision login timed out after {self.timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise SecureVisionUnavailable(
                f"SecureVision login unreachable: {self.redact(str(exc))}") from exc
        if resp.status_code == 403:
            raise SecureVisionAuthError(
                "the SecureVision service account is disabled (HTTP 403)")
        if resp.status_code != 200:
            raise SecureVisionAuthError(
                f"SecureVision login rejected (HTTP {resp.status_code})")
        try:
            body = resp.json()
        except ValueError as exc:
            raise SecureVisionInvalidResponse(
                "SecureVision login returned a non-JSON body") from exc
        parsed = self._model(SvLoginResponse, body, "auth/login")
        token = (parsed.access_token or "").strip()
        if not token:
            raise SecureVisionAuthError("SecureVision login answered without a token")
        # Username is safe to log (it identifies the service account); the
        # password and the issued token never are.
        log.info("securevision_login_ok", username=self.username,
                 role=(parsed.user.role if parsed.user else None))
        return token


__all__ = ["SecureVisionClient", "DEFAULT_BASE_URL", "LOGIN_PATH", "ME_PATH",
           "CONFLICT_ANALYSIS_EXPIRED", "CONFLICT_DUPLICATE"]
