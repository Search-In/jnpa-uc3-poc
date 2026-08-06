"""JNPA Port-Data API contract tests — the REAL client against the REAL sim
in-process (httpx.ASGITransport; no network, no DB).

This file is the executable half of the interface contract:

  * every assertion in JNPA's own Postman collection
    (Data/15-API Access/JNPA_DigitalTwin.postman_collection.json) is
    re-encoded as a pytest case — including the two that are mutually
    contradictory as shipped (D27/D28), kept as strict xfail so the defect
    stays visible in every run;
  * the operational hazards the docs under-specify are pinned: exclusive
    ``since`` over a non-unique sort key (boundary-tie skip, D13b), cursor
    round-trip (cursor == last fileRef, D13), 429 without Retry-After (D6),
    token expiry mid-pagination.

The sim seeds from a miniature corpus (tests/_jnpa_sim_fixtures.py) whose
customs group has 8 files => the sim's tie fixture collapses the last 4
onto one publishedAt.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest

from integrations.jnpa_portdata import (
    EXPECTED_GROUP_SLUGS,
    JnpaHTTPError,
    JnpaPortDataClient,
)

from _jnpa_sim_fixtures import (
    SIM_KEY,
    build_fixture_corpus,
    fresh_sim,
    sim_asgi_app,
)


@pytest.fixture()
def sim_state(tmp_path):
    data_dir = build_fixture_corpus(tmp_path)
    return fresh_sim(data_dir)


@pytest.fixture()
def client(sim_state):
    transport = httpx.ASGITransport(app=sim_asgi_app())
    http = httpx.AsyncClient(transport=transport, base_url="http://sim")
    return JnpaPortDataClient(
        "http://sim", client_key=SIM_KEY, http_client=http,
        retries=1, backoff_s=0.0,
        rate_limited_wait_s=0.01, rate_limited_jitter_s=0.0)


def run(coro):
    return asyncio.run(coro)


async def _raw(method: str, path: str, *, token: str = None, **kwargs):
    """Raw ASGI request for assertions the client (correctly) hides."""
    transport = httpx.ASGITransport(app=sim_asgi_app())
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://sim") as http:
        headers = kwargs.pop("headers", {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return await http.request(method, path, headers=headers, **kwargs)


async def _token() -> str:
    resp = await _raw("POST", "/v2/auth/token", json={"clientKey": SIM_KEY})
    return resp.json()["accessToken"]


# =====================================================================
# Postman folder 0 · Authentication
# =====================================================================
def test_pm_token_issued(client):
    """PM: 'token issued' — accessToken is a string."""
    info = run(client.get_token())
    assert isinstance(info.access_token, str) and info.access_token


def test_pm_rejects_unknown_client_key(sim_state):
    """PM: 'Rejects an unknown client key' — 401, nothing about why."""
    async def scenario():
        return await _raw("POST", "/v2/auth/token",
                          json={"clientKey": "not-a-real-key"})
    resp = run(scenario())
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized",
                           "message": "Client key not recognised"}


# =====================================================================
# Postman folder 1 · Discovery
# =====================================================================
def test_pm_health_ok(client):
    """PM: 'ok' — status == ok."""
    body = run(client.health())
    assert body["status"] == "ok"


def test_pm_groups_returned_and_stronger_all_13(client):
    """PM asserts only length > 0 (defect D33); we assert the full slug set."""
    envelope = run(client.list_groups())
    assert len(envelope.groups) > 0                       # the PM assertion
    assert {g.group for g in envelope.groups} == set(EXPECTED_GROUP_SLUGS)


def test_service_description_documents_the_surface(client):
    """PM has NO assertions on GET / (defect D33) — we pin the endpoint map."""
    body = run(client.service_description())
    assert body["endpoints"]["records"] == "/v2/groups/{group}/records"
    assert body["timeZone"] == "Asia/Kolkata"


# =====================================================================
# Postman folder 2 · Group records
# =====================================================================
def test_pm_indexed_groups_metadata_only_with_file_reference(client):
    """PM (x10 groups): 200 + every item carries a string file.fileRef."""
    async def scenario():
        out = {}
        for group in ("customs", "nlp-marine", "cfs-ecy", "transport"):
            page = await client.records_page(group, limit=25)
            for item in page.items:
                assert item.file is not None
                assert isinstance(item.file.fileRef, str) and item.file.fileRef
            out[group] = len(page.items)
        return out
    counts = run(scenario())
    assert counts["customs"] == 9          # 8 IGM + 1 OOC fixture files
    assert counts["nlp-marine"] == 3


def test_pm_bathymetry_200_static_empty(client):
    """PM: bathymetry asserts 200 only (defect D32/D12): catalogued but
    static — records endpoint answers, with nothing in it."""
    page = run(client.records_page("bathymetry"))
    assert page.delivery == "static"
    assert page.items == []


def test_pm_report_groups_json_no_file_reference(client):
    """PM (x2): 200 + no item has a 'file' property; and the envelope is the
    5-field variant (defect D9) — no order/matched/hasMore/nextCursor."""
    async def scenario():
        return (await client.get_report("berthing-reports",
                                        date_="2026-08-03", terminal="BMCT"),
                await client.get_report("daily-reports", date_="2026-08-03"))
    berthing, daily = run(scenario())
    for envelope in (berthing, daily):
        assert envelope.delivery == "report"
        assert all("file" not in item for item in envelope.items)
        assert envelope.order is None
        assert envelope.matched is None
        assert envelope.hasMore is None
        assert envelope.nextCursor is None


# =====================================================================
# Postman folder 3 · Files
# =====================================================================
def test_pm_download_by_file_reference_has_filename(client):
    """PM: '200 or 304' + 'has a filename' — on the 200 path both hold."""
    async def scenario():
        page = await client.records_page("customs", limit=1)
        record = page.items[0]
        return await client.fetch_file(record.file.fileRef,
                                       expected_sha256=record.file.checksumSha256)
    fetched = run(scenario())
    assert fetched.status == 200
    assert fetched.filename            # from Content-Disposition
    assert fetched.sha256 == fetched.etag


@pytest.mark.xfail(strict=True, reason=(
    "spec defect D27/D28: the Postman request never sends If-None-Match yet "
    "asserts '200 or 304' AND 'has a filename'; a real 304 carries no "
    "Content-Disposition, so the two assertions cannot both hold"))
def test_pm_304_would_fail_the_has_a_filename_assertion(client):
    async def scenario():
        page = await client.records_page("customs", limit=1)
        record = page.items[0]
        return await client.fetch_file(record.file.fileRef,
                                       etag=record.file.checksumSha256)
    fetched = run(scenario())
    assert fetched.status == 304
    assert fetched.filename is not None    # <- Postman's 'has a filename'


def test_pm_rejects_a_tampered_reference(sim_state):
    """PM: 'Rejects a tampered reference' — 404, fail closed."""
    async def scenario():
        token = await _token()
        return await _raw("GET", "/v2/files/ref_TAMPERED0000000000",
                          token=token)
    resp = run(scenario())
    assert resp.status_code == 404
    assert resp.json()["error"] == "invalid_reference"


# =====================================================================
# Postman folder 4 · Query patterns
# =====================================================================
def test_pm_latest_first_default_order(client):
    """PM: 'newest first' — publishedAt descending by default."""
    page = run(client.records_page("customs", limit=9))
    stamps = [i.publishedAt for i in page.items]
    assert stamps == sorted(stamps, reverse=True)


def test_pm_bounded_by_date_range_inclusive(client):
    """PM: 'inside the range' — from/to inclusive, bare date = whole day.
    (The shipped PM assertion passes vacuously on empty data, defect D30 —
    this fixture guarantees rows.)"""
    page = run(client.records_page("customs", from_="2026-07-09",
                                   to="2026-07-31", limit=50))
    assert page.items                       # NOT vacuous
    for item in page.items:
        assert "2026-07-09" <= item.publishedAt[:10] <= "2026-07-31"


def test_pm_incremental_read_with_since_is_exclusive(client):
    """PM: 'since is exclusive, so you never re-read the last record'."""
    async def scenario():
        full = [i async for i in client.iter_records("customs", limit=50)]
        last = full[-1].publishedAt
        again = await client.records_page("customs", since=last, order="asc",
                                          limit=50)
        return full, again
    full, again = run(scenario())
    assert full
    assert again.items == []                # nothing strictly after the last


def test_pm_page_with_the_cursor_verbatim_roundtrip(client):
    """PM: 'pass nextCursor back verbatim' — and the pages tile exactly."""
    async def scenario():
        first = await client.records_page("customs", order="asc", limit=4)
        assert first.hasMore and first.nextCursor
        # Faithful defect D13: the cursor IS the last item's fileRef.
        assert first.nextCursor == first.items[-1].file.fileRef
        second = await client.records_page("customs", order="asc", limit=4,
                                           cursor=first.nextCursor)
        return first, second
    first, second = run(scenario())
    ids = [i.recordId for i in first.items] + [i.recordId for i in second.items]
    assert len(ids) == len(set(ids))        # no boundary duplication
    assert len(ids) == 8


def test_fabricated_cursor_earns_bad_cursor(client):
    with pytest.raises(JnpaHTTPError) as excinfo:
        run(client.records_page("customs", cursor="ref_FABRICATED000000"))
    assert excinfo.value.status_code == 400
    assert excinfo.value.is_bad_cursor


# =====================================================================
# Hazards beyond the Postman collection
# =====================================================================
def test_boundary_tie_skip_hazard_and_the_minus_1s_defense(client, sim_state):
    """Defect D13b: 4 customs records share one publishedAt. Resuming with
    since=<that timestamp> (exclusive) SKIPS all four; the sync layer's
    since=watermark-1s + recordId dedup recovers them exactly once."""
    async def scenario():
        full = [i async for i in client.iter_records("customs", limit=50)]
        tie_stamp = full[-1].publishedAt
        tied = [i for i in full if i.publishedAt == tie_stamp]
        naive = await client.records_page("customs", since=tie_stamp,
                                          order="asc", limit=50)
        from datetime import datetime
        rewound = (datetime.fromisoformat(tie_stamp)
                   - timedelta(seconds=1)).isoformat()
        defended = await client.records_page("customs", since=rewound,
                                             order="asc", limit=50)
        return tied, naive, defended
    tied, naive, defended = run(scenario())
    assert len(tied) == 4                   # the sim's tie fixture
    assert naive.items == []                # the skip hazard is REAL
    got = {i.recordId for i in defended.items}
    assert {i.recordId for i in tied} <= got   # -1s recovers every tied record


def test_429_no_retry_after_blind_backoff_recovers(client, sim_state):
    sim_state.force_429_remaining = 1
    envelope = run(client.list_groups())
    assert len(envelope.groups) == 13
    stats = client.request_stats()
    assert stats.http_429_count == 1


def test_token_expiry_mid_pagination_single_reauth(tmp_path):
    """A 1-second bearer dies between pages; the client re-auths ONCE and
    the walk completes with no duplicates."""
    data_dir = build_fixture_corpus(tmp_path)
    fresh_sim(data_dir, token_ttl_s=1.0)
    transport = httpx.ASGITransport(app=sim_asgi_app())
    http = httpx.AsyncClient(transport=transport, base_url="http://sim")
    client = JnpaPortDataClient(
        "http://sim", client_key=SIM_KEY, http_client=http,
        retries=2, backoff_s=0.0,
        token_refresh_margin_s=0.0)         # no proactive refresh: force 401

    async def scenario():
        first = await client.records_page("customs", order="asc", limit=4)
        await asyncio.sleep(1.1)            # the bearer dies here
        second = await client.records_page("customs", order="asc", limit=4,
                                           cursor=first.nextCursor)
        return first, second

    first, second = run(scenario())
    ids = [i.recordId for i in first.items] + [i.recordId for i in second.items]
    assert len(ids) == 8 and len(set(ids)) == 8


def test_future_range_returns_empty_not_error(client):
    """'A future range returns an empty result, not an error.'"""
    page = run(client.records_page("customs", from_="2030-01-01", limit=5))
    assert page.items == [] and page.matched == 0 and page.hasMore is False


def test_checksum_roundtrip_against_real_bytes(client):
    """The record checksum, the ETag and the downloaded bytes agree — the
    dump-vs-API dedup key is trustworthy end to end."""
    async def scenario():
        page = await client.records_page("customs", limit=3)
        out = []
        for record in page.items:
            fetched = await client.fetch_file(
                record.file.fileRef,
                expected_sha256=record.file.checksumSha256)
            out.append((record.file.checksumSha256, fetched.sha256,
                        fetched.etag, len(fetched.content),
                        record.file.sizeBytes))
        return out
    for record_sha, body_sha, etag, size, declared in run(scenario()):
        assert record_sha == body_sha == etag
        assert size == declared
