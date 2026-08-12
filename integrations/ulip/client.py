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

Any single API can be given its own budget with ``ULIP_<KEY>_TIMEOUT_S`` (see
:data:`DEFAULT_API_TIMEOUTS`) — ``ULIP_LDB_TIMEOUT_S`` defaults to 30 s because
LDB/01 takes 10-20 s on production while everything else answers in under a
second.

Every granted API path is env-overridable via ``ULIP_<NAME>_API`` (see
:data:`DEFAULT_API_PATHS`) — e.g. ``ULIP_FASTAG_API`` (default ``FASTAG/01``),
``ULIP_LDB_API`` (default ``LDB/01``), ``ULIP_VAHAN_RC_API`` (default
``VAHAN/04``). NLDSL versions these paths independently of the account, so a
bumped API never needs a code change.

Auth model: when only ULIP_CLIENT_ID/SECRET are set the client logs in
(``POST {base}/user/login``), caches the issued bearer token for
ULIP_TOKEN_TTL_S, and forces exactly ONE re-login when an API call answers
401/403 (an expired token); a second rejection surfaces as
:class:`~integrations.ulip.exceptions.UlipAuthError` — retrying rejected
credentials cannot help. A static ULIP_API_KEY takes precedence when present.

A 412 on login is NOT a credential failure: it is ULIP's source-IP allowlist
gate ("Access denied Please contact ULIP support!"), returned identically for a
nonexistent username. It surfaces as the distinct
:class:`~integrations.ulip.exceptions.UlipAccessDenied` so health surfaces can
say "register the egress IP with NLDSL" rather than "check the password".

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
import re
import time
from typing import Any, Dict, Optional

import httpx
from pydantic import ValidationError

from jnpa_shared.logging import get_logger

from .exceptions import (
    UlipAccessDenied,
    UlipAuthError,
    UlipError,
    UlipHTTPError,
    UlipInvalidRequest,
    UlipInvalidResponse,
    UlipNotConfigured,
    UlipTimeout,
    UlipUnavailable,
)
from .schemas import UlipEnvelope

log = get_logger("integrations.ulip.client")

DEFAULT_API_URL = "https://www.ulip.dpiit.gov.in/ulip/v1.0.0"
STAGING_API_URL = "https://www.ulipstaging.dpiit.gov.in/ulip/v1.0.0"
LOGIN_PATH = "user/login"

# ULIP requires an explicit JSON Accept header and rejects anything else with a
# bodiless **HTTP 400** — including httpx's default ``Accept: */*``. Verified
# against staging on 2026-08-11: identical login payload, `*/*` -> 400,
# `application/json` -> 200. Send this on every request, login included.
JSON_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

# The 13 APIs granted to this account, keyed by the logical name used in the
# ``ULIP_<KEY>_API`` env override. Values are the paths documented in
# ulip-docs/*.pdf. NLDSL also documents VAHAN/05, VAHAN/06, GATISHAKTI/05 and
# CFSICD/01 — deliberately absent here because they are NOT granted.
DEFAULT_API_PATHS: Dict[str, str] = {
    "FASTAG": "FASTAG/01",          # toll crossings (72-hour retention!)
    "FASTAG_TAG": "FASTAG/02",      # tag registry / status
    "LDB": "LDB/01",                # container tracking
    "VAHAN_RC": "VAHAN/04",         # RC by vehicle number — native JSON
    "VAHAN_RC_XML": "VAHAN/01",     # RC by vehicle number — XML-in-JSON
    "VAHAN_CHASSIS": "VAHAN/02",    # RC by chassis number — XML-in-JSON
    "VAHAN_ENGINE": "VAHAN/03",     # RC by engine number  — XML-in-JSON
    "SARATHI_DL": "SARATHI/02",     # DL by licence number — clean JSON
    "SARATHI_DL_DOB": "SARATHI/01", # DL by licence number + date of birth
    "GS_NH_ROAD": "GATISHAKTI/01",  # national-highway detail by NH number
    "GS_STATE_ROADS": "GATISHAKTI/02",   # state road network by state id
    "GS_ROAD_POINTS": "GATISHAKTI/03",   # named road points (lat/lon) by state id
    "GS_TOLL_PLAZAS": "GATISHAKTI/04",   # NHAI toll plazas by state id
}

# Backwards-compatible aliases (imported by services/logistics and its tests).
DEFAULT_FASTAG_API = DEFAULT_API_PATHS["FASTAG"]
DEFAULT_LDB_API = DEFAULT_API_PATHS["LDB"]

# Per-API timeout budgets, overriding ULIP_TIMEOUT_S for the APIs that need it.
#
# LDB/01 aggregates a container's whole trail across terminals, rail and road
# and is genuinely slow — and, more awkwardly, VARIABLE. Measured on production:
# 0.1-0.7 s for every other granted API, but LDB at 14.5 s for TCLU8538808 and
# 35.5 s for CXRU1145597 in the same minute. The 5 s default timed it out on all
# three retries, so container tracking failed 100% of the time while looking
# like an outage; a 30 s budget still lost the slower containers, and because a
# timeout is RETRIED the failure cost ~40 s before surfacing.
#
# The budget is therefore set above the slowest observed call, not the typical
# one: a timeout here does not mean the upstream is broken, only that it is
# slow, and retrying a slow call just doubles the wait. Raising ULIP_TIMEOUT_S
# globally would be the wrong fix — a gate decision must not wait a minute on a
# VAHAN lookup — so the budget is per API. Env override: ``ULIP_<KEY>_TIMEOUT_S``.
DEFAULT_API_TIMEOUTS: Dict[str, float] = {"LDB": 60.0}

# Request-field patterns, copied verbatim from the integration PDFs. ULIP
# answers a violation with HTTP 400 and echoes the pattern, so rejecting the
# argument here saves a round trip and gives a far clearer error. Kept
# deliberately close to the docs — do NOT tighten them to what "looks right".
_RE_VEHICLE_FASTAG = re.compile(r"^[A-Z0-9]{5,11}$|^[A-Z0-9]{17,20}$")
_RE_VEHICLE_VAHAN = re.compile(r"^[a-zA-Z0-9]{5,11}$")
_RE_CHASSIS = re.compile(r"^[a-zA-Z0-9]{1,20}$")
_RE_ENGINE = re.compile(r"^[a-zA-Z0-9]{1,20}$")
_RE_CONTAINER = re.compile(r"^[a-zA-Z0-9]{8,15}$")
_RE_TAGID = re.compile(r"^[A-Z0-9]{0,25}$")
_RE_DL = re.compile(r"^.{1,25}$")
_RE_DOB = re.compile(r"^\d{4}-(0[1-9]|1[012])-(0[1-9]|[12][0-9]|3[01])$")
_RE_NH_NO = re.compile(r"^[A-Z]{2}-[A-Z0-9]{1,4}$")
_RE_STATE_ID = re.compile(r"^[0-9]{1,10}$")


def _require(value: Any, pattern: re.Pattern[str], field: str) -> str:
    """Normalise and pre-validate one request field against its documented
    pattern, raising the same shape of complaint ULIP would answer with."""
    text = str(value or "").strip()
    if not text:
        raise UlipInvalidRequest(f"{field} is required")
    if not pattern.match(text):
        raise UlipInvalidRequest(
            f"Data format failed OR wrong value entered at: {field}. "
            f"Format should follow {pattern.pattern}")
    return text


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
    """Async client for every granted ULIP gateway API.

    One client for all of FASTAG / LDB / VAHAN / SARATHI / GATISHAKTI, so the
    login token, retry budget, redaction rules and audit shape are shared
    rather than reimplemented per vertical.

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
        api_paths: Optional[Dict[str, str]] = None,
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
        # API-path registry: explicit override > ULIP_<KEY>_API env > doc default.
        self.api_paths = {
            key: ((api_paths or {}).get(key)
                  or env.get(f"ULIP_{key}_API", "").strip()
                  or default).strip("/")
            for key, default in DEFAULT_API_PATHS.items()
        }
        # The two original kwargs predate the registry and stay honoured.
        if fastag_api:
            self.api_paths["FASTAG"] = fastag_api.strip("/")
        if ldb_api:
            self.api_paths["LDB"] = ldb_api.strip("/")
        # Per-API timeout budgets, keyed by the RESOLVED path so _call_api can
        # find one without every fetch method having to pass its key through.
        self.api_timeouts = {
            key: _as_float(env.get(f"ULIP_{key}_TIMEOUT_S"),
                           DEFAULT_API_TIMEOUTS.get(key, self.timeout_s))
            for key in self.api_paths
        }
        self._timeout_by_path = {self.api_paths[key]: budget
                                 for key, budget in self.api_timeouts.items()}
        self._http = http_client
        self._token: Optional[str] = None
        self._token_at: float = 0.0

    def api_path(self, key: str) -> str:
        """The configured path for one logical API (see DEFAULT_API_PATHS)."""
        try:
            return self.api_paths[key]
        except KeyError:  # pragma: no cover - programming error, not runtime
            raise ValueError(f"unknown ULIP API key {key!r}; "
                             f"known: {sorted(DEFAULT_API_PATHS)}") from None

    # Pre-registry attribute names, kept so services/logistics and its health
    # payload ("apis": {"vehicle": …, "container": …}) need no change.
    @property
    def fastag_api(self) -> str:
        return self.api_paths["FASTAG"]

    @property
    def ldb_api(self) -> str:
        return self.api_paths["LDB"]

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

    # ------------------------------------------------------- public API: FASTag
    async def fetch_vehicle_movement(self, vehicle_number: str) -> UlipEnvelope:
        """FASTAG/01 — toll-crossing history for one vehicle registration
        number (the vehicle-movement signal for the corridor).

        IMPORTANT: NLDSL retains only the **past 72 hours** of crossings for a
        given VRN. An on-demand call is therefore empty for most vehicles most
        of the time; durable history has to be accumulated by polling (see
        ``services.fastag.poller``), never assembled at read time.
        """
        return await self._call_api(
            self.api_path("FASTAG"),
            {"vehiclenumber": _require(vehicle_number.upper(),
                                       _RE_VEHICLE_FASTAG, "vehiclenumber")})

    async def fetch_tag_status(self, *, vehicle_number: Optional[str] = None,
                               tag_id: Optional[str] = None) -> UlipEnvelope:
        """FASTAG/02 — tag registry / status by vehicle number OR tag id.

        Exactly one of the two must be supplied: the doc is explicit that
        sending both makes the upstream answer ``respCode 239`` inside a 200,
        and sending neither yields a ``responseStatus: ERROR`` block. Both are
        wasted calls against a rate-limited subscription, so they are rejected
        here instead.
        """
        veh = (vehicle_number or "").strip().upper()
        tag = (tag_id or "").strip().upper()
        if veh and tag:
            raise UlipInvalidRequest(
                "vehiclenumber and tagid are mutually exclusive for FASTAG/02 "
                "— supply exactly one (upstream answers respCode 239 for both)")
        if not veh and not tag:
            raise UlipInvalidRequest(
                "vehiclenumber or tagid :must not be Empty or null, "
                "Please enter vehiclenumber or tagid")
        payload = ({"vehiclenumber": _require(veh, _RE_VEHICLE_FASTAG, "vehiclenumber")}
                   if veh else {"tagid": _require(tag, _RE_TAGID, "tagid")})
        return await self._call_api(self.api_path("FASTAG_TAG"), payload)

    # ---------------------------------------------------------- public API: LDB
    async def fetch_container_tracking(self, container_number: str) -> UlipEnvelope:
        """LDB/01 — Logistics Data Bank tracking for one container number."""
        return await self._call_api(
            self.api_path("LDB"),
            {"containerNumber": _require(container_number.upper(),
                                         _RE_CONTAINER, "containerNumber")})

    # -------------------------------------------------------- public API: VAHAN
    async def fetch_vehicle_by_rc(self, vehicle_number: str) -> UlipEnvelope:
        """VAHAN/04 — RC particulars by vehicle number, as native JSON.

        Preferred over VAHAN/01: same input, same data, but no XML string to
        unwrap. Note VAHAN masks ``rc_owner_name``, ``rc_f_name``,
        ``rc_mobile_no`` and both address fields by default for all users.
        """
        return await self._call_api(
            self.api_path("VAHAN_RC"),
            {"vehiclenumber": _require(vehicle_number.upper(),
                                       _RE_VEHICLE_VAHAN, "vehiclenumber")})

    async def fetch_vehicle_by_rc_xml(self, vehicle_number: str) -> UlipEnvelope:
        """VAHAN/01 — RC particulars by vehicle number, as an XML string
        wrapped in the JSON envelope. Kept as the retry rung behind VAHAN/04:
        the two are fed by different upstream calls, so one can answer when the
        other misses."""
        return await self._call_api(
            self.api_path("VAHAN_RC_XML"),
            {"vehiclenumber": _require(vehicle_number.upper(),
                                       _RE_VEHICLE_VAHAN, "vehiclenumber")})

    async def fetch_vehicle_by_chassis(self, chassis_number: str) -> UlipEnvelope:
        """VAHAN/02 — RC particulars by chassis number (XML-in-JSON).

        Note the documented request key is ``chasisnumber`` — ULIP's spelling,
        not a typo here."""
        return await self._call_api(
            self.api_path("VAHAN_CHASSIS"),
            {"chasisnumber": _require(chassis_number.upper(),
                                      _RE_CHASSIS, "chasisnumber")})

    async def fetch_vehicle_by_engine(self, engine_number: str) -> UlipEnvelope:
        """VAHAN/03 — RC particulars by engine number (XML-in-JSON)."""
        return await self._call_api(
            self.api_path("VAHAN_ENGINE"),
            {"enginenumber": _require(engine_number.upper(),
                                      _RE_ENGINE, "enginenumber")})

    # ------------------------------------------------------ public API: SARATHI
    async def fetch_dl(self, dl_number: str) -> UlipEnvelope:
        """SARATHI/02 — driving-licence particulars by licence number.

        Preferred over SARATHI/01: it needs no date of birth (which the gate
        rarely holds) and answers a compact ``DLinformation`` object rather than
        the deep ``dldetobj`` tree.
        """
        return await self._call_api(
            self.api_path("SARATHI_DL"),
            {"dlnumber": _require(dl_number.upper(), _RE_DL, "dlnumber")})

    async def fetch_dl_with_dob(self, dl_number: str, dob: str) -> UlipEnvelope:
        """SARATHI/01 — driving-licence particulars by licence number + DOB.

        Richer than SARATHI/02 (endorsements, COV history, badge, objections)
        but needs a date of birth as ``yyyy-mm-dd``.
        """
        return await self._call_api(
            self.api_path("SARATHI_DL_DOB"),
            {"dlnumber": _require(dl_number.upper(), _RE_DL, "dlnumber"),
             "dob": _require(dob, _RE_DOB, "dob")})

    # --------------------------------------------------- public API: GATISHAKTI
    async def fetch_nh_road(self, nh_no: str) -> UlipEnvelope:
        """GATISHAKTI/01 — national-highway detail for one NH number
        (e.g. ``NH-348``, the JNPA corridor)."""
        return await self._call_api(
            self.api_path("GS_NH_ROAD"),
            {"nhno": _require(nh_no.upper(), _RE_NH_NO, "nhno")})

    async def fetch_state_roads(self, state_id: Any) -> UlipEnvelope:
        """GATISHAKTI/02 — the road network for one state id (27 = Maharashtra)."""
        return await self._call_api(
            self.api_path("GS_STATE_ROADS"),
            {"stateid": _require(state_id, _RE_STATE_ID, "stateid")})

    async def fetch_state_road_points(self, state_id: Any) -> UlipEnvelope:
        """GATISHAKTI/03 — named road points (``vname``/``lat``/``lon``) for
        one state id."""
        return await self._call_api(
            self.api_path("GS_ROAD_POINTS"),
            {"stateid": _require(state_id, _RE_STATE_ID, "stateid")})

    async def fetch_toll_plazas(self, state_id: Any) -> UlipEnvelope:
        """GATISHAKTI/04 — NHAI toll plazas for one state id. The registry that
        gives FASTAG/01's ``tollPlazaName`` a canonical geocode."""
        return await self._call_api(
            self.api_path("GS_TOLL_PLAZAS"),
            {"stateid": _require(state_id, _RE_STATE_ID, "stateid")})

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
        path = api_path.strip("/")
        budget = self._timeout_by_path.get(path, self.timeout_s)
        client = self._http or httpx.AsyncClient(timeout=budget)
        owns = self._http is None
        url = f"{self.api_url}/{path}"
        try:
            body = await self._post_json(client, url, payload,
                                         allow_reauth=self.auth_mode == "login",
                                         timeout_s=budget)
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
                f"code={envelope.code!r} message={self._redact(_envelope_reason(envelope))!r}")
        return envelope

    async def _post_json(self, client: httpx.AsyncClient, url: str,
                         payload: Dict[str, Any], *, allow_reauth: bool,
                         timeout_s: Optional[float] = None) -> Dict[str, Any]:
        """POST with bounded retries. Retries timeouts / network errors / 5xx;
        401/403 forces ONE re-login (login mode) then fails as UlipAuthError;
        other 4xx (429 rate limit included) fail fast."""
        budget = self.timeout_s if timeout_s is None else timeout_s
        last_exc: UlipError = UlipUnavailable(f"no attempt made against {url}")
        reauthed = False
        for attempt in range(self.retries + 1):
            if attempt:
                await asyncio.sleep(self.backoff_s * (2 ** (attempt - 1)))
            token = await self._ensure_token(client)
            try:
                resp = await client.post(
                    url, json=payload,
                    headers={"Authorization": f"Bearer {token}", **JSON_HEADERS},
                    timeout=budget,
                )
            except httpx.TimeoutException as exc:
                last_exc = UlipTimeout(f"ULIP timed out after {budget}s")
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

            if resp.status_code == 412:
                # The source-IP allowlist gate — see UlipAccessDenied. Never
                # retried and never confused with a bad credential.
                raise UlipAccessDenied(
                    "ULIP denied access before evaluating the credential "
                    f"(HTTP 412: {_error_reason(resp) or 'Access denied'}) — "
                    "the caller's egress IP is not registered with NLDSL")

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
                headers=JSON_HEADERS,
                timeout=self.timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise UlipTimeout(f"ULIP login timed out after {self.timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise UlipUnavailable(
                f"ULIP login unreachable: {self._redact(str(exc))}") from exc
        if resp.status_code == 412:
            # 412 is returned WITHOUT evaluating the password — for an
            # unregistered caller IP and for a username ULIP does not know.
            # A wrong password against a known username answers 401 instead
            # (both verified against staging on 2026-08-11), so 412 never
            # means "check the password".
            raise UlipAccessDenied(
                "ULIP login denied before the credential was evaluated "
                f"(HTTP 412: {_error_reason(resp) or 'Access denied'}) — "
                "this deployment's egress IP is not registered with NLDSL, "
                "or ULIP does not know the configured username")
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


def _envelope_reason(envelope: "UlipEnvelope") -> str:
    """The most useful text in a failing envelope.

    ULIP frequently leaves the top-level ``message`` null and puts the real
    reason in the first response element — an upstream outage arrives as
    ``{"response": [{"response": "LDB_01 - 3rd party service is down!",
    "responseStatus": "ERROR"}], "error": "true"}``. Reporting ``message=None``
    for that would send the reader hunting for a bug in our own code.
    """
    if envelope.message not in (None, ""):
        return str(envelope.message)
    items = envelope.response
    if isinstance(items, dict):
        items = [items]
    for item in items or []:
        if isinstance(item, str) and item.strip():
            return item.strip()
        if isinstance(item, dict):
            inner = item.get("response") or item.get("message")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return str(envelope.message)


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


__all__ = ["UlipClient", "DEFAULT_API_URL", "STAGING_API_URL", "DEFAULT_API_PATHS",
           "DEFAULT_FASTAG_API", "DEFAULT_LDB_API", "LOGIN_PATH"]
