"""JNPA Port-Data API client unit tests (no DB / no network / no sim).

Each strict-adherence behavior of integrations/jnpa_portdata has a dedicated
test (same pattern as tests/test_ulip_logistics.py, httpx.MockTransport):

  * base-URL normalisation      (a /v2 suffix is stripped + observed — D1)
  * %2B timestamp encoding      (the wire URL never carries a raw '+' — D22)
  * cursor passed verbatim      (opaque string survives round-trip — D13)
  * tolerant report envelope    (5-field answer parses — D9)
  * checksum verification       (mismatch -> JnpaChecksumMismatch + obs)
  * filename extraction         (quoted / unquoted / RFC 5987 / absent)
  * single-flight token         (N concurrent callers -> ONE token POST)
  * proactive + reactive auth   (refresh near expiry; ONE re-auth on 401,
                                 second 401 -> JnpaAuthError)
  * 429 blind backoff           (no Retry-After exists — wait then retry)
  * RateLimit-Remaining absence (never an error; stats track presence)
  * not-configured posture      (JnpaNotConfigured, provider disabled)
  * credential redaction        (key/token never in exception text)
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable, List, Optional

import httpx
import pytest

from integrations.jnpa_portdata import (
    JnpaAuthError,
    JnpaChecksumMismatch,
    JnpaHTTPError,
    JnpaNotConfigured,
    JnpaPortDataClient,
    JnpaRateLimited,
    ReportEnvelope,
)
from integrations.jnpa_portdata.client import _filename_from_disposition

BASE = "https://sim.test/poc-api-data-access"
KEY = "VGhpc0lzQVRlc3RLZXk="

TOKEN_BODY = {
    "accessToken": "tok-abc123",
    "tokenType": "Bearer",
    "expiresIn": 3600,
    "expiresAt": "2026-08-05T01:00:00+05:30",
    "scopes": ["groups:read", "files:read"],
    "client": {"id": "cli_test", "organisation": "TEST"},
}


def make_client(handler: Callable[[httpx.Request], httpx.Response],
                **kwargs) -> JnpaPortDataClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    kwargs.setdefault("retries", 0)
    kwargs.setdefault("backoff_s", 0.0)
    return JnpaPortDataClient(BASE, client_key=KEY, http_client=http, **kwargs)


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------- URL and params
def test_base_url_v2_suffix_stripped_and_observed():
    client = JnpaPortDataClient(BASE + "/v2", client_key=KEY)
    assert client.api_url == BASE
    codes = [o.code for o in client.drain_observations()]
    assert "D1_BASE_URL_V2" in codes


def test_timestamp_offset_is_percent_encoded_on_the_wire():
    seen: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json=TOKEN_BODY)
        seen.append(str(request.url))
        return httpx.Response(200, json={
            "asOf": "x", "group": "customs", "delivery": "indexed",
            "order": "asc", "count": 0, "matched": 0, "hasMore": False,
            "nextCursor": None, "items": []})

    client = make_client(handler)
    run(client.records_page("customs", since="2026-07-30T00:00:00+05:30",
                            order="asc", limit=500))
    assert len(seen) == 1
    assert "%2B05%3A30" in seen[0]          # encoded offset present
    assert "+05:30" not in seen[0]          # raw plus never on the wire


def test_cursor_is_passed_verbatim():
    seen: List[httpx.URL] = []
    cursor = "ref_NjI0Mwp3SrsyLUpVBp"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json=TOKEN_BODY)
        seen.append(request.url)
        return httpx.Response(200, json={
            "asOf": "x", "group": "customs", "delivery": "indexed",
            "order": "asc", "count": 0, "matched": 0, "hasMore": False,
            "nextCursor": None, "items": []})

    client = make_client(handler)
    run(client.records_page("customs", cursor=cursor))
    assert seen[0].params["cursor"] == cursor


# ------------------------------------------------------------ token lifecycle
def test_token_single_flight_under_concurrency():
    token_posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            token_posts.append(1)
            return httpx.Response(200, json=TOKEN_BODY)
        return httpx.Response(200, json={"status": "ok"})

    client = make_client(handler)

    async def burst():
        await asyncio.gather(*(client.get_token() for _ in range(8)))

    run(burst())
    assert len(token_posts) == 1


def test_token_proactive_refresh_inside_margin():
    token_posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            token_posts.append(1)
            body = dict(TOKEN_BODY)
            body["accessToken"] = f"tok-{len(token_posts)}"
            body["expiresIn"] = 100     # expires_in 100s < margin 300s
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={"status": "ok"})

    client = make_client(handler)  # default margin 300s > expiresIn

    async def scenario():
        first = await client.get_token()
        second = await client.get_token()
        return first, second

    first, second = run(scenario())
    assert len(token_posts) == 2           # always inside the margin => refresh
    assert first.access_token != second.access_token


def test_one_reactive_reauth_on_401_then_success():
    calls = {"token": 0, "data": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            calls["token"] += 1
            body = dict(TOKEN_BODY)
            body["accessToken"] = f"tok-{calls['token']}"
            return httpx.Response(200, json=body)
        calls["data"] += 1
        if calls["data"] == 1:
            return httpx.Response(401, json={
                "error": "unauthorized", "message": "Token is not valid",
                "reason": "expired"})
        return httpx.Response(200, json={"groups": [
            {"group": g} for g in (
                "nlp-marine", "bathymetry", "port-craft-pilot",
                "shipping-lines", "customs", "edi-messages",
                "berthing-reports", "gate-documents", "rail-fois",
                "rail-form11-icd", "transport", "daily-reports", "cfs-ecy")]})

    client = make_client(handler, retries=2)
    envelope = run(client.list_groups())
    assert len(envelope.groups) == 13
    assert calls["token"] == 2             # initial + ONE forced re-auth


def test_second_401_surfaces_as_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json=TOKEN_BODY)
        return httpx.Response(401, json={
            "error": "unauthorized", "message": "Token is not valid"})

    client = make_client(handler, retries=3)
    with pytest.raises(JnpaAuthError):
        run(client.list_groups())


def test_bad_client_key_is_terminal_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={
            "error": "unauthorized", "message": "Client key not recognised"})

    client = make_client(handler)
    with pytest.raises(JnpaAuthError):
        run(client.get_token())


def test_not_configured_is_disabled_not_broken():
    client = JnpaPortDataClient(BASE, client_key="")
    assert not client.configured
    with pytest.raises(JnpaNotConfigured):
        run(client.list_groups())


# ------------------------------------------------------------- rate limiting
def test_429_blind_backoff_then_retry_succeeds():
    calls = {"data": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json=TOKEN_BODY)
        calls["data"] += 1
        if calls["data"] == 1:
            # Faithful: 429 with NO Retry-After, NO RateLimit-* headers.
            return httpx.Response(429, json={
                "error": "rate_limited",
                "message": "The per-minute request allowance has been exhausted"})
        return httpx.Response(200, json={"groups": []})

    client = make_client(handler, retries=1,
                         rate_limited_wait_s=0.01, rate_limited_jitter_s=0.0)
    run(client.list_groups())
    assert calls["data"] == 2
    assert client.request_stats().http_429_count == 1


def test_429_exhausting_retries_raises_rate_limited():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json=TOKEN_BODY)
        return httpx.Response(429, json={"error": "rate_limited",
                                         "message": "exhausted"})

    client = make_client(handler, retries=1,
                         rate_limited_wait_s=0.0, rate_limited_jitter_s=0.0)
    with pytest.raises(JnpaRateLimited):
        run(client.list_groups())


def test_missing_ratelimit_header_is_not_an_error_and_stats_track_presence():
    responses = iter([
        httpx.Response(200, json={"groups": []},
                       headers={"RateLimit-Remaining": "596"}),
        httpx.Response(200, json={"groups": []}),   # header absent (D5)
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json=TOKEN_BODY)
        return next(responses)

    client = make_client(handler)

    async def scenario():
        await client.list_groups()
        await client.list_groups()

    run(scenario())
    stats = client.request_stats()
    assert stats.rate_limit_remaining_last == 596
    assert stats.rate_limit_remaining_min == 596


# ------------------------------------------------------------------- errors
def test_unknown_group_maps_to_typed_http_error_with_code():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json=TOKEN_BODY)
        return httpx.Response(404, json={
            "error": "unknown_group", "message": "No group 'nope'",
            "availableGroups": ["customs"]})

    client = make_client(handler)
    with pytest.raises(JnpaHTTPError) as excinfo:
        run(client.records_page("nope"))
    assert excinfo.value.status_code == 404
    assert excinfo.value.error_code == "unknown_group"
    assert not excinfo.value.is_bad_cursor


def test_credentials_never_in_exception_text():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json=TOKEN_BODY)
        return httpx.Response(400, json={
            "error": "bad_parameter",
            "message": f"echoing secrets {KEY} tok-abc123"})

    client = make_client(handler)
    with pytest.raises(JnpaHTTPError) as excinfo:
        run(client.records_page("customs"))
    text = str(excinfo.value)
    assert KEY not in text
    assert "tok-abc123" not in text


# ------------------------------------------------------------------- files
def _file_response(content: bytes, sha: str,
                   disposition: Optional[str]) -> httpx.Response:
    headers = {"ETag": f'"{sha}"', "Content-Type": "application/xml"}
    if disposition:
        headers["Content-Disposition"] = disposition
    return httpx.Response(200, content=content, headers=headers)


def test_fetch_file_verifies_checksum_and_extracts_filename():
    import hashlib
    content = b"<CHPOI03Payload/>"
    sha = hashlib.sha256(content).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json=TOKEN_BODY)
        return _file_response(content, sha,
                              'attachment; filename="IGM_1.xml"')

    client = make_client(handler)
    fetched = run(client.fetch_file("ref_X", expected_sha256=sha))
    assert fetched.status == 200
    assert fetched.filename == "IGM_1.xml"
    assert fetched.sha256 == sha


def test_fetch_file_checksum_mismatch_raises_and_observes():
    import hashlib
    content = b"<CHPOI03Payload/>"
    wrong = hashlib.sha256(b"other").hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json=TOKEN_BODY)
        # ETag matches the body; the RECORD's checksum is what mismatches.
        return _file_response(content,
                              hashlib.sha256(content).hexdigest(),
                              'attachment; filename="IGM_1.xml"')

    client = make_client(handler)
    with pytest.raises(JnpaChecksumMismatch):
        run(client.fetch_file("ref_X", expected_sha256=wrong))
    codes = [o.code for o in client.drain_observations()]
    assert "RUNTIME_CHECKSUM_MISMATCH" in codes


def test_fetch_file_304_revalidation():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json=TOKEN_BODY)
        assert request.headers.get("If-None-Match") == '"abc"'
        return httpx.Response(304, headers={"ETag": '"abc"'})

    client = make_client(handler)
    fetched = run(client.fetch_file("ref_X", etag="abc"))
    assert fetched.not_modified
    assert fetched.content is None


def test_missing_content_disposition_is_observed_not_fatal():
    import hashlib
    content = b"data"
    sha = hashlib.sha256(content).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json=TOKEN_BODY)
        return _file_response(content, sha, None)

    client = make_client(handler)
    fetched = run(client.fetch_file("ref_X"))
    assert fetched.filename is None
    codes = [o.code for o in client.drain_observations()]
    assert "RUNTIME_NO_CONTENT_DISPOSITION" in codes


# ----------------------------------------------------------- report envelope
def test_report_envelope_tolerates_missing_pagination_trio():
    body = {"group": "berthing-reports", "delivery": "report",
            "asOf": "2026-07-31T23:56:21+05:30", "count": 0, "items": []}
    envelope = ReportEnvelope.model_validate(body)
    assert envelope.hasMore is None
    assert envelope.matched is None
    assert envelope.nextCursor is None


def test_report_truncation_risk_is_observed_when_count_equals_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json=TOKEN_BODY)
        return httpx.Response(200, json={
            "group": "daily-reports", "delivery": "report", "asOf": "x",
            "count": 2, "items": [{"a": 1}, {"a": 2}]})

    client = make_client(handler)
    run(client.get_report("daily-reports", date_="2026-08-03", limit=2))
    codes = [o.code for o in client.drain_observations()]
    assert "RUNTIME_REPORT_PAGE_TRUNCATION_RISK" in codes


# ------------------------------------------------------- filename extraction
@pytest.mark.parametrize("header,expected", [
    ('attachment; filename="CHPOI03_IGM_1197294_31-07-2026-195900.xml"',
     "CHPOI03_IGM_1197294_31-07-2026-195900.xml"),
    ("attachment; filename=plain.csv", "plain.csv"),
    ("attachment; filename*=UTF-8''x%20y.xml", "x y.xml"),
    ("attachment", None),
    (None, None),
])
def test_filename_from_disposition(header, expected):
    assert _filename_from_disposition(header) == expected
