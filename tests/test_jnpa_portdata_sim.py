"""JNPA Port-Data API sim behaviour tests — the error model and the faithful
header/envelope omissions, asserted at the raw HTTP level (the client hides
several of these on purpose; the sim must still emulate them exactly).
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from _jnpa_sim_fixtures import (
    SIM_KEY,
    build_fixture_corpus,
    fresh_sim,
    sim_asgi_app,
)


@pytest.fixture()
def sim_state(tmp_path):
    return fresh_sim(build_fixture_corpus(tmp_path))


def run(coro):
    return asyncio.run(coro)


async def _http():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=sim_asgi_app()),
                             base_url="http://sim")


async def _token(http) -> str:
    resp = await http.post("/v2/auth/token", json={"clientKey": SIM_KEY})
    return resp.json()["accessToken"]


def test_auth_error_vocabulary(sim_state):
    async def scenario():
        async with await _http() as http:
            missing = await http.get("/v2/groups")
            invalid = await http.get(
                "/v2/groups", headers={"Authorization": "Bearer bogus"})
            return missing, invalid
    missing, invalid = run(scenario())
    assert missing.status_code == 401
    assert missing.json()["message"] == "Bearer token required"
    assert invalid.status_code == 401
    assert invalid.json()["message"] == "Token is not valid"
    assert "reason" not in invalid.json()


def test_expired_token_carries_reason(tmp_path):
    fresh_sim(build_fixture_corpus(tmp_path), token_ttl_s=0.05)

    async def scenario():
        async with await _http() as http:
            token = await _token(http)
            await asyncio.sleep(0.1)
            return await http.get(
                "/v2/groups", headers={"Authorization": f"Bearer {token}"})
    resp = run(scenario())
    assert resp.status_code == 401
    assert resp.json()["reason"] == "expired"


def test_unknown_group_lists_available_groups(sim_state):
    async def scenario():
        async with await _http() as http:
            token = await _token(http)
            return await http.get(
                "/v2/groups/not-a-group/records",
                headers={"Authorization": f"Bearer {token}"})
    resp = run(scenario())
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "unknown_group"
    assert len(body["availableGroups"]) == 13
    assert body["availableGroups"][0] == "nlp-marine"   # catalogue order


@pytest.mark.parametrize("query,fragment", [
    ("limit=0", "limit"),
    ("limit=501", "limit"),
    ("limit=abc", "limit"),
    ("order=sideways", "order"),
    ("type=NOT-A-TYPE", "type"),
])
def test_bad_parameter_vocabulary(sim_state, query, fragment):
    async def scenario():
        async with await _http() as http:
            token = await _token(http)
            return await http.get(
                f"/v2/groups/customs/records?{query}",
                headers={"Authorization": f"Bearer {token}"})
    resp = run(scenario())
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_parameter"
    assert fragment in resp.json()["message"]


def test_raw_plus_offset_decodes_to_space_and_fails(sim_state):
    """The server-side face of defect D22: an unencoded '+05:30' arrives as
    ' 05:30' and is rejected — JNPA's own Postman collection would hit this
    the moment data exists in its query window."""
    async def scenario():
        async with await _http() as http:
            token = await _token(http)
            # Build the URL by hand so the '+' goes out RAW.
            return await http.get(
                "/v2/groups/customs/records?since=2026-07-30T00:00:00+05:30",
                headers={"Authorization": f"Bearer {token}"})
    resp = run(scenario())
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_parameter"


def test_ratelimit_header_only_on_success(sim_state):
    """Defect D5: RateLimit-Remaining present on 200s, absent on errors."""
    async def scenario():
        async with await _http() as http:
            token = await _token(http)
            ok = await http.get(
                "/v2/groups", headers={"Authorization": f"Bearer {token}"})
            bad = await http.get(
                "/v2/groups/nope/records",
                headers={"Authorization": f"Bearer {token}"})
            return ok, bad
    ok, bad = run(scenario())
    assert "RateLimit-Remaining" in ok.headers
    assert "RateLimit-Remaining" not in bad.headers


def test_429_carries_no_retry_after_and_no_ratelimit_header(sim_state):
    sim_state.force_429_remaining = 1

    async def scenario():
        async with await _http() as http:
            token = await _token(http)
            return await http.get(
                "/v2/groups", headers={"Authorization": f"Bearer {token}"})
    resp = run(scenario())
    assert resp.status_code == 429
    assert resp.json()["error"] == "rate_limited"
    assert "Retry-After" not in resp.headers
    assert "RateLimit-Remaining" not in resp.headers


def test_304_carries_etag_but_no_disposition_and_no_ratelimit(sim_state):
    """Defects D5/D27 in one response: the 304 is ETag-only."""
    async def scenario():
        async with await _http() as http:
            token = await _token(http)
            headers = {"Authorization": f"Bearer {token}"}
            page = await http.get("/v2/groups/customs/records?limit=1",
                                  headers=headers)
            item = page.json()["items"][0]
            sha = item["file"]["checksumSha256"]
            ref = item["file"]["fileRef"]
            return await http.get(
                f"/v2/files/{ref}",
                headers={**headers, "If-None-Match": f'"{sha}"'})
    resp = run(scenario())
    assert resp.status_code == 304
    assert resp.headers.get("ETag")
    assert "Content-Disposition" not in resp.headers
    assert "RateLimit-Remaining" not in resp.headers
    assert resp.content == b""


def test_record_and_file_ref_share_the_id_defect(sim_state):
    """Defect D2: fileRef == recordId with the prefix swapped, and the first
    base64 characters decode to a decimal integer."""
    import base64

    async def scenario():
        async with await _http() as http:
            token = await _token(http)
            page = await http.get(
                "/v2/groups/customs/records?limit=2",
                headers={"Authorization": f"Bearer {token}"})
            return page.json()["items"]
    items = run(scenario())
    for item in items:
        record_id = item["recordId"]
        file_ref = item["file"]["fileRef"]
        assert file_ref == "ref_" + record_id[len("rec_"):]
        prefix = record_id[len("rec_"):][:6]
        padded = prefix + "=" * (-len(prefix) % 4)
        decoded = base64.b64decode(padded).decode("ascii")
        assert decoded.isdigit()


def test_report_synthetic_toggle_serves_items(tmp_path):
    fresh_sim(build_fixture_corpus(tmp_path), report_items="synthetic")

    async def scenario():
        async with await _http() as http:
            token = await _token(http)
            # The live report endpoint returns the FULL set in one call and
            # ignores date/terminal request filters — so does the sim now.
            return await http.get(
                "/v2/groups/berthing-reports/records?limit=25",
                headers={"Authorization": f"Bearer {token}"})
    resp = run(scenario())
    body = resp.json()
    assert body["delivery"] == "report"
    assert body["count"] == 10              # 2 dates x 5 terminals, all returned
    assert len(body["items"]) == 10
    item = body["items"][0]
    assert item["reportType"] == "DAILY_BERTHING_REPORT"
    assert item["terminal"] in ("APMT", "NSICT", "NSIGT", "BMCT", "NSFT")
    assert isinstance(item.get("vesselCalls"), list) and item["vesselCalls"]
    assert "file" not in item               # report JSON, no file reference
    assert "hasMore" not in body            # 5-field envelope holds
    assert "nextCursor" not in body


def test_static_bathymetry_catalogued_but_empty(sim_state):
    async def scenario():
        async with await _http() as http:
            token = await _token(http)
            headers = {"Authorization": f"Bearer {token}"}
            catalogue = await http.get("/v2/groups", headers=headers)
            records = await http.get("/v2/groups/bathymetry/records",
                                     headers=headers)
            return catalogue.json(), records.json()
    catalogue, records = run(scenario())
    bathy = next(g for g in catalogue["groups"] if g["group"] == "bathymetry")
    assert bathy["delivery"] == "static"
    assert bathy["records"] == 0
    assert bathy["note"]
    assert "coverage" not in bathy          # non-uniform catalogue (D11)
    assert records["items"] == []
