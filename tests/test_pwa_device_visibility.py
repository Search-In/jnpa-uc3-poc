"""Driver PWA device identity -> Control Centre Congestion Rerouting.

THE BUG THIS PINS. A driver signing in to the PWA establishes a device identity
end to end:

    vehicle plate -> core.vehicle.vehicle_id -> TRK-###### -> JWT device_id
                  -> core.push_subscription.device_id

but the Driver-Advisory (Congestion Rerouting) console read ONLY the truck
simulator's in-memory ``Fleet.trucks`` population. A signed-in driver therefore
never appeared there — except by coincidence, when the simulator happened to
hold a synthetic truck with the same id in AT_GATE_QUEUE. The operator could not
see, let alone re-route, a real driver.

THE FIX ASSERTED HERE. ``GET /api/trucks?state=…`` now additionally returns
``registered_devices``: the devices a real driver is signed in on, read back from
``core.push_subscription``. The invariants that make that safe are the substance
of this suite:

  * registered devices are a SEPARATE list — never merged into ``devices``, so
    ``count``, the queue-depth cards and the empty/unavailable classification
    still describe the AT_GATE_QUEUE measurement and nothing else;
  * every device carries an explicit ``source`` (``truck-sim`` vs
    ``pwa-registered``) so the console cannot present one as the other;
  * a registration is not a measurement: ``state``/``eta_s``/``remaining_km``/
    ``gate_id`` are ALWAYS null for a registered device — never 0, never derived;
  * a registered device appears WITHOUT being in AT_GATE_QUEUE, and survives a
    simulator outage that makes the queue itself unanswerable;
  * the existing simulator ladder (LIVE -> CACHED -> unanswerable) is untouched.

Plus the collision fix: operator-created Vehicle IDs are minted from a reserved
range that the simulator's namespace can never reach.

No server and no database: the router handlers are called directly with the
gateway state stubbed, per this repo's router-test idiom.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from gateway import fleet  # noqa: E402
from gateway.routers import trucks as T  # noqa: E402

DRIVER_A = "TRK-000026"
DRIVER_B = "TRK-000028"
DSN = "postgresql+asyncpg://x:x@127.0.0.1:1/none"


# --------------------------------------------------------------------- stubs
class _Resp:
    def __init__(self, status_code: int = 200, body: Optional[dict] = None):
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict:
        return self._body


class _Http:
    def __init__(self, result: Any):
        self.result = result

    async def get(self, url, params=None, timeout=None):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _gw(http_result: Any, *, dsn: Optional[str] = DSN) -> SimpleNamespace:
    async def record_decision(**kw):
        pass

    return SimpleNamespace(
        cfg=SimpleNamespace(truck_api_url="http://truck-sim:9000", postgres_dsn=dsn),
        http=_Http(http_result),
        record_decision=record_decision,
    )


# One signed-in driver device as the registered-device query returns it: a real
# push registration joined to the Vehicle Master, with a real telemetry fix.
REGISTERED_ROW = {
    "device_id": DRIVER_A,
    "plate": "MH04QA9911",
    "driver_id": "DRV-0001",
    "driver_name": "A. Driver",
    "lat": 18.949,
    "lon": 72.951,
    "last_seen": datetime(2026, 8, 13, 6, 30, tzinfo=timezone.utc),
}

# Two synthetic simulator trucks, exactly as the sim serves them (no `source`:
# the sim does not stamp its own payload — the gateway does).
SIM_BODY = {
    "count": 2,
    "filter_state": "AT_GATE_QUEUE",
    "devices": [
        {"device_id": "TRK-000014", "plate": "KL07WB9662", "gate_id": "G-JNPCT",
         "state": "AT_GATE_QUEUE", "remaining_km": 0.0, "eta_s": 1500},
        {"device_id": "TRK-000174", "plate": "TN22SR0294", "gate_id": "G-JNPCT",
         "state": "AT_GATE_QUEUE", "remaining_km": 0.0, "eta_s": 1380},
    ],
}


@pytest.fixture(autouse=True)
def _clean():
    T._LIST_CACHE.clear()
    T._REGISTERED_CACHE.clear()
    fleet._MEM.clear()
    fleet._BACKEND.clear()
    yield
    T._LIST_CACHE.clear()
    T._REGISTERED_CACHE.clear()
    fleet._MEM.clear()
    fleet._BACKEND.clear()


@pytest.fixture()
def registered_db(monkeypatch):
    """Serve ``rows`` from the registered-device query; record the SQL it ran."""
    import jnpa_shared.db as real_db

    state = SimpleNamespace(rows=[dict(REGISTERED_ROW)], sql=[], params=[])

    async def fetch_all(sql, params=None, *, dsn=None):
        state.sql.append(sql)
        state.params.append(dict(params or {}))
        return [dict(r) for r in state.rows]

    monkeypatch.setattr(real_db, "fetch_all", fetch_all, raising=False)
    return state


def _list(gw, state="AT_GATE_QUEUE", limit=500) -> dict:
    return asyncio.run(T.list_trucks(state=state, limit=limit, gw=gw))


# ============================================================ the device rung
# Scenario 4: a registered PWA device appears in Congestion Rerouting.
def test_registered_pwa_device_appears_in_the_advisory_answer(registered_db):
    body = _list(_gw(_Resp(200, SIM_BODY)))
    ids = [d["device_id"] for d in body["registered_devices"]]
    assert ids == [DRIVER_A]
    assert body["registered_count"] == 1


# Scenario 5 + 6: provenance is explicit on BOTH sides, never inferred.
def test_every_device_declares_whether_it_is_real_or_synthetic(registered_db):
    body = _list(_gw(_Resp(200, SIM_BODY)))
    assert {d["source"] for d in body["devices"]} == {"truck-sim"}
    assert {d["source"] for d in body["registered_devices"]} == {"pwa-registered"}


# Scenario 7: the whole point — no AT_GATE_QUEUE membership is required.
def test_a_registered_device_appears_without_being_in_the_gate_queue(registered_db):
    """The simulator reports an EMPTY gate queue. The signed-in driver is still
    listed: their visibility depends on having signed in, not on the simulator
    having placed a synthetic truck of the same id into a queue."""
    empty = {"count": 0, "filter_state": "AT_GATE_QUEUE", "devices": []}
    body = _list(_gw(_Resp(200, empty)))
    assert body["devices"] == []                       # queue genuinely empty
    assert body["count"] == 0
    assert [d["device_id"] for d in body["registered_devices"]] == [DRIVER_A]


def test_a_registered_device_survives_a_truck_sim_outage(registered_db):
    """With the sim unreachable and no memo the QUEUE is unanswerable — but who
    is signed in is a DB question, so the operator can still re-route a real
    driver. The queue's own posture is unchanged (see the invariants below)."""
    import httpx

    body = _list(_gw(httpx.ConnectError("refused")))
    assert body["devices"] == []
    assert body["state_filter_supported"] is False     # queue: still unanswerable
    assert [d["device_id"] for d in body["registered_devices"]] == [DRIVER_A]


# Scenarios 8 + 9: a registration is not a measurement.
def test_no_eta_no_distance_no_state_is_invented_for_a_registered_device(registered_db):
    dev = _list(_gw(_Resp(200, SIM_BODY)))["registered_devices"][0]
    # Every one of these is NULL, not 0 and not derived. A 0.0 here would render
    # as "0.0 km" / "<1 min" in the console and read as a live measurement.
    assert dev["eta_s"] is None
    assert dev["remaining_km"] is None
    assert dev["state"] is None
    assert dev["gate_id"] is None
    assert dev["speed_kmh"] is None
    assert dev["heading"] is None


def test_real_telemetry_is_used_when_it_exists_and_null_when_it_does_not(registered_db):
    with_fix = _list(_gw(_Resp(200, SIM_BODY)))["registered_devices"][0]
    assert with_fix["position"] == {"lat": 18.949, "lon": 72.951}
    assert with_fix["last_seen"] == "2026-08-13T06:30:00+00:00"

    # A device that has never reported: no position is fabricated for it.
    T._REGISTERED_CACHE.clear()
    registered_db.rows = [dict(REGISTERED_ROW, lat=None, lon=None, last_seen=None)]
    without = _list(_gw(_Resp(200, SIM_BODY)))["registered_devices"][0]
    assert without["position"] is None
    assert without["last_seen"] is None


def test_the_driver_binding_is_carried_through_for_display(registered_db):
    dev = _list(_gw(_Resp(200, SIM_BODY)))["registered_devices"][0]
    assert dev["driver_id"] == "DRV-0001"
    assert dev["driver_name"] == "A. Driver"
    assert dev["plate"] == "MH04QA9911"


# ------------------------------------------------------------- query contract
def test_the_registered_query_only_counts_live_registrations_on_known_vehicles(
    registered_db,
):
    """The predicate is the guarantee that this list is REAL: a row needs a push
    registration, a recent refresh, and a Vehicle Master entry — the same gate
    POST /api/driver/login applies. Without these it would be an arbitrary
    device list."""
    _list(_gw(_Resp(200, SIM_BODY)))
    sql = " ".join(registered_db.sql[0].split())
    assert "FROM core.push_subscription p" in sql
    assert "JOIN core.vehicle v ON v.vehicle_id = p.device_id" in sql
    assert "p.webpush IS NOT NULL OR p.fcm_token IS NOT NULL" in sql
    assert "p.updated_at > now() - interval '12 hours'" in sql
    # The driver join is by the SAME identity spine the PWA login uses.
    assert "d.vehicle_no_norm = p.device_id" in sql
    assert "d.status = 'ACTIVE'" in sql


def test_a_vehicle_id_is_never_presented_as_a_registration_plate(registered_db):
    """core.vehicle.vehicle_no falls back to the vehicle_id when no plate is
    known, so the query must NULL that case out rather than print 'TRK-000026'
    in the PLATE column."""
    _list(_gw(_Resp(200, SIM_BODY)))
    assert "NULLIF(v.vehicle_no, v.vehicle_id) AS plate" in " ".join(
        registered_db.sql[0].split()
    )


def test_the_rung_degrades_to_empty_and_never_breaks_the_fleet_list(monkeypatch):
    """Additive means additive: a failing registered-device query must not be
    able to take the gate queue down with it."""
    import jnpa_shared.db as real_db

    async def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(real_db, "fetch_all", boom, raising=False)
    body = _list(_gw(_Resp(200, SIM_BODY)))
    assert body["registered_devices"] == []
    assert [d["device_id"] for d in body["devices"]] == ["TRK-000014", "TRK-000174"]


def test_no_dsn_means_no_registered_devices_not_an_error():
    body = _list(_gw(_Resp(200, SIM_BODY), dsn=None))
    assert body["registered_devices"] == []
    assert body["count"] == 2


# ================================================== existing behaviour intact
# Scenario 15: the simulator queue is unchanged.
def test_the_queue_itself_still_means_exactly_what_it_meant(registered_db):
    """`devices` / `count` describe the AT_GATE_QUEUE measurement ONLY. Folding
    registered devices in here would silently inflate every queue-depth card."""
    body = _list(_gw(_Resp(200, SIM_BODY)))
    assert [d["device_id"] for d in body["devices"]] == ["TRK-000014", "TRK-000174"]
    assert body["count"] == 2                          # NOT 3
    assert body["decision_path"] == "PRIMARY"
    assert body["source"] == "truck-sim"
    assert body["degraded"] is False
    assert body["state_filter_supported"] is True
    assert DRIVER_A not in [d["device_id"] for d in body["devices"]]


# Scenario 16: a genuinely empty queue still reads as empty, not as unavailable.
def test_a_genuinely_empty_gate_queue_is_still_reported_as_empty(registered_db):
    empty = {"count": 0, "filter_state": "AT_GATE_QUEUE", "devices": []}
    body = _list(_gw(_Resp(200, empty)))
    assert body["count"] == 0
    assert body["degraded"] is False                   # answered, just empty
    assert body["state_filter_supported"] is True


def test_the_unfiltered_fleet_list_is_untouched(registered_db):
    """`registered_devices` is attached to STATE-FILTERED answers only — the
    unfiltered fleet read is a different question with its own rungs."""
    body = _list(_gw(_Resp(200, SIM_BODY)), state=None)
    assert "registered_devices" not in body


def test_the_memo_never_learns_the_registered_list(registered_db):
    """_LIST_CACHE must hold exactly what the simulator said. A registered device
    leaking into the memo would be replayed as a queue member on the CACHED rung."""
    _list(_gw(_Resp(200, SIM_BODY)))
    assert len(T._LIST_CACHE) == 1
    (_, cached) = next(iter(T._LIST_CACHE.values()))
    assert "registered_devices" not in cached
    assert [d["device_id"] for d in cached["devices"]] == ["TRK-000014", "TRK-000174"]


# ============================================ Vehicle ID collision (scenario 14)
def test_operator_vehicle_ids_cannot_collide_with_the_simulator_namespace():
    """The simulator mints TRK-000001 … TRK-{num_devices} (default 20 000, hard
    ceiling 30 000) while boot sync imports only the first 5 000 into
    core.vehicle. The old MAX(suffix)+1 therefore handed out TRK-005001 — an id
    the simulator was already using for a DIFFERENT truck with a DIFFERENT plate,
    so a re-route pushed to it would have reached the wrong driver."""
    first = asyncio.run(fleet.next_vehicle_id(DSN))
    assert first == "TRK-900001"
    assert fleet.is_operator_id(first)
    assert not fleet.is_simulator_id(first)


def test_a_simulator_id_can_never_advance_the_operator_sequence():
    """Even with the WHOLE simulator fleet synced into the master, the next
    operator id stays in the reserved range."""
    async def go():
        # every id the sim could possibly mint, including its hard ceiling
        for seq in (1, 5_000, 20_000, 30_000):
            await fleet.add_vehicle(DSN, vehicle_id=f"TRK-{seq:06d}",
                                    vehicle_number=f"MH04AB{seq % 10000:04d}",
                                    created_by="system:truck-sim")
        return await fleet.next_vehicle_id(DSN)

    assert asyncio.run(go()) == "TRK-900001"


def test_operator_ids_still_advance_among_themselves():
    async def go():
        ids = []
        for i in range(3):
            vid = await fleet.next_vehicle_id(DSN)
            await fleet.add_vehicle(DSN, vehicle_id=vid,
                                    vehicle_number=f"MH04ZZ{i:04d}",
                                    created_by="operator")
            ids.append(vid)
        return ids

    assert asyncio.run(go()) == ["TRK-900001", "TRK-900002", "TRK-900003"]


def test_the_two_namespaces_are_disjoint_by_construction():
    """The property, not just the examples: nothing the simulator can mint is an
    operator id, and nothing minted for an operator is a simulator id."""
    sim_pkg = str(REPO_ROOT / "ingest" / "trucking_app")
    if sim_pkg not in sys.path:
        sys.path.insert(0, sim_pkg)
    from trucking_app.config import TruckConfig

    # Read from the simulator's OWN config, so raising its ceiling without
    # raising the operator floor fails here rather than in production.
    ceiling = TruckConfig().max_devices
    assert ceiling < fleet.OPERATOR_ID_FLOOR
    for seq in (1, 2, ceiling - 1, ceiling):
        vid = f"TRK-{seq:06d}"
        assert fleet.is_simulator_id(vid) and not fleet.is_operator_id(vid)
    for seq in (fleet.OPERATOR_ID_FLOOR, fleet.OPERATOR_ID_FLOOR + 5_000, 999_999):
        vid = f"TRK-{seq:06d}"
        assert fleet.is_operator_id(vid) and not fleet.is_simulator_id(vid)
