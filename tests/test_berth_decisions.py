"""GAP-FLOW-15 — the berth allocation decision log.

Flow F-15 asks that re-ordering the berth queue record four things: which call
moved, why, on whose authority and when. The planning panel previously produced
a proposal, said "a planner accepts or edits", and recorded none of them.

These cases pin the properties that make the log worth having. They use
in-memory fakes — no DB — so they run anywhere; the RDS verification is in
07_BUILD_LOG.md.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from fastapi import HTTPException  # noqa: E402

from gateway.routers import berth_decisions as bd  # noqa: E402

_STATE = SimpleNamespace(cfg=SimpleNamespace(postgres_dsn="postgresql+asyncpg://x:x@127.0.0.1:1/none"))

_CODES = [
    {"code": "TIDE_WINDOW", "label": "Tide window", "category": "PHYSICAL"},
    {"code": "OPTIMISER_ACCEPTED", "label": "Accepted the proposal", "category": "SYSTEM"},
]


def _req(sub=None, role=None):
    principal = SimpleNamespace(sub=sub, role=role) if sub else None
    return SimpleNamespace(state=SimpleNamespace(principal=principal))


@pytest.fixture
def captured(monkeypatch):
    seen: dict = {}

    async def fake_codes(state):
        return {"reason_codes": _CODES, "count": len(_CODES)}

    async def fake_write(sql, params=None, *, dsn=None):
        seen.update(sql=sql, params=params or {})
        return {"decision_id": 42, "decided_at": "2026-08-17T10:00:00+05:30"}

    monkeypatch.setattr(bd, "reason_codes", fake_codes)
    import jnpa_shared.db as db
    monkeypatch.setattr(db, "execute_returning", fake_write)
    return seen


def _record(body, request):
    return asyncio.run(bd.record_decision(body, request=request, state=_STATE))


def test_records_who_decided_from_the_authenticated_principal(captured):
    out = _record(
        bd.BerthDecisionIn(call_id="INNSA1NS0S0552", reason_code="TIDE_WINDOW"),
        _req("planner.a", "TERMINAL_OPS"),
    )
    assert out["recorded"] is True
    assert captured["params"]["actor"] == "planner.a"
    assert captured["params"]["actor_role"] == "TERMINAL_OPS"


def test_an_unauthenticated_decision_is_marked_not_blanked(captured):
    """The open demo profile has no principal. Recording NULL would make an
    unattributed decision look like a missing field rather than an unattributed
    decision."""
    _record(bd.BerthDecisionIn(call_id="X", reason_code="TIDE_WINDOW"), _req())
    assert captured["params"]["actor"] == "unauthenticated"


def test_an_unknown_reason_code_is_rejected(captured):
    """A log of free-text reasons cannot be counted, and 'why did we re-order
    berths this month' is the question it exists to answer."""
    with pytest.raises(HTTPException) as exc:
        _record(bd.BerthDecisionIn(call_id="X", reason_code="BECAUSE"), _req("a", "TERMINAL_OPS"))
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "unknown_reason_code"


def test_accepting_the_optimiser_is_itself_a_decision(captured):
    """Not an afterthought: a log that captured only overrides would make the
    optimiser look unused rather than trusted, and leave the accepted plan with
    no author."""
    _record(
        bd.BerthDecisionIn(call_id="INNSA1NS0S0552", reason_code="OPTIMISER_ACCEPTED"),
        _req("planner.b", "JNPA_TRAFFIC"),
    )
    assert captured["params"]["reason_code"] == "OPTIMISER_ACCEPTED"


def test_the_write_commits(captured):
    """It must go through execute_returning, not fetch_all.

    fetch_all opens a non-transactional connection: an INSERT through it hands
    back the new id and is rolled back on close, so the endpoint would report
    `recorded: true` for a write that never happened. That is exactly what this
    endpoint did before it was corrected.
    """
    src = Path(bd.__file__).read_text()
    assert "execute_returning" in src
    assert "fetch_all(sql, params" not in src.split("async def record_decision")[1]


def test_the_log_is_append_only():
    """No PUT, no DELETE. Superseding a decision means appending another — the
    value of the log is that it says what was believed at the time."""
    methods = {m for r in bd.router.routes for m in getattr(r, "methods", set())}
    assert methods <= {"GET", "POST", "HEAD", "OPTIONS"}, methods
