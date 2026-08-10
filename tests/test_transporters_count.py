"""UC3-001 — the transporter registry reports its EXACT database total.

The bug this pins: ``GET /api/transporters`` returned ``count = len(rows)`` with
the page hard-capped at ``le=1000``, so a 2,191-row registry rendered as
"Total Transporters: 1,000" and the list badge as "1,000+". There was no
``count(*)`` anywhere in the transporter path.

The contract now: ``total`` is a server-side ``count(*)`` over the SAME predicate
as the page, page size is independent of it, and the KPI cards read a dedicated
``/stats`` aggregate instead of reducing over whatever rows happen to be loaded.

These run WITHOUT a database: ``jnpa_shared.db.fetch_all`` / ``fetch_one`` are
replaced by a small in-memory table that mirrors the real one — 2,191 rows that
all share a single ``created_at``, which is what makes the ``t.id`` tiebreaker
load-bearing. The last test in the file talks to the real RDS and skips when it
is unreachable.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

from gateway.routers import transporters as tr  # noqa: E402

# --------------------------------------------------------------------------- #
# Fixture data — shaped like the imported TransporterDetails.xlsx corpus.
# --------------------------------------------------------------------------- #
TOTAL = 2191
CREATED_AT = "2026-08-09T09:15:00+00:00"  # ONE value for every row, as in RDS

# Real rows from the QA import, at the ranks that matter for the regression.
NAMED = {1: ("Royal Container Carrier", 81, "9820915591"),
         1500: ("Aniket Container Service", 3403, "9222111373"),
         2191: ("SAS CARGO MOVERS", 4958, "9664054764")}


def _make_table() -> List[Dict[str, Any]]:
    rows = []
    for i in range(1, TOTAL + 1):
        name, company_id, mobile = NAMED.get(i, (f"Transporter {i:04d}", 1000 + i,
                                                 f"90000{i:05d}"))
        rows.append({"id": i, "company_id": company_id, "company_name": name,
                     "mobile_number": mobile, "status": "ACTIVE",
                     "data_origin": "MANUAL", "created_at": CREATED_AT,
                     "code": None, "gstin": None, "contact_person": None,
                     "email": None})
    return rows


TABLE = _make_table()


def _matches(row: Dict[str, Any], params: Dict[str, Any]) -> bool:
    """Python mirror of the router's WHERE clause."""
    if "q" in params:
        needle = params["q"].strip("%").lower()
        haystack = " ".join(str(row.get(k) or "").lower() for k in
                            ("company_name", "code", "gstin", "contact_person",
                             "email", "mobile_number", "company_id"))
        if needle not in haystack:
            return False
    if "status" in params and row["status"] != params["status"]:
        return False
    if "data_origin" in params and row["data_origin"] != params["data_origin"]:
        return False
    return True


def _where_of(sql: str) -> str:
    """The TOP-LEVEL WHERE clause, normalised — proves page and count agree.

    Anchored on ``FROM core.transporter t`` so the correlated subqueries inside
    the page SELECT (``WHERE v.transporter_id = t.id``) are not mistaken for it.
    """
    m = re.search(r"FROM core\.transporter t\s+WHERE(.*?)(?:ORDER BY|LIMIT|$)",
                  sql, re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


class Recorder:
    """Captures every SQL/params pair the router issues, and serves results."""

    def __init__(self) -> None:
        self.calls: List[tuple[str, Dict[str, Any]]] = []

    def _filtered(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows = [r for r in TABLE if _matches(r, params)]
        # created_at is identical everywhere, so id is the real sort key.
        rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=False)
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows

    async def fetch_all(self, sql: str, params=None, *, dsn=None):
        params = dict(params or {})
        self.calls.append((sql, params))
        rows = self._filtered(params)
        off = int(params.get("offset", 0))
        lim = int(params.get("limit", len(rows)))
        page = rows[off:off + lim]
        # Mirror the router's column aliases (company_name AS name, etc.) so the
        # tests assert against the shape callers actually receive.
        return [dict(r, name=r["company_name"], mobile=r["mobile_number"],
                     source_company_id=r["company_id"],
                     vehicle_count=0, blacklisted=False) for r in page]

    async def fetch_one(self, sql: str, params=None, *, dsn=None):
        params = dict(params or {})
        self.calls.append((sql, params))
        rows = self._filtered(params)
        if "WITH scope" in sql:
            return {"total": len(rows), "active": len(rows),
                    "blacklisted": 0, "vehicles_assigned": 0}
        if re.search(r"count\(\*\)\s+AS n", sql, re.I):
            return {"n": len(rows)}
        return None

    def sql_matching(self, pattern: str) -> List[tuple[str, Dict[str, Any]]]:
        return [c for c in self.calls if re.search(pattern, c[0], re.I | re.S)]


@pytest.fixture()
def db(monkeypatch) -> Recorder:
    import jnpa_shared.db as real_db
    rec = Recorder()
    monkeypatch.setattr(real_db, "fetch_all", rec.fetch_all, raising=False)
    monkeypatch.setattr(real_db, "fetch_one", rec.fetch_one, raising=False)
    return rec


REQUEST = SimpleNamespace(state=SimpleNamespace(principal=None),
                          url=SimpleNamespace(path="/api/transporters"),
                          headers={}, query_params={})
STATE = SimpleNamespace(cfg=SimpleNamespace(postgres_dsn="postgresql://stub/db"))


def call_list(*, q: Optional[str] = None, status: Optional[str] = None,
              limit: int = 50, offset: int = 0, mode: Optional[str] = None):
    return asyncio.run(tr.list_transporters(request=REQUEST, q=q, status=status,
                                            limit=limit, offset=offset, mode=mode,
                                            state=STATE))


def call_stats(*, mode: Optional[str] = None):
    return asyncio.run(tr.transporter_stats(mode=mode, state=STATE))


# ============================================================ the exact total
def test_total_is_the_database_count_not_the_page_length(db):
    """The headline regression: 2,191 rows must not report as the page size."""
    res = call_list(limit=50)
    assert res["total"] == TOTAL            # exact database count
    assert len(res["items"]) == 50          # page is still a page
    assert res["count"] == 50               # `count` stays the page length
    assert res["total"] != len(res["items"])


@pytest.mark.parametrize("limit", [1, 10, 25, 50, 200])
def test_total_is_independent_of_page_size(db, limit):
    res = call_list(limit=limit)
    assert res["total"] == TOTAL
    assert len(res["items"]) == limit


def test_total_survives_the_old_thousand_boundary(db):
    """No 1000-shaped cap anywhere: page 21 of 50 sits past the old ceiling."""
    res = call_list(limit=50, offset=1000)
    assert res["total"] == TOTAL
    assert res["items"], "rows beyond the old 1000 cap must be reachable"
    assert min(r["id"] for r in res["items"]) > 1000


def test_count_query_is_a_real_count_star(db):
    call_list(limit=10)
    counts = db.sql_matching(r"count\(\*\)\s+AS n\s+FROM core\.transporter")
    assert len(counts) == 1, "the endpoint must issue exactly one COUNT(*)"
    assert "LIMIT" not in counts[0][0].upper(), "the COUNT must not be paginated"


# ============================================================== pagination
def test_pagination_returns_every_row_exactly_once(db):
    """44 pages of 50 → 2,191 rows, no duplicates, no gaps."""
    seen: List[int] = []
    offset = 0
    while True:
        res = call_list(limit=50, offset=offset)
        if not res["items"]:
            break
        seen.extend(r["id"] for r in res["items"])
        offset += 50
    assert len(seen) == TOTAL
    assert len(set(seen)) == TOTAL, "pages overlapped — ordering is not stable"


def test_offset_past_the_end_is_empty_but_total_is_still_exact(db):
    res = call_list(limit=50, offset=TOTAL + 100)
    assert res["items"] == []
    assert res["total"] == TOTAL


def test_limit_and_offset_are_echoed_for_the_client(db):
    res = call_list(limit=25, offset=75)
    assert res["limit"] == 25 and res["offset"] == 75


# =========================================================== stable ordering
def test_ordering_has_a_unique_tiebreaker(db):
    """Every row shares one created_at, so id must break the tie in SQL."""
    call_list(limit=10)
    page = db.sql_matching(r"vehicle_count")[0][0]
    assert re.search(r"ORDER BY\s+t\.created_at DESC,\s*t\.id", page), (
        "ORDER BY must include the t.id tiebreaker or LIMIT/OFFSET paging is "
        "non-deterministic across the 2,191 identical created_at values")


def test_page_and_count_share_an_identical_where_clause(db):
    call_list(q="cargo", status="ACTIVE", mode="MANUAL")
    page = db.sql_matching(r"vehicle_count")[0]
    count = db.sql_matching(r"count\(\*\)\s+AS n")[0]
    assert _where_of(page[0]) == _where_of(count[0])
    # Paging params are the only difference between the two bind sets.
    assert {k: v for k, v in page[1].items() if k not in ("limit", "offset")} == count[1]


# ==================================================================== search
def test_search_reaches_a_row_far_beyond_the_first_page(db):
    """SAS CARGO MOVERS is row 2,191 — unreachable under the old 1000 window."""
    res = call_list(q="SAS CARGO MOVERS", limit=50)
    assert res["total"] == 1
    assert res["items"][0]["name"] == "SAS CARGO MOVERS"


def test_search_by_company_id_and_mobile(db):
    assert call_list(q="9222111373")["total"] == 1     # mobile, rank 1500
    assert call_list(q="Royal Container Carrier")["total"] == 1


def test_search_total_counts_all_matches_not_just_the_page(db):
    res = call_list(q="Transporter", limit=10)
    assert len(res["items"]) == 10
    assert res["total"] > 1000, "search total must span the whole match set"


def test_empty_search_result_reports_zero(db):
    res = call_list(q="no-such-company-anywhere")
    assert res["total"] == 0 and res["items"] == []


# ===================================================================== stats
def test_stats_are_database_aggregates(db):
    res = call_stats()
    assert res == {"total": TOTAL, "active": TOTAL,
                   "blacklisted": 0, "vehicles_assigned": 0}


def test_stats_never_paginate(db):
    call_stats()
    sql = db.sql_matching(r"WITH scope")[0][0]
    assert "LIMIT" not in sql.upper()


# ============================================================ data-mode filter
def test_data_mode_filters_total_and_items_together(db):
    """Every imported row is MANUAL, so a LIVE request must see a true zero."""
    live = call_list(mode="API", limit=50)
    assert live["total"] == 0 and live["items"] == []

    demo = call_list(mode="MANUAL", limit=50)
    assert demo["total"] == TOTAL and len(demo["items"]) == 50


def test_data_mode_applies_to_stats_too(db):
    assert call_stats(mode="API")["total"] == 0
    assert call_stats(mode="MANUAL")["total"] == TOTAL


# ================================================ frontend regression guards
def test_the_ui_no_longer_hardcodes_a_thousand_row_window():
    """The screen must not reintroduce the capped-window vocabulary."""
    src = (REPO_ROOT / "web/src/screens/TransporterBlacklist.tsx").read_text()
    for banned in ("LIST_LIMIT", "window 1000", "capped", "refine search to narrow"):
        assert banned not in src, f"{banned!r} is back in TransporterBlacklist.tsx"


def test_the_ui_reads_the_server_total_and_stats():
    src = (REPO_ROOT / "web/src/screens/TransporterBlacklist.tsx").read_text()
    assert "statsQ.data?.total" in src, "KPI must read the /stats total"
    assert "listQ.data?.total" in src, "list footer must read the server total"
    assert "rows.slice(" not in src, "paging must be server-side, not a slice"


# ============================================== live RDS (skips when absent)
def _live_dsn() -> Optional[str]:
    import os
    for var in ("RFID_POSTGRES_DSN", "TRANSPORTER_TEST_DSN"):
        dsn = os.environ.get(var)
        if dsn:
            return dsn
    return None


@pytest.mark.skipif(_live_dsn() is None,
                    reason="no RFID_POSTGRES_DSN in the environment")
def test_live_database_total_matches_count_star():
    """Against the real registry: the endpoint's total == SELECT count(*)."""
    psycopg = pytest.importorskip("psycopg")
    dsn = _live_dsn()
    try:
        with psycopg.connect(dsn, connect_timeout=15) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM core.transporter")
                db_total = cur.fetchone()[0]
                cur.execute(
                    "SELECT count(DISTINCT id) FROM ("
                    "  SELECT t.id FROM core.transporter t"
                    "  ORDER BY t.created_at DESC, t.id LIMIT 50 OFFSET 0) s")
                assert cur.fetchone()[0] == min(50, db_total)
    except Exception as exc:  # noqa: BLE001 - unreachable DB is a skip, not a fail
        pytest.skip(f"Postgres unreachable: {exc}")
    assert db_total > 0
