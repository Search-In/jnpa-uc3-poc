"""JNPA Simulated Port-Data API client — the ONLY layer that talks to
``dt.jnpa.in/poc-api-data-access``.

JNPA's PoC data API (Reference v2.0, 31-Jul-2026) serves the 13 sample-pack
data groups behind a client-key -> 1-hour-bearer auth flow. Everything is
env-driven — NO hardcoded credential, NO vendor URL in business code (mirrors
integrations.ulip.client):

    JNPA_PORTDATA_API_URL      base URL (default
                               https://dt.jnpa.in/poc-api-data-access).
                               A trailing /v2 is stripped: the reference PDF
                               defines the base WITH /v2 while every other
                               source defines it WITHOUT (spec defect D1) —
                               this client always adds /v2 itself.
    JNPA_PORTDATA_CLIENT_KEY   the issued client key (BACKEND-ONLY)
    JNPA_PORTDATA_TIMEOUT_S    per-attempt budget            (default 15.0)
    JNPA_PORTDATA_RETRIES      retries AFTER the first try   (default 2)
    JNPA_PORTDATA_RATE_BUDGET  client-side req/min ceiling   (default 100)
    JNPA_PORTDATA_PROXY        optional proxy URL for ALL API traffic, e.g.
                               socks5://65.2.212.121:1080 — used while the
                               allowlisted egress IP is only reachable via
                               the jnpa3 tunnel (unset ⇒ direct)

Auth model: the client key is exchanged at POST /v2/auth/token for a bearer
valid exactly 3600 s. The token is cached in-process, refreshed PROACTIVELY
inside the last 5 minutes of life and REACTIVELY exactly once when a data
call answers 401; a second 401 surfaces as JnpaAuthError. Token acquisition
is single-flight (one lock) so concurrent callers cannot login-storm — and
the API deliberately slows the bad-key path (~250 ms), so hammering it is
doubly wrong.

Strict-adherence + defect defenses baked in (each has a dedicated test):
  * timestamps go through httpx query encoding, so ``+05:30`` is sent as
    ``%2B05:30`` — JNPA's own Postman collection and HTML examples get this
    wrong (defect D22: a raw ``+`` decodes to a space server-side);
  * ``nextCursor`` is treated as an opaque string and passed back VERBATIM
    (it shares a namespace with fileRef — defect D13);
  * report groups return a 5-field envelope with no pagination trio (defect
    D9) — all such fields are Optional and never assumed;
  * file downloads verify sha256(content) against the record checksum / the
    quoted ETag; a mismatch raises and logs (defect defense);
  * ``RateLimit-Remaining`` is parsed when present but its ABSENCE is normal
    (it is omitted on errors and 304s — defect D5); a client-side sliding
    window enforces JNPA_PORTDATA_RATE_BUDGET regardless;
  * 429 carries no Retry-After (defect D6): the retry waits a blind
    60 s + jitter window;
  * every deviation the client can detect is captured as a
    DefectObservation — drained by the sync layer into core.api_defect_log,
    because JNPA's 31-Jul notice REQUIRES observed defects to be reported.

Failure contract: every failure surfaces as a typed JnpaError subclass.
Timeouts, network errors and 5xx are retried with exponential backoff; 4xx
fail fast (except the one reactive re-auth on 401 and the blind 429 wait).
No credential or token ever appears in logs or exception messages.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import random
import re
import time
from collections import deque
from datetime import date, datetime
from typing import Any, AsyncIterator, Deque, Dict, List, Optional, Union

import httpx
from pydantic import ValidationError

from jnpa_shared.logging import get_logger

from .exceptions import (
    JnpaAuthError,
    JnpaChecksumMismatch,
    JnpaError,
    JnpaHTTPError,
    JnpaInvalidResponse,
    JnpaNotConfigured,
    JnpaRateLimited,
    JnpaTimeout,
    JnpaUnavailable,
)
from .schemas import (
    ApiErrorBody,
    DefectObservation,
    EXPECTED_GROUP_SLUGS,
    FileFetch,
    GroupsEnvelope,
    IndexedRecord,
    RecordsEnvelope,
    ReportEnvelope,
    RequestStats,
    TokenInfo,
    TokenResponse,
)

log = get_logger("integrations.jnpa_portdata.client")

DEFAULT_API_URL = "https://dt.jnpa.in/poc-api-data-access"
TOKEN_PATH = "v2/auth/token"

# Bearer lifetime is a fixed 3600 s; refresh proactively inside the last 5 min.
DEFAULT_TOKEN_REFRESH_MARGIN_S = 300.0
# 429 has no Retry-After / RateLimit-Reset (defect D6) — blind window + jitter.
RATE_LIMITED_WAIT_S = 60.0
RATE_LIMITED_JITTER_S = 15.0

TimestampLike = Union[str, date, datetime, None]


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


def _ts_param(value: TimestampLike) -> Optional[str]:
    """Timestamp params accept RFC3339 strings, dates or datetimes. Strings
    pass through untouched; httpx percent-encodes the ``+05:30`` offset at
    the wire (the D22 defense — never pre-encode, never send raw)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip() or None


def _filename_from_disposition(value: Optional[str]) -> Optional[str]:
    """Extract the filename from a Content-Disposition header.

    The captured API sends ``attachment; filename="..."``; the RFC 5987
    ``filename*=`` form and the unquoted form are handled defensively. The
    filename matters: several corpus parsers derive terminal / layout /
    variant from it (the API's fileRef is deliberately opaque)."""
    if not value:
        return None
    # RFC 5987 extended form takes precedence when both are present.
    match = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)?''([^;]+)", value)
    if match:
        from urllib.parse import unquote

        candidate = unquote(match.group(1).strip())
        if candidate:
            return candidate
    match = re.search(r'filename\s*=\s*"([^"]*)"', value)
    if match:
        return match.group(1) or None
    match = re.search(r"filename\s*=\s*([^;]+)", value)
    if match:
        candidate = match.group(1).strip().strip('"')
        return candidate or None
    return None


class _RateBudget:
    """Client-side sliding-window request budget (requests per 60 s).

    The documented per-org quota is 120/min while captures imply 600/min
    (defect D4); staying under the DOCUMENTED number is the conservative,
    notice-compliant posture (NOTICE §6.4: sustained excessive load may
    suspend access)."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, per_minute)
        self._window: Deque[float] = deque()

    async def acquire(self) -> None:
        while True:
            now = time.monotonic()
            while self._window and now - self._window[0] >= 60.0:
                self._window.popleft()
            if len(self._window) < self.per_minute:
                self._window.append(now)
                return
            await asyncio.sleep(max(0.05, 60.0 - (now - self._window[0])))


class JnpaPortDataClient:
    """Async client for the JNPA Simulated Port-Data API v2.

    Stateless apart from configuration, the cached bearer and the
    defect-observation buffer. An externally-owned ``httpx.AsyncClient`` may
    be injected (tests use ``httpx.ASGITransport`` against the local sim);
    otherwise a short-lived client is created per public call.
    """

    def __init__(
        self,
        api_url: Optional[str] = None,
        *,
        client_key: Optional[str] = None,
        proxy: Optional[str] = None,
        timeout_s: Optional[float] = None,
        retries: Optional[int] = None,
        backoff_s: float = 0.5,
        rate_budget_per_min: Optional[int] = None,
        token_refresh_margin_s: float = DEFAULT_TOKEN_REFRESH_MARGIN_S,
        rate_limited_wait_s: float = RATE_LIMITED_WAIT_S,
        rate_limited_jitter_s: float = RATE_LIMITED_JITTER_S,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        env = os.environ
        raw_url = (api_url or env.get("JNPA_PORTDATA_API_URL", "").strip()
                   or DEFAULT_API_URL).rstrip("/")
        # Defect D1 defense: PDF §1.1 defines the base WITH /v2, everything
        # else without; concatenating the PDF's base with the PDF's own /v2
        # paths yields /v2/v2. Normalise here, once.
        if raw_url.endswith("/v2"):
            raw_url = raw_url[: -len("/v2")].rstrip("/")
            log.warning("jnpa_base_url_v2_stripped", url=raw_url)
            self._observe("D1_BASE_URL_V2", "config",
                          "configured base URL ended in /v2; stripped "
                          "(client adds /v2 itself — spec defect D1)")
        self.api_url = raw_url
        self.client_key = (client_key if client_key is not None
                           else env.get("JNPA_PORTDATA_CLIENT_KEY", "")).strip()
        self.proxy = (proxy if proxy is not None
                      else env.get("JNPA_PORTDATA_PROXY", "")).strip() or None
        self.timeout_s = (timeout_s if timeout_s is not None
                          else _as_float(env.get("JNPA_PORTDATA_TIMEOUT_S"), 15.0))
        self.retries = (retries if retries is not None
                        else max(0, _as_int(env.get("JNPA_PORTDATA_RETRIES"), 2)))
        self.backoff_s = backoff_s
        self.token_refresh_margin_s = token_refresh_margin_s
        self.rate_limited_wait_s = rate_limited_wait_s
        self.rate_limited_jitter_s = rate_limited_jitter_s
        self._budget = _RateBudget(
            rate_budget_per_min if rate_budget_per_min is not None
            else _as_int(env.get("JNPA_PORTDATA_RATE_BUDGET"), 100))
        self._http = http_client
        self._token: Optional[TokenInfo] = None
        self._token_lock = asyncio.Lock()
        self._stats = RequestStats()
        # NOTE: _observe may run before __init__ finishes (URL normalisation
        # above), so the buffer is created lazily in _observe.

    # ------------------------------------------------------------ properties
    @property
    def configured(self) -> bool:
        """True when a client key is present — the provider participates."""
        return bool(self.client_key)

    @property
    def token_info(self) -> Optional[TokenInfo]:
        return self._token

    # ----------------------------------------------------- observations/stats
    def _observe(self, code: str, endpoint: str, detail: str,
                 severity: str = "INFO") -> None:
        buf = getattr(self, "_observations", None)
        if buf is None:
            buf = []
            self._observations = buf
        buf.append(DefectObservation(code=code, endpoint=endpoint,
                                     detail=detail, severity=severity))
        log.info("jnpa_defect_observed", code=code, endpoint=endpoint,
                 detail=detail)

    def drain_observations(self) -> List[DefectObservation]:
        """Return and clear the buffered defect observations (the sync layer
        persists them into core.api_defect_log every run)."""
        buf = getattr(self, "_observations", None) or []
        self._observations = []
        return list(buf)

    def request_stats(self, *, reset: bool = False) -> RequestStats:
        stats = self._stats
        if reset:
            self._stats = RequestStats()
        return stats

    # ------------------------------------------------------------ public API
    async def service_description(self) -> Dict[str, Any]:
        """GET / — unauthenticated service description."""
        resp = await self._request("GET", "", auth=False)
        return self._json_object(resp, "GET /")

    async def health(self) -> Dict[str, Any]:
        """GET /v2/health — unauthenticated liveness check."""
        resp = await self._request("GET", "v2/health", auth=False)
        return self._json_object(resp, "GET /v2/health")

    async def get_token(self) -> TokenInfo:
        """The cached bearer, refreshed proactively when inside the refresh
        margin. Single-flight: concurrent callers share one exchange."""
        if not self.configured:
            raise JnpaNotConfigured("JNPA_PORTDATA_CLIENT_KEY is not set")
        async with self._token_lock:
            if self._token and not self._token.expiring(self.token_refresh_margin_s):
                return self._token
            self._token = await self._exchange_key()
            return self._token

    async def list_groups(self) -> GroupsEnvelope:
        """GET /v2/groups — the data-group catalogue. Drift against the 13
        published slugs is recorded as an observation, never an error."""
        resp = await self._request("GET", "v2/groups")
        body = self._json_object(resp, "GET /v2/groups")
        try:
            envelope = GroupsEnvelope.model_validate(body)
        except ValidationError as exc:
            raise JnpaInvalidResponse(
                f"/v2/groups failed validation: {self._redact(str(exc))}") from exc
        slugs = {g.group for g in envelope.groups}
        expected = set(EXPECTED_GROUP_SLUGS)
        if slugs and slugs != expected:
            self._observe(
                "RUNTIME_GROUP_CATALOGUE_DRIFT", "GET /v2/groups",
                f"missing={sorted(expected - slugs)} extra={sorted(slugs - expected)}",
                severity="WARN")
        return envelope

    async def records_page(
        self,
        group: str,
        *,
        since: TimestampLike = None,
        from_: TimestampLike = None,
        to: TimestampLike = None,
        until: TimestampLike = None,
        type_: Optional[str] = None,
        order: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> RecordsEnvelope:
        """One page of GET /v2/groups/{group}/records (indexed groups).

        ``cursor`` must be the ``nextCursor`` of the previous page, passed
        back verbatim (signed; fabricating one earns 400 bad_cursor)."""
        params: Dict[str, Any] = {}
        if since is not None:
            params["since"] = _ts_param(since)
        if from_ is not None:
            params["from"] = _ts_param(from_)
        if to is not None:
            params["to"] = _ts_param(to)
        if until is not None:
            params["until"] = _ts_param(until)
        if type_:
            params["type"] = type_
        if order:
            params["order"] = order
        if limit is not None:
            params["limit"] = int(limit)
        if cursor:
            params["cursor"] = cursor
        resp = await self._request(
            "GET", f"v2/groups/{group}/records", params=params)
        body = self._json_object(resp, f"GET /v2/groups/{group}/records")
        try:
            return RecordsEnvelope.model_validate(body)
        except ValidationError as exc:
            raise JnpaInvalidResponse(
                f"records envelope failed validation ({group}): "
                f"{self._redact(str(exc))}") from exc

    async def iter_records(
        self,
        group: str,
        *,
        since: TimestampLike = None,
        order: str = "asc",
        limit: int = 500,
        type_: Optional[str] = None,
    ) -> AsyncIterator[IndexedRecord]:
        """Walk a group forward with cursor pagination (the sanctioned
        incremental pattern: since + order=asc + limit, PDF §4.4).

        Last-page detection: ``hasMore is False`` OR ``nextCursor is None``.
        An indexed envelope MISSING hasMore entirely is treated as final and
        logged (that shape is documented only for report groups — D9)."""
        cursor: Optional[str] = None
        while True:
            page = await self.records_page(
                group, since=since, order=order, limit=limit,
                type_=type_, cursor=cursor)
            for item in page.items:
                yield item
            if page.hasMore is None and page.nextCursor is None:
                if page.items:
                    self._observe(
                        "RUNTIME_INDEXED_ENVELOPE_NO_PAGINATION",
                        f"GET /v2/groups/{group}/records",
                        "indexed group answered without hasMore/nextCursor "
                        "(documented only for report groups — D9); treated "
                        "as final page", severity="WARN")
                return
            if not page.hasMore or not page.nextCursor:
                return
            cursor = page.nextCursor

    async def get_report(
        self,
        group: str,
        *,
        date_: TimestampLike = None,
        terminal: Optional[str] = None,
        from_: TimestampLike = None,
        to: TimestampLike = None,
        limit: Optional[int] = None,
    ) -> ReportEnvelope:
        """GET /v2/groups/{group}/records for a REPORT group (JSON items,
        no file references, 5-field envelope — defects D9/D10).

        There is NO defined last-page detection for report groups; when
        ``count == limit`` a truncation-risk observation is recorded."""
        params: Dict[str, Any] = {}
        if date_ is not None:
            params["date"] = _ts_param(date_)
        if terminal:
            params["terminal"] = terminal
        if from_ is not None:
            params["from"] = _ts_param(from_)
        if to is not None:
            params["to"] = _ts_param(to)
        if limit is not None:
            params["limit"] = int(limit)
        resp = await self._request(
            "GET", f"v2/groups/{group}/records", params=params)
        body = self._json_object(resp, f"GET /v2/groups/{group}/records")
        try:
            envelope = ReportEnvelope.model_validate(body)
        except ValidationError as exc:
            raise JnpaInvalidResponse(
                f"report envelope failed validation ({group}): "
                f"{self._redact(str(exc))}") from exc
        if any(isinstance(i, dict) and "file" in i for i in envelope.items):
            self._observe(
                "RUNTIME_REPORT_ITEM_HAS_FILE",
                f"GET /v2/groups/{group}/records",
                "report-group item carries a 'file' property (spec: report "
                "delivery issues no file reference)", severity="WARN")
        if limit is not None and envelope.count == limit and limit > 0:
            self._observe(
                "RUNTIME_REPORT_PAGE_TRUNCATION_RISK",
                f"GET /v2/groups/{group}/records",
                f"count == limit == {limit} with no pagination fields on a "
                "report group (D9) — results may be truncated", severity="WARN")
        return envelope

    async def fetch_file(
        self,
        file_ref: str,
        *,
        etag: Optional[str] = None,
        expected_sha256: Optional[str] = None,
    ) -> FileFetch:
        """GET /v2/files/{fileRef} — download the referenced file.

        ``etag`` (the stored checksum) is sent as If-None-Match for a cheap
        304 revalidation. On 200 the body is sha256-hashed locally and
        verified against ``expected_sha256`` (the record's checksumSha256)
        and the response ETag; a mismatch raises JnpaChecksumMismatch."""
        headers: Dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = f'"{etag.strip(chr(34))}"'
        resp = await self._request(
            "GET", f"v2/files/{file_ref}", headers=headers,
            expect_binary=True, allowed_statuses=(304,))
        # RFC 7232 weak validator. A front-end that gzips on the fly downgrades
        # the strong ETag to W/"<sha256>" (the Vercel sim does exactly this, and
        # httpx negotiates gzip by default), so the W/ prefix has to come off
        # BEFORE the quotes — .strip('"') alone leaves W/"<sha256> behind, which
        # then fails the comparison below on a byte-correct file.
        raw_etag = (resp.headers.get("ETag") or "").strip()
        etag_is_weak = raw_etag.startswith("W/")
        if etag_is_weak:
            raw_etag = raw_etag[2:].strip()
        resp_etag = raw_etag.strip('"') or None
        if resp.status_code == 304:
            return FileFetch(file_ref=file_ref, status=304, etag=resp_etag)
        content = resp.content
        digest = hashlib.sha256(content).hexdigest()
        # The live deployment gzips on the fly and its front-end appends the
        # content-coding to the ETag Apache-style ("<sha256>-gzip"); compare
        # on the base hash and record the deviation instead of failing.
        etag_cmp = resp_etag
        if etag_is_weak:
            self._observe(
                "RUNTIME_ETAG_WEAK_VALIDATOR", f"GET /v2/files/{file_ref}",
                f'response ETag is a weak validator (W/"{resp_etag}") because '
                f"the front-end gzipped the body; verified against the base hash")
        if resp_etag:
            match = re.fullmatch(r"([0-9a-fA-F]{64})-(?:gzip|br|deflate)",
                                 resp_etag)
            if match:
                etag_cmp = match.group(1)
                self._observe(
                    "RUNTIME_ETAG_CODING_SUFFIX", f"GET /v2/files/{file_ref}",
                    f"response ETag carries a content-coding suffix "
                    f"({resp_etag}); verified against the base hash")
        filename = _filename_from_disposition(
            resp.headers.get("Content-Disposition"))
        if filename is None:
            self._observe(
                "RUNTIME_NO_CONTENT_DISPOSITION", f"GET /v2/files/{file_ref}",
                "no filename in Content-Disposition; parsers that derive "
                "terminal/layout from the filename will need routing hints",
                severity="WARN")
        for label, expected in (("record checksumSha256", expected_sha256),
                                ("response ETag", etag_cmp)):
            if expected and expected.lower() != digest:
                self._observe(
                    "RUNTIME_CHECKSUM_MISMATCH", f"GET /v2/files/{file_ref}",
                    f"sha256(body)={digest} != {label}={expected}",
                    severity="ERROR")
                raise JnpaChecksumMismatch(file_ref, expected, digest)
        return FileFetch(
            file_ref=file_ref, status=200, content=content, filename=filename,
            media_type=resp.headers.get("Content-Type"), etag=resp_etag,
            sha256=digest, size_bytes=len(content))

    # ------------------------------------------------------------- plumbing
    def _redact(self, text: str) -> str:
        """No credential or issued token may surface in logs or exceptions."""
        for secret in (self.client_key,
                       self._token.access_token if self._token else None):
            if secret:
                text = text.replace(secret, "***")
        return text

    def _json_object(self, resp: httpx.Response, endpoint: str) -> Dict[str, Any]:
        try:
            body = resp.json()
        except ValueError as exc:
            raise JnpaInvalidResponse(f"{endpoint} returned non-JSON body") from exc
        if not isinstance(body, dict):
            raise JnpaInvalidResponse(
                f"{endpoint} returned a {type(body).__name__}, expected an object")
        return body

    async def _exchange_key(self) -> TokenInfo:
        """POST /v2/auth/token — client key -> 1-hour bearer. Caller holds
        the token lock. A 401 here is terminal (bad/unregistered key)."""
        client, owns = self._client()
        try:
            resp = await self._send(
                client, "POST", f"{self.api_url}/{TOKEN_PATH}",
                json_body={"clientKey": self.client_key})
        finally:
            if owns:
                await client.aclose()
        if resp.status_code == 401:
            # Deliberately slowed (~250 ms) and deliberately vague server-side.
            raise JnpaAuthError(
                "client key refused at /v2/auth/token (401) — key not "
                "registered, or the address is not in the server allowlist")
        if resp.status_code != 200:
            code, reason = self._error_parts(resp)
            raise JnpaHTTPError(resp.status_code, code, self._redact(reason or ""))
        body = self._json_object(resp, "POST /v2/auth/token")
        try:
            parsed = TokenResponse.model_validate(body)
        except ValidationError as exc:
            raise JnpaInvalidResponse(
                f"token response failed validation: {self._redact(str(exc))}") from exc
        if not parsed.accessToken:
            raise JnpaAuthError("token endpoint answered without accessToken")
        scopes = parsed.scopes or []
        if "admin:read" in scopes:
            # Defect D0b: undocumented over-privileged scope grant.
            self._observe("D0B_ADMIN_SCOPE_GRANTED", "POST /v2/auth/token",
                          f"token carries undocumented scopes: {scopes}",
                          severity="WARN")
        info = TokenInfo(
            access_token=parsed.accessToken,
            expires_in=float(parsed.expiresIn or 3600),
            acquired_monotonic=time.monotonic(),
            expires_at=parsed.expiresAt,
            scopes=scopes,
            client_id=parsed.client.id if parsed.client else None,
            organisation=parsed.client.organisation if parsed.client else None,
        )
        log.info("jnpa_token_issued", client_id=info.client_id,
                 expires_at=info.expires_at)
        return info

    def _client(self) -> tuple[httpx.AsyncClient, bool]:
        if self._http is not None:
            return self._http, False
        # SOCKS proxies (socks5://…) need the socksio extra; hostnames are
        # resolved BY the proxy, so the allowlisted egress also does the DNS.
        return httpx.AsyncClient(timeout=self.timeout_s, proxy=self.proxy), True

    async def _send(self, client: httpx.AsyncClient, method: str, url: str, *,
                    params: Optional[Dict[str, Any]] = None,
                    headers: Optional[Dict[str, str]] = None,
                    json_body: Optional[Dict[str, Any]] = None) -> httpx.Response:
        """One wire attempt, budget-gated and stats-counted. httpx encodes
        query values, so RFC3339 ``+05:30`` offsets leave as %2B05:30."""
        await self._budget.acquire()
        resp = await client.request(
            method, url, params=params, headers=headers, json=json_body,
            timeout=self.timeout_s)
        remaining_raw = resp.headers.get("RateLimit-Remaining")
        remaining = None
        if remaining_raw is not None:
            try:
                remaining = int(remaining_raw)
            except ValueError:
                remaining = None
        self._stats.record(resp.status_code, remaining,
                           len(resp.content) if resp.content else 0)
        return resp

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        auth: bool = True,
        expect_binary: bool = False,
        allowed_statuses: tuple = (),
    ) -> httpx.Response:
        """Request with bounded retries. Retries timeouts / network errors /
        5xx (exponential backoff) and 429 (blind 60 s + jitter — no
        Retry-After exists, defect D6); 401 forces exactly ONE re-auth; other
        4xx fail fast as typed errors."""
        if auth and not self.configured:
            raise JnpaNotConfigured("JNPA_PORTDATA_CLIENT_KEY is not set")
        url = f"{self.api_url}/{path}" if path else f"{self.api_url}/"
        client, owns = self._client()
        last_exc: JnpaError = JnpaUnavailable(f"no attempt made against {url}")
        reauthed = False
        try:
            for attempt in range(self.retries + 1):
                if attempt:
                    self._stats.retry_count += 1
                    await asyncio.sleep(self.backoff_s * (2 ** (attempt - 1)))
                req_headers = dict(headers or {})
                if auth:
                    token = await self.get_token()
                    req_headers["Authorization"] = f"Bearer {token.access_token}"
                try:
                    resp = await self._send(client, method, url,
                                            params=params, headers=req_headers)
                except httpx.TimeoutException as exc:
                    last_exc = JnpaTimeout(
                        f"JNPA API timed out after {self.timeout_s}s")
                    log.warning("jnpa_timeout", attempt=attempt,
                                error=self._redact(str(exc)))
                    continue
                except httpx.HTTPError as exc:
                    last_exc = JnpaUnavailable(
                        f"JNPA API unreachable: {self._redact(str(exc))}")
                    log.warning("jnpa_unreachable", attempt=attempt,
                                error=self._redact(str(exc)))
                    continue

                if resp.status_code == 200 or resp.status_code in allowed_statuses:
                    return resp

                if resp.status_code == 401 and auth:
                    # Expired/refused bearer: ONE forced re-auth, then fail.
                    if not reauthed:
                        log.info("jnpa_token_refused_reauth")
                        async with self._token_lock:
                            self._token = None
                        reauthed = True
                        continue
                    raise JnpaAuthError(
                        "bearer refused twice (401 after forced re-auth)")

                code, reason = self._error_parts(resp)

                if resp.status_code == 429:
                    wait = (self.rate_limited_wait_s
                            + random.uniform(0, self.rate_limited_jitter_s))
                    last_exc = JnpaRateLimited(
                        f"rate limited (429): {reason or 'no detail'}")
                    log.warning("jnpa_rate_limited", attempt=attempt,
                                wait_s=round(wait, 1))
                    await asyncio.sleep(wait)
                    continue

                if resp.status_code >= 500:
                    last_exc = JnpaHTTPError(resp.status_code, code,
                                             self._redact(reason or ""))
                    log.warning("jnpa_5xx", attempt=attempt,
                                status=resp.status_code, code=code)
                    continue

                # Remaining 4xx — fail fast, typed.
                raise JnpaHTTPError(resp.status_code, code,
                                    self._redact(reason or ""))
            raise last_exc
        finally:
            if owns:
                await client.aclose()

    @staticmethod
    def _error_parts(resp: httpx.Response) -> tuple[Optional[str], Optional[str]]:
        """The API's error body: {"error": code, "message": text, ...}."""
        try:
            body = resp.json()
        except ValueError:
            return None, None
        if not isinstance(body, dict):
            return None, None
        try:
            parsed = ApiErrorBody.model_validate(body)
        except ValidationError:
            return None, None
        return parsed.error, parsed.message


__all__ = ["JnpaPortDataClient", "DEFAULT_API_URL", "TOKEN_PATH",
           "RATE_LIMITED_WAIT_S", "RATE_LIMITED_JITTER_S"]
