"""UC-II -> UC-III cross-twin (XT-2) end-to-end integration.

Walks the whole contract path the audit flagged as unverified:

    UC-II event  ->  transport (Kafka pump | HTTP inject)
                 ->  UC-III consumer (gateway.crosstwin.apply)
                 ->  slot book applied      (gateway.tas_mock)
                 ->  DB persistence         (core.deferred_arrival_window)
                 ->  WebSocket notification (dashboard "tas" + per-driver advisory)

Two properties matter and are asserted separately:

  * **Both transports converge.** The Kafka pump and ``POST /api/tas/deferred-windows``
    must reach the SAME applier, or the deployed topology behaves differently
    from the one that gets demoed (audit task D-07).
  * **Every leg after the apply is best-effort.** Metering a gate must not fail
    because RDS, the WS hub, or push is down. Each is failed independently here
    and the apply must still succeed.

Runs with fakes for the repository / WS hub / push, so no database, broker or
socket is required.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from jnpa_shared.schemas import DeferredArrivalWindow  # noqa: E402

from gateway import crosstwin, tas_mock  # noqa: E402

CORR = "XT2-TEST-0001"
GATE = "GATE-3"
DEVICE_A = "TRK-000026"
DEVICE_B = "TRK-000028"


# --------------------------------------------------------------------- fakes
class _FakeWs:
    def __init__(self, fail: bool = False):
        self.frames: list[tuple[str, dict]] = []
        self.fail = fail

    async def broadcast(self, topic, payload):
        if self.fail:
            raise RuntimeError("ws hub down")
        self.frames.append((topic, payload))


class _FakeRepo:
    def __init__(self, fail: bool = False):
        self.rows: list[dict] = []
        self.fail = fail

    async def upsert(self, window, *, transport="KAFKA"):
        if self.fail:
            raise RuntimeError("rds unreachable")
        self.rows.append({"window": dict(window), "transport": transport})
        return True


class _Cfg:
    postgres_dsn = "postgresql+asyncpg://x:x@127.0.0.1:1/none"


class _Gw:
    def __init__(self, ws=None):
        self.cfg = _Cfg()
        self.ws = ws or _FakeWs()


def _window(correlation_id=CORR, gate_id=GATE, slot_cap=4, window_min=45):
    """A DeferredArrivalWindow exactly as UC-II publishes it.

    Built through the REAL pydantic model (``jnpa_shared.schemas``) rather than a
    hand-rolled dict, so this suite exercises the same validation the Kafka pump
    and the HTTP inject route apply. A dict would have passed the test while
    diverging from the wire contract.
    """
    return DeferredArrivalWindow(
        correlation_id=correlation_id,
        gate_id=gate_id,
        window_start="2026-08-04T09:00:00Z",
        window_min=window_min,
        slot_cap=slot_cap,
        source="UC-II",
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Fresh slot book + repo per test."""
    crosstwin.reset_for_tests()
    if hasattr(tas_mock, "reset_for_tests"):
        tas_mock.reset_for_tests()
    yield
    crosstwin.reset_for_tests()


def _use_repo(monkeypatch, repo):
    monkeypatch.setattr(crosstwin, "_repo", lambda gw: repo)


def _no_affected_drivers(monkeypatch):
    async def _none(gw, gate_id, slot_codes):
        return []
    monkeypatch.setattr(crosstwin, "_affected_devices", _none)


def _affected(monkeypatch, rows):
    async def _rows(gw, gate_id, slot_codes):
        return rows
    monkeypatch.setattr(crosstwin, "_affected_devices", _rows)


# ==================================================== 1. the happy path
@pytest.mark.asyncio
async def test_uc2_window_is_applied_persisted_and_broadcast(monkeypatch):
    repo, ws = _FakeRepo(), _FakeWs()
    _use_repo(monkeypatch, repo)
    _no_affected_drivers(monkeypatch)

    res = await crosstwin.apply(_Gw(ws), _window(), transport="KAFKA")

    # applied to the slot book
    assert "applied_slots" in res
    # persisted — the durability gap 0115 closed
    assert len(repo.rows) == 1
    assert repo.rows[0]["window"]["correlation_id"] == CORR
    assert repo.rows[0]["transport"] == "KAFKA"
    # dashboard told
    topics = [t for t, _ in ws.frames]
    assert "tas" in topics
    payload = next(p for t, p in ws.frames if t == "tas")
    assert payload["type"] == "deferred_arrival_applied"
    assert payload["correlation_id"] == CORR
    assert payload["gate_id"] == GATE
    assert payload["transport"] == "KAFKA"


@pytest.mark.asyncio
async def test_result_reports_what_actually_happened(monkeypatch):
    """The applier returns facts, not assumptions — the demo asserts on these."""
    _use_repo(monkeypatch, _FakeRepo())
    _no_affected_drivers(monkeypatch)
    res = await crosstwin.apply(_Gw(), _window(), transport="HTTP")
    assert res["persisted"] is True
    assert res["notified"] == 0
    assert res["transport"] == "HTTP"


# ============================================ 2. both transports converge
@pytest.mark.asyncio
async def test_kafka_and_http_reach_the_same_applier(monkeypatch):
    """Audit D-07: the deployed (Kafka) path must not differ from the demoed one.

    Same window over both transports must produce the same applied slots and the
    same persisted record — only the `transport` tag differs.
    """
    repo = _FakeRepo()
    _use_repo(monkeypatch, repo)
    _no_affected_drivers(monkeypatch)

    via_kafka = await crosstwin.apply(_Gw(), _window("XT2-K"), transport="KAFKA")
    if hasattr(tas_mock, "reset_for_tests"):
        tas_mock.reset_for_tests()
    via_http = await crosstwin.apply(_Gw(), _window("XT2-H"), transport="HTTP")

    assert via_kafka["applied_slots"] == via_http["applied_slots"]
    assert via_kafka["persisted"] == via_http["persisted"] is True
    assert {r["transport"] for r in repo.rows} == {"KAFKA", "HTTP"}


@pytest.mark.asyncio
async def test_transport_is_recorded_so_provenance_is_auditable(monkeypatch):
    repo = _FakeRepo()
    _use_repo(monkeypatch, repo)
    _no_affected_drivers(monkeypatch)
    await crosstwin.apply(_Gw(), _window(), transport="KAFKA")
    assert repo.rows[0]["transport"] == "KAFKA"


# ================================== 3. driver advisories are per-device
@pytest.mark.asyncio
async def test_only_affected_drivers_are_notified(monkeypatch):
    """Driver A holds a rescheduled slot; driver B does not. Only A is told."""
    _use_repo(monkeypatch, _FakeRepo())
    _affected(monkeypatch, [{"driver_id": "DRV-A", "vehicle_id": DEVICE_A,
                             "slot_code": "S-0900"}])

    dispatched: list[dict] = []

    async def _resolve(gw, *, driver_id=None, vehicle_id=None):
        return vehicle_id

    async def _dispatch(gw, device_id, **kw):
        dispatched.append({"device_id": device_id, **kw})
        return {"ok": True}

    from gateway import notifications
    from gateway.routers import push as push_router
    monkeypatch.setattr(push_router, "resolve_device", _resolve)
    monkeypatch.setattr(notifications, "dispatch_alert", _dispatch)

    res = await crosstwin.apply(_Gw(), _window())

    assert res["notified"] == 1
    assert len(dispatched) == 1
    assert dispatched[0]["device_id"] == DEVICE_A
    assert DEVICE_B not in [d["device_id"] for d in dispatched]
    assert dispatched[0]["kind"] == "TAS_RESLOT"
    # provenance travels with the advisory so the driver's app can attribute it
    assert dispatched[0]["extra"]["source"] == "UC-II"
    assert dispatched[0]["extra"]["correlation_id"] == CORR


@pytest.mark.asyncio
async def test_driver_without_a_resolvable_device_is_skipped(monkeypatch):
    """An unpaired driver must not abort the loop for everyone else."""
    _use_repo(monkeypatch, _FakeRepo())
    _affected(monkeypatch, [
        {"driver_id": "DRV-NO-DEVICE", "vehicle_id": None, "slot_code": "S-0900"},
        {"driver_id": "DRV-B", "vehicle_id": DEVICE_B, "slot_code": "S-0915"},
    ])
    dispatched = []

    async def _resolve(gw, *, driver_id=None, vehicle_id=None):
        return vehicle_id  # None for the unpaired driver

    async def _dispatch(gw, device_id, **kw):
        dispatched.append(device_id)
        return {"ok": True}

    from gateway import notifications
    from gateway.routers import push as push_router
    monkeypatch.setattr(push_router, "resolve_device", _resolve)
    monkeypatch.setattr(notifications, "dispatch_alert", _dispatch)

    res = await crosstwin.apply(_Gw(), _window())
    assert dispatched == [DEVICE_B]
    assert res["notified"] == 1


# ============================== 4. every leg after apply is best-effort
@pytest.mark.asyncio
async def test_apply_survives_a_dead_database(monkeypatch):
    """Metering a gate must not depend on RDS being reachable.

    REGRESSION: apply() used to call the repository unguarded, so an unreachable
    RDS raised out of apply() — the Kafka pump saw an exception and the HTTP
    inject route 500'd, losing a window that had ALREADY been applied to the slot
    book. The meter was in force with nothing recording it.
    """
    _use_repo(monkeypatch, _FakeRepo(fail=True))
    _no_affected_drivers(monkeypatch)
    ws = _FakeWs()
    try:
        res = await crosstwin.apply(_Gw(ws), _window())
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"apply must not propagate a persistence failure: {exc!r}")
    # The meter still applied, and the dashboard was still told.
    assert "applied_slots" in res
    assert any(t == "tas" for t, _ in ws.frames)
    # ...but the result does NOT claim durability it did not achieve.
    assert res["persisted"] is False


@pytest.mark.asyncio
async def test_apply_survives_a_dead_ws_hub(monkeypatch):
    _use_repo(monkeypatch, _FakeRepo())
    _no_affected_drivers(monkeypatch)
    res = await crosstwin.apply(_Gw(_FakeWs(fail=True)), _window())
    assert res["persisted"] is True   # persistence unaffected by the WS failure


@pytest.mark.asyncio
async def test_apply_survives_a_push_failure(monkeypatch):
    _use_repo(monkeypatch, _FakeRepo())
    _affected(monkeypatch, [{"driver_id": "DRV-A", "vehicle_id": DEVICE_A,
                             "slot_code": "S-0900"}])

    async def _resolve(gw, **kw):
        raise RuntimeError("push backend down")

    from gateway.routers import push as push_router
    monkeypatch.setattr(push_router, "resolve_device", _resolve)

    res = await crosstwin.apply(_Gw(), _window())
    assert res["persisted"] is True
    assert res["notified"] == 0     # reported honestly, not silently claimed


@pytest.mark.asyncio
async def test_notified_count_is_truthful_when_dispatch_returns_none(monkeypatch):
    """dispatch_alert returning None means "not delivered" — do not count it."""
    _use_repo(monkeypatch, _FakeRepo())
    _affected(monkeypatch, [{"driver_id": "DRV-A", "vehicle_id": DEVICE_A,
                             "slot_code": "S-0900"}])

    async def _resolve(gw, *, driver_id=None, vehicle_id=None):
        return vehicle_id

    async def _dispatch(gw, device_id, **kw):
        return None

    from gateway import notifications
    from gateway.routers import push as push_router
    monkeypatch.setattr(push_router, "resolve_device", _resolve)
    monkeypatch.setattr(notifications, "dispatch_alert", _dispatch)

    res = await crosstwin.apply(_Gw(), _window())
    assert res["notified"] == 0


# =================================================== 5. idempotence
@pytest.mark.asyncio
async def test_same_correlation_id_applied_twice_is_recorded_by_the_repo(monkeypatch):
    """Redelivery is normal on Kafka; the repo's correlation_id key absorbs it."""
    repo = _FakeRepo()
    _use_repo(monkeypatch, repo)
    _no_affected_drivers(monkeypatch)
    await crosstwin.apply(_Gw(), _window(), transport="KAFKA")
    await crosstwin.apply(_Gw(), _window(), transport="KAFKA")
    assert {r["window"]["correlation_id"] for r in repo.rows} == {CORR}
