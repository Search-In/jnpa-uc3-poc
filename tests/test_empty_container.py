"""Tests for the empty-container supply-demand optimiser (Appendix C #3).

These exercise the *pure* functions in ``empty_container.seed`` and
``empty_container.optimizer`` plus the shared KPI engine, so no server or infra
is required — they stay green with only fastapi + pydantic + jnpa_shared
installed (the repo .venv). A FastAPI TestClient smoke test runs additionally
*iff* httpx is importable; it is skipped otherwise.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT / "empty-container")):
    if p not in sys.path:
        sys.path.insert(0, p)

from empty_container import optimizer, seed  # noqa: E402
from jnpa_shared.kpi import compute_kpi  # noqa: E402


# --- books ------------------------------------------------------------------

def test_books_build_and_have_stock():
    supply = seed.supply_book()
    demand = seed.demand_book(40)
    assert len(supply) == seed.DEFAULT_SUPPLY_DEPOTS
    assert {d.kind for d in supply} == {"ECD", "CFS"}
    assert all(dep.total_stock() > 0 for dep in supply)
    assert len(demand) == 40
    # Each container type appears in stock for every depot.
    for dep in supply:
        assert set(dep.stock) == set(seed.CONTAINER_TYPES)


# --- (a) one allocation per satisfiable demand ------------------------------

def test_allocation_per_satisfiable_demand():
    supply = seed.supply_book()
    demand = seed.demand_book(40)
    allocs = optimizer.allocate(supply, demand)

    # No more allocations than demands, and never more than one per demand id.
    assert len(allocs) <= len(demand)
    ids = [a.demand_id for a in allocs]
    assert len(ids) == len(set(ids))  # one allocation per demand at most

    # Every allocation references a real depot and a demand from the book.
    depot_ids = {d.depot_id for d in supply}
    demand_ids = {d.demand_id for d in demand}
    for a in allocs:
        assert a.supply_depot in depot_ids
        assert a.demand_id in demand_ids
        assert a.container_type in seed.CONTAINER_TYPES
        assert a.distance_km >= 0.0
        assert a.est_trt_min > 0.0
        assert 0.0 <= a.confidence <= 1.0

    # With ample dry-box stock, every satisfiable demand should be served.
    # A demand is satisfiable iff some depot holds its container type at all.
    satisfiable = [
        d for d in demand
        if any(dep.stock.get(d.container_type, 0) > 0 for dep in supply)
    ]
    # Greedy stock-aware match: all satisfiable demands get exactly one alloc
    # (stock is deep enough that no dry type is exhausted in the 40-record book).
    assert len(allocs) == len(satisfiable)
    assert {a.demand_id for a in allocs} == {d.demand_id for d in satisfiable}


# --- (b) determinism across two runs ----------------------------------------

def test_allocations_deterministic_across_runs():
    a1 = optimizer.allocate(seed.supply_book(), seed.demand_book(40))
    a2 = optimizer.allocate(seed.supply_book(), seed.demand_book(40))
    assert [x.to_dict() for x in a1] == [x.to_dict() for x in a2]

    # Books themselves are deterministic too.
    assert [seed.demand_to_dict(d) for d in seed.demand_book(40)] == \
           [seed.demand_to_dict(d) for d in seed.demand_book(40)]
    assert [seed.depot_to_dict(d) for d in seed.supply_book()] == \
           [seed.depot_to_dict(d) for d in seed.supply_book()]


# --- (c) cargo variants are handled -----------------------------------------

def test_cargo_variants_handled():
    supply = seed.supply_book()
    demand = seed.demand_book(40)
    cargo_in_demand = {d.cargo_type for d in demand}
    for variant in ("oil_tanker", "break_bulk", "cement_bowser", "container"):
        assert variant in cargo_in_demand, f"missing cargo variant in demand: {variant}"

    allocs = optimizer.allocate(supply, demand)
    allocated_variants = {a.cargo_type for a in allocs}
    for variant in ("oil_tanker", "break_bulk", "cement_bowser"):
        assert variant in allocated_variants, f"variant not allocated: {variant}"

    # Synthetic injection also handles each variant deterministically.
    for variant in ("oil_tanker", "break_bulk", "cement_bowser"):
        d = seed.synthetic_demand(0, cargo_type=variant)
        assert d.cargo_type == variant
        a = optimizer.allocate(supply, [d])
        assert len(a) == 1
        assert a[0].cargo_type == variant

    with pytest.raises(ValueError):
        seed.synthetic_demand(0, cargo_type="not_a_cargo")


# --- (d) est_trt feeds compute_kpi ------------------------------------------

def test_est_trt_feeds_kpi():
    allocs = optimizer.allocate(seed.supply_book(), seed.demand_book(40))
    value = optimizer.mean_est_trt(allocs)
    assert value > 0.0

    result = compute_kpi("trt_empty_ecd", value)
    d = result.to_dict()
    assert d["key"] == "trt_empty_ecd"
    assert "deltaPct" in d
    assert "onTarget" in d
    assert isinstance(d["onTarget"], bool)
    assert isinstance(d["deltaPct"], (int, float))
    assert d["value"] == pytest.approx(round(value, 2), abs=0.01)


def test_mean_est_trt_empty_is_zero():
    assert optimizer.mean_est_trt([]) == 0.0


# --- optional FastAPI TestClient smoke test (skipped without httpx) ---------

def test_endpoints_smoke():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from empty_container.app import app

    with TestClient(app) as client:
        h = client.get("/healthz").json()
        assert h["status"] == "ok"
        assert h["depots"] > 0 and h["demand"] > 0

        allocs = client.get("/allocations").json()
        assert allocs["count"] >= 1
        assert "unsatisfied" in allocs

        kpi = client.get("/kpi/trt_empty").json()
        assert kpi["key"] == "trt_empty_ecd"
        assert "deltaPct" in kpi and "onTarget" in kpi

        before = client.get("/demand").json()["count"]
        inj = client.post("/demand/inject", json={"cargo_type": "oil_tanker",
                                                   "priority": "high"}).json()
        assert inj["injected"] is True
        assert client.get("/demand").json()["count"] == before + 1
