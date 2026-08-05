"""ULIP HTTP client — the ONLY layer that talks to the ULIP gateway.

ULIP (Unified Logistics Interface Platform, DPIIT) fronts the national
logistics source systems (FASTag/NPCI toll crossings, LDB container tracking,
VAHAN, …) behind one API gateway. Access requires a registered account
(https://www.ulip.dpiit.gov.in/). Everything is env-driven — NO hardcoded
credential, NO vendor URL in business code (mirrors integrations.tomtom.client):

    ULIP_API_URL        base URL (default
                        https://www.ulip.dpiit.gov.in/ulip/v1.0.0)
    ULIP_CLIENT_ID      account username for POST /user/login (BACKEND-ONLY)
    ULIP_CLIENT_SECRET  account password for POST /user/login (BACKEND-ONLY)
    ULIP_API_KEY        alternative: a pre-issued static bearer token — used
                        as-is when set, skipping /user/login entirely
    ULIP_TIMEOUT_S      per-attempt budget            (default 5.0)
    ULIP_RETRIES        retries AFTER the first try   (default 2)
    ULIP_TOKEN_TTL_S    login-token reuse window      (default 1800)
    ULIP_FASTAG_API     API path for vehicle movement (default FASTAG/01)
    ULIP_LDB_API        API path for container tracking (default LDB/01)

Auth model: when only ULIP_CLIENT_ID/SECRET are set the client logs in
(``POST {base}/user/login``), caches the issued bearer token for
ULIP_TOKEN_TTL_S, and forces exactly ONE re-login when an API call answers
401/403 (an expired token); a second rejection surfaces as
:class:`~integrations.ulip.exceptions.UlipAuthError` — retrying rejected
credentials cannot help. A static ULIP_API_KEY takes precedence when present.

Failure contract: every failure surfaces as a typed
:class:`~integrations.ulip.exceptions.UlipError` subclass. Timeouts, network
errors and 5xx are retried with exponential backoff; 4xx fail fast. 200
bodies are validated through the tolerant envelope schema before anything
downstream sees them. No credential or token ever appears in logs or
exception messages (redacted everywhere).
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, Optional

import httpx
from pydantic import ValidationError

from jnpa_shared.logging import get_logger

from .exceptions import (
    UlipAuthError,
    UlipError,
    UlipHTTPError,
    UlipInvalidResponse,
    UlipNotConfigured,
    UlipTimeout,
    UlipUnavailable,
)
from .schemas import UlipEnvelope

log = get_logger("integrations.ulip.client")

DEFAULT_API_URL = "https://www.ulip.dpiit.gov.in/ulip/v1.0.0"
DEFAULT_FASTAG_API = "FASTAG/01"
DEFAULT_LDB_API = "LDB/01"
LOGIN_PATH = "user/login"


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


class UlipClient:
    """Async client for the ULIP gateway APIs (FASTAG / LDB).

    Stateless apart from configuration and the cached login token. An
    externally-owned ``httpx.AsyncClient`` may be injected (tests / the
    gateway's pooled client); otherwise a short-lived client is created per
    call, exactly like :class:`integrations.tomtom.TomTomClient`.
    """

    def __init__(
        self,
        api_url: Optional[str] = None,
        *,
        api_key: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        timeout_s: Optional[float] = None,
        retries: Optional[int] = None,
        backoff_s: float = 0.25,
        token_ttl_s: Optional[float] = None,
        fastag_api: Optional[str] = None,
        ldb_api: Optional[str] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        env = os.environ
        self.api_url = (api_url or env.get("ULIP_API_URL", "").strip()
                        or DEFAULT_API_URL).rstrip("/")
        self.api_key = (api_key if api_key is not None
                        else env.get("ULIP_API_KEY", "")).strip()
        self.client_id = (client_id if client_id is not None
                          else env.get("ULIP_CLIENT_ID", "")).strip()
        self.client_secret = (client_secret if client_secret is not None
                              else env.get("ULIP_CLIENT_SECRET", "")).strip()
        self.timeout_s = (timeout_s if timeout_s is not None
                          else _as_float(env.get("ULIP_TIMEOUT_S"), 5.0))
        self.retries = (retries if retries is not None
                        else max(0, _as_int(env.get("ULIP_RETRIES"), 2)))
        self.backoff_s = backoff_s
        self.token_ttl_s = (token_ttl_s if token_ttl_s is not None
                            else _as_float(env.get("ULIP_TOKEN_TTL_S"), 1800.0))
        self.fastag_api = (fastag_api or env.get("ULIP_FASTAG_API", "").strip()
                           or DEFAULT_FASTAG_API).strip("/")
        self.ldb_api = (ldb_api or env.get("ULIP_LDB_API", "").strip()
                        or DEFAULT_LDB_API).strip("/")
        self._http = http_client
        self._token: Optional[str] = None
        self._token_at: float = 0.0

    @property
    def configured(self) -> bool:
        """True when a credential is present — the provider participates at
        all. Either a static token OR a login credential pair qualifies."""
        return bool(self.api_key or (self.client_id and self.client_secret))

    @property
    def auth_mode(self) -> str:
        """``static`` (pre-issued ULIP_API_KEY) | ``login`` | ``none``."""
        if self.api_key:
            return "static"
        if self.client_id and self.client_secret:
            return "login"
        return "none"

    # ------------------------------------------------------------ public API
    async def fetch_vehicle_movement(self, vehicle_number: str) -> UlipEnvelope:
        """FASTag toll-crossing history for one vehicle registration number
        (the vehicle-movement signal for the corridor)."""
        return await self._call_api(self.fastag_api,
                                    {"vehiclenumber": vehicle_number.strip().upper()})

    async def fetch_container_tracking(self, container_number: str) -> UlipEnvelope:
        """LDB (Logistics Data Bank) tracking for one container number."""
        return await self._call_api(self.ldb_api,
                                    {"containerNumber": container_number.strip().upper()})

    # ------------------------------------------------------------- plumbing
    def _redact(self, text: str) -> str:
        """No credential or issued token may surface in logs or exceptions."""
        for secret in (self.api_key, self.client_secret, self._token):
            if secret:
                text = text.replace(secret, "***")
        return text

    async def _call_api(self, api_path: str, payload: Dict[str, Any]) -> UlipEnvelope:
        if not self.configured:
            raise UlipNotConfigured(
                "neither ULIP_API_KEY nor ULIP_CLIENT_ID/ULIP_CLIENT_SECRET is set")
        client = self._http or httpx.AsyncClient(timeout=self.timeout_s)
        owns = self._http is None
        url = f"{self.api_url}/{api_path.strip('/')}"
        try:
            body = await self._post_json(client, url, payload,
                                         allow_reauth=self.auth_mode == "login")
        finally:
            if owns:
                await client.aclose()
        try:
            envelope = UlipEnvelope.model_validate(body)
        except ValidationError as exc:  # pragma: no cover - tolerant model
            raise UlipInvalidResponse(
                f"ULIP response failed validation: {self._redact(str(exc))}") from exc
        if not envelope.ok:
            raise UlipInvalidResponse(
                "ULIP reported an API-level error: "
                f"code={envelope.code!r} message={self._redact(str(envelope.message))!r}")
        return envelope

    async def _post_json(self, client: httpx.AsyncClient, url: str,
                         payload: Dict[str, Any], *, allow_reauth: bool) -> Dict[str, Any]:
        """POST with bounded retries. Retries timeouts / network errors / 5xx;
        401/403 forces ONE re-login (login mode) then fails as UlipAuthError;
        other 4xx (429 rate limit included) fail fast."""
        last_exc: UlipError = UlipUnavailable(f"no attempt made against {url}")
        reauthed = False
        for attempt in range(self.retries + 1):
            if attempt:
                await asyncio.sleep(self.backoff_s * (2 ** (attempt - 1)))
            token = await self._ensure_token(client)
            try:
                resp = await client.post(
                    url, json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=self.timeout_s,
                )
            except httpx.TimeoutException as exc:
                last_exc = UlipTimeout(f"ULIP timed out after {self.timeout_s}s")
                log.warning("ulip_timeout", attempt=attempt,
                            error=self._redact(str(exc)))
                continue
            except httpx.HTTPError as exc:
                last_exc = UlipUnavailable(
                    f"ULIP unreachable: {self._redact(str(exc))}")
                log.warning("ulip_unreachable", attempt=attempt,
                            error=self._redact(str(exc)))
                continue

            if resp.status_code == 200:
                try:
                    body = resp.json()
                except ValueError as exc:
                    raise UlipInvalidResponse("ULIP returned non-JSON body") from exc
                if not isinstance(body, dict):
                    raise UlipInvalidResponse(
                        f"ULIP returned a {type(body).__name__}, expected an object")
                return body

            if resp.status_code in (401, 403):
                # Expired/refused token. In login mode, force exactly one
                # re-login and retry the SAME attempt budget; a second
                # rejection means the credentials themselves are refused.
                if allow_reauth and not reauthed:
                    log.info("ulip_token_refused_reauth", status=resp.status_code)
                    self._token, self._token_at = None, 0.0
                    reauthed = True
                    continue
                raise UlipAuthError(
                    f"ULIP rejected the credential (HTTP {resp.status_code})")

            reason = _error_reason(resp)
            if resp.status_code >= 500:
                last_exc = UlipHTTPError(resp.status_code,
                                         self._redact(reason) if reason else None)
                log.warning("ulip_5xx", attempt=attempt,
                            status=resp.status_code, reason=reason)
                continue
            # Remaining 4xx — bad request / rate limit; retrying cannot help.
            raise UlipHTTPError(resp.status_code,
                                self._redact(reason) if reason else None)
        raise last_exc

    async def _ensure_token(self, client: httpx.AsyncClient) -> str:
        """The bearer credential for the next call: the static key, or a
        cached-then-refreshed login token."""
        if self.auth_mode == "static":
            return self.api_key
        now = time.monotonic()
        if self._token and (now - self._token_at) < self.token_ttl_s:
            return self._token
        self._token = await self._login(client)
        self._token_at = now
        return self._token

    async def _login(self, client: httpx.AsyncClient) -> str:
        """POST /user/login -> bearer token. Auth failures are terminal
        (UlipAuthError); transport failures surface as timeout/unavailable so
        the outer retry loop (via the caller) still applies its budget."""
        url = f"{self.api_url}/{LOGIN_PATH}"
        try:
            resp = await client.post(
                url,
                json={"username": self.client_id, "password": self.client_secret},
                timeout=self.timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise UlipTimeout(f"ULIP login timed out after {self.timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise UlipUnavailable(
                f"ULIP login unreachable: {self._redact(str(exc))}") from exc
        if resp.status_code != 200:
            raise UlipAuthError(
                f"ULIP login rejected (HTTP {resp.status_code})")
        try:
            body = resp.json()
        except ValueError as exc:
            raise UlipInvalidResponse("ULIP login returned non-JSON body") from exc
        token = _extract_token(body)
        if not token:
            raise UlipAuthError("ULIP login answered without a token")
        log.info("ulip_login_ok")
        return token


def _extract_token(body: Any) -> Optional[str]:
    """The issued token: ``response.id`` in the documented login answer, with
    tolerant fallbacks for ``token`` / ``accessToken`` / a top-level ``id``."""
    if not isinstance(body, dict):
        return None
    response = body.get("response")
    candidates = []
    if isinstance(response, dict):
        candidates.extend((response.get("id"), response.get("token"),
                           response.get("accessToken")))
    candidates.extend((body.get("id"), body.get("token"), body.get("accessToken")))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _error_reason(resp: httpx.Response) -> Optional[str]:
    """ULIP error bodies carry ``message`` (envelope) or ``error`` (gateway)."""
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    for key in ("message", "error", "detail"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


__all__ = ["UlipClient", "DEFAULT_API_URL", "DEFAULT_FASTAG_API", "DEFAULT_LDB_API",
           "LOGIN_PATH"]
