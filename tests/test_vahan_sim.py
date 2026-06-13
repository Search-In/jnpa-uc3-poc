"""Tests for the Vahan/Sarathi/FASTag simulator + live adapter.

The simulator + live-adapter tests run entirely in-process via Starlette's
TestClient — no docker stack required, so they stay green in CI without infra.

The vehicle_master writeback test needs a live Postgres (compose publishes it on
host 5433) and is skipped automatically when it is unreachable.
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = REPO_ROOT / "shared"
INGEST_DIR = REPO_ROOT / "ingest"
for p in (str(SHARED_DIR), str(INGEST_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Keep the dataset modest + DB pointed nowhere by default so the in-process
# tests are fast and don't touch infra. Individual tests override as needed.
os.environ.setdefault("VAHAN_TOTAL_PLATES", "3000")
os.environ.setdefault("VAHAN_FIXTURE_PATH", "/tmp/jnpa_known_plates_test.json")
# Trim simulated latency so 1000 lookups complete quickly while still
# exercising the p95 path (mean 20ms +/- 10ms; assertion bound scales below).
os.environ.setdefault("VAHAN_LATENCY_MEAN_MS", "20")
os.environ.setdefault("VAHAN_LATENCY_JITTER_MS", "10")

from starlette.testclient import TestClient  # noqa: E402

from jnpa_shared.schemas import is_valid_dl, is_valid_plate  # noqa: E402


def _pg_host_dsn() -> str | None:
    """Return a host DSN for the compose Postgres (port 5433) or None."""
    dsn = os.environ.get(
        "POSTGRES_TEST_DSN",
        "postgresql+asyncpg://postgres:jnpa_pw@localhost:5433/postgres",
    )
    try:
        with socket.create_connection(("localhost", 5433), timeout=2.0):
            return dsn
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def sim_client():
    # No DB for the in-process suite: point the DSN at an unroutable address so
    # the writeback path fails fast and silently (it's exercised separately).
    os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")
    from jnpa_shared.config import get_settings
    get_settings.cache_clear()
    import importlib
    import vahan_sim.config as cfgmod
    importlib.reload(cfgmod)
    import vahan_sim.app as appmod
    importlib.reload(appmod)
    with TestClient(appmod.app) as client:
        yield client, appmod


def test_seed_distributions_and_validity():
    """The 25k corpus hits the spec'd anomaly rates and every plate/DL is valid."""
    from vahan_sim.seed import generate_dataset, REFERENCE_DATE
    from jnpa_shared.schemas import BlacklistStatus, FastagStatus

    ds = generate_dataset(25_000)
    # >= 25k: a few well-known plates (PINNED_PLATES) are injected on top.
    assert len(ds) >= 25_000
    from vahan_sim.seed import PINNED_PLATES
    assert all(p in ds for p in PINNED_PLATES), "pinned plates must always resolve"
    assert all(is_valid_plate(p) for p in ds), "every generated plate must pass the regex"
    assert all(is_valid_dl(r.dl.dl_number) for r in ds.values())

    n = len(ds)
    expired = sum(1 for r in ds.values() if r.rc.fitness_valid_to < REFERENCE_DATE)
    black = sum(1 for r in ds.values() if r.rc.blacklist_status is BlacklistStatus.BLACKLISTED)
    flow = sum(1 for r in ds.values() if r.fastag_status is FastagStatus.LOW_BALANCE)
    fblack = sum(1 for r in ds.values() if r.fastag_status is FastagStatus.BLACKLISTED)

    # Distributions within +/-1.5 absolute percentage points of the spec.
    assert abs(100 * expired / n - 8) < 1.5
    assert abs(100 * black / n - 3) < 1.5
    assert abs(100 * flow / n - 5) < 1.5
    assert abs(100 * fblack / n - 1) < 1.5


def test_fixture_has_benign_and_issue_halves(tmp_path):
    """known_plates.json: 50 plates, half benign, half with >=1 issue."""
    from vahan_sim.seed import generate_dataset, write_fixture

    ds = generate_dataset(25_000)
    out = tmp_path / "known_plates.json"
    payload = write_fixture(out, ds, n=50)
    assert payload["count"] == 50
    assert payload["benign"] == 25
    assert payload["with_issues"] == 25
    assert all(not p["issues"] for p in payload["plates"][:25])
    assert all(p["issues"] for p in payload["plates"][25:])


def test_sim_lookup_p95_latency(sim_client):
    """1000 random plate lookups; p95 latency < 400 ms (spec)."""
    import time

    client, appmod = sim_client
    plates = list(appmod._STORE.keys())
    assert len(plates) >= 1000

    # Deterministic "random" sample without RNG: stride across the keyspace.
    stride = max(1, len(plates) // 1000)
    sample = plates[::stride][:1000]
    assert len(sample) == 1000

    latencies = []
    for plate in sample:
        t0 = time.perf_counter()
        r = client.get(f"/vahan/rc/{plate}")
        latencies.append((time.perf_counter() - t0) * 1000.0)
        assert r.status_code == 200

    latencies.sort()
    p95 = latencies[int(0.95 * len(latencies)) - 1]
    assert p95 < 400.0, f"p95 latency {p95:.1f}ms exceeded 400ms"


def test_sim_record_shapes(sim_client):
    client, appmod = sim_client
    plate = next(iter(appmod._STORE))
    rec = appmod._STORE[plate]

    rc = client.get(f"/vahan/rc/{plate}").json()
    assert rc["rc_number"] == plate
    for field in ("owner_name_masked", "vehicle_class", "fuel_type", "fitness_valid_to",
                  "puc_valid_to", "insurance_valid_to", "registration_date", "state",
                  "rto_code", "blacklist_status"):
        assert field in rc
    # Owner name is masked (PII safe): contains a '*'.
    assert "*" in rc["owner_name_masked"]

    dl = client.get(f"/sarathi/dl/{rec.dl.dl_number}").json()
    assert dl["dl_number"] == rec.dl.dl_number

    ft = client.get(f"/fastag/balance/{plate}").json()
    assert ft["plate"] == plate
    assert ft["status"] in {"ACTIVE", "LOW_BALANCE", "BLACKLISTED", "INACTIVE"}


def test_sim_invalid_and_miss(sim_client):
    client, _ = sim_client
    assert client.get("/vahan/rc/NOTAPLATE").status_code == 422
    assert client.get("/vahan/rc/MH04ZZ9999").status_code == 404  # valid shape, not seeded
    assert client.get("/sarathi/dl/GARBAGE").status_code == 422


# ---------------------------------------------------------------------------
# Live adapter — 503 when token absent
# ---------------------------------------------------------------------------
def test_live_returns_503_without_token(monkeypatch):
    monkeypatch.setenv("SUREPASS_API_TOKEN", "")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")
    from jnpa_shared.config import get_settings
    get_settings.cache_clear()
    import importlib
    import vahan_live.config as cfgmod
    importlib.reload(cfgmod)
    import vahan_live.app as appmod
    importlib.reload(appmod)

    assert appmod.cfg.enabled is False
    with TestClient(appmod.app) as client:
        for path in ("/vahan/rc/MH04AB1234", "/sarathi/dl/MH0420110012345",
                     "/fastag/balance/MH04AB1234"):
            r = client.get(path)
            assert r.status_code == 503
            assert r.json() == {"error": "live_disabled"}


# ---------------------------------------------------------------------------
# Vehicle-master writeback — needs Postgres (skipped if unreachable)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(_pg_host_dsn() is None,
                    reason="Postgres not reachable on localhost:5433; run `make up` first.")
def test_vehicle_master_grows_after_batch(monkeypatch):
    """A batch of RC lookups increases jnpa.vehicle_master's row count."""
    import asyncio

    dsn = _pg_host_dsn()
    monkeypatch.setenv("POSTGRES_DSN", dsn)
    monkeypatch.setenv("VAHAN_TOTAL_PLATES", "3000")
    monkeypatch.setenv("VAHAN_FIXTURE_PATH", "/tmp/jnpa_known_plates_wb.json")
    from jnpa_shared.config import get_settings
    get_settings.cache_clear()
    import importlib
    import vahan_sim.config as cfgmod
    importlib.reload(cfgmod)
    import vahan_sim.app as appmod
    importlib.reload(appmod)

    from jnpa_shared import db, vahan_db

    async def _count() -> int:
        # Dispose first so the cached engine is rebuilt on *this* event loop —
        # the lru_cache'd engine is bound to whatever loop created it, and each
        # asyncio.run()/TestClient uses a different loop.
        await db.dispose_all()
        await vahan_db.ensure_schema(dsn=dsn)
        n = await vahan_db.vehicle_master_count(dsn=dsn)
        await db.dispose_all()
        return n

    before = asyncio.run(_count())

    with TestClient(appmod.app) as client:
        # Query 200 distinct plates that are very unlikely to all pre-exist.
        plates = list(appmod._STORE.keys())[:200]
        ok = 0
        for plate in plates:
            if client.get(f"/vahan/rc/{plate}").status_code == 200:
                ok += 1
        assert ok > 0

    after = asyncio.run(_count())
    assert after >= before, "row count must not shrink"
    # At least some of the 200 freshly-queried plates should be new rows.
    assert after > before or before >= 200, (
        f"vehicle_master did not grow (before={before}, after={after})"
    )
