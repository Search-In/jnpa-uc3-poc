"""GAP-API-04 — a date window on /api/kpi/{view} must reach a view that can answer it.

`mart.v_gate_trip_timeline` and the three KPIs derived from it bake
`WHERE ts > now() - interval '24 hours'` into the view body. A June or July
request against those returns zero rows for events that are sitting in
`core.gate_event` — measured on RDS 17-Aug, the June window gave 0 TAT buckets
from the pinned view and 80 from the unpinned twin over the same 120 gate trips.

These cases pin the routing decision (which view answers, and whether the caller
is told the window was applied). They use in-memory fakes — no DB — so they run
anywhere; the RDS numbers above are recorded in 07_BUILD_LOG.md.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from gateway.datewindow import DateWindow  # noqa: E402
from gateway.routers import kpi  # noqa: E402


@pytest.fixture
def captured(monkeypatch):
    """Record what _read_view was asked for instead of hitting a database."""
    seen: dict = {}

    async def fake_read_view(state, view_sql, limit=500, *, where="", params=None):
        seen.update(view=view_sql, where=where, params=params or {})
        return []

    monkeypatch.setattr(kpi, "_read_view", fake_read_view)
    return seen


_STATE = SimpleNamespace(cfg=SimpleNamespace(postgres_dsn="postgresql+asyncpg://x:x@127.0.0.1:1/none"))


def _call(view: str, window: DateWindow) -> dict:
    return asyncio.run(kpi.kpi_view(view, window=window, state=_STATE))


def test_no_window_keeps_the_live_pinned_view(captured):
    """The control-room boards must not change behaviour."""
    out = _call("gate_trip_timeline", DateWindow(None, None))
    assert captured["view"] == "mart.v_gate_trip_timeline"
    assert captured["where"] == ""
    assert out["window_applied"] is False


def test_a_window_switches_to_the_unpinned_twin(captured):
    out = _call("tat_inside_port", DateWindow(date(2026, 6, 1), date(2026, 6, 30)))
    assert captured["view"] == "mart.v_tat_inside_port_all"
    assert "bucket" in captured["where"]
    assert out["window_applied"] is True
    assert out["source_view"] == "mart.v_tat_inside_port_all"


def test_timeline_windows_on_coalesce_not_arrival_alone(captured):
    """June records only GATE_IN/GATE_OUT — no GATE_ARRIVAL exists before July.

    Keying the window on `arrival_ts` would hide every June trip for a second
    time, which is the same failure the ticket is about.
    """
    _call("gate_trip_timeline", DateWindow(date(2026, 6, 1), date(2026, 6, 30)))
    assert "COALESCE" in captured["where"]
    assert "gate_in_ts" in captured["where"]


def test_window_bounds_are_bound_parameters_not_inlined(captured):
    _call("tat_inside_port", DateWindow(date(2026, 6, 1), date(2026, 6, 30)))
    assert "2026-06-01" not in captured["where"]
    assert captured["params"], "window bounds must be bound, not interpolated"


def test_a_view_with_no_timestamp_says_so_rather_than_pretending(captured):
    """Silently ignoring the window would return an unfiltered result that reads
    as filtered — worse than refusing, because nothing on screen would differ."""
    out = _call("alerts_by_kind", DateWindow(date(2026, 6, 1), date(2026, 6, 30)))
    assert out["window_applied"] is False
    assert out["window_note"] and "NOT applied" in out["window_note"]
    assert captured["where"] == ""


def test_every_windowed_view_is_a_known_kpi_view():
    """The twin map must not drift from the view whitelist."""
    assert set(kpi._WINDOWED_VIEWS) <= set(kpi.KPI_VIEWS)
    assert set(kpi._UNWINDOWABLE_VIEWS) <= set(kpi.KPI_VIEWS)


def test_every_kpi_view_is_either_windowable_or_explicitly_not():
    """No view may fall through silently: each is in exactly one of the maps."""
    windowed, unwindowable = set(kpi._WINDOWED_VIEWS), set(kpi._UNWINDOWABLE_VIEWS)
    assert not (windowed & unwindowable), "a view cannot be both"
    assert set(kpi.KPI_VIEWS) == windowed | unwindowable


def test_unknown_view_still_404s():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _call("not_a_view", DateWindow(None, None))
    assert exc.value.status_code == 404
