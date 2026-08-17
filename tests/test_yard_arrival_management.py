"""UC-3 peak-yard utilisation + truck-arrival management.

Covers the pure decision layer (thresholds, congestion pressure, parking choice,
hold plan, release arithmetic) and the orchestration with every port injected —
no database, no gateway, no network. The endpoints are smoke-tested for wiring
and RBAC only; their data path is the repository, which needs Postgres.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from services.yard_capacity import model  # noqa: E402
from services.yard_capacity.service import (  # noqa: E402
    ADVISORY_HOLD, ADVISORY_RELEASE, YardCapacityService, YardThresholds)


# --------------------------------------------------------------- thresholds
def test_utilization_bands():
    band = lambda p: model.utilization_status(p, high_pct=90.0, critical_pct=95.0)  # noqa: E731
    assert band(62.0) == model.STATUS_NORMAL
    assert band(83.0) == model.STATUS_ELEVATED     # within 10 pts of HIGH
    assert band(90.0) == model.STATUS_HIGH
    assert band(95.0) == model.STATUS_CRITICAL
    assert model.constrained(band(95.0)) and not model.constrained(band(83.0))


def test_utilization_pct_and_slots():
    assert model.utilization_pct(4560, 4800) == 95.0
    assert model.utilization_pct(10, 0) == 0.0        # no capacity -> never NaN
    assert model.available_slots(4560, 4800) == 240
    assert model.admissible_trucks(240, 2) == 120     # 2 ground slots per truck
    assert model.admissible_trucks(1, 2) == 0         # half a slot admits nobody


def test_operating_ceiling_reserves_the_critical_band():
    # The yard plans up to 95% of capacity, not to the last physical slot.
    assert model.operating_ceiling(4800, 95.0) == 4560
    # At the ceiling there is free space on the ground but none to book.
    assert model.available_slots(4560, 4800) == 240
    assert model.headroom_slots(4560, 4800, 95.0) == 0
    assert model.admissible_trucks(model.headroom_slots(4560, 4800, 95.0), 2) == 0
    # Below it, headroom is what is bookable before the reserve.
    assert model.headroom_slots(4500, 4800, 95.0) == 60


# -------------------------------------------------------------- pressure
def test_congestion_pressure_needs_both_terms():
    # A full yard with nothing arriving is not a gate problem.
    assert model.congestion_pressure(utilisation=95.0, arrivals=0, admissible=0) == 0.57
    # An empty yard with a queue is not one either.
    assert model.congestion_pressure(utilisation=10.0, arrivals=30, admissible=30) == 0.06
    # Full yard + more arrivals than it can take -> above the 0.80 alert bar.
    assert model.congestion_pressure(utilisation=95.0, arrivals=30, admissible=2) > 0.80


def test_congestion_pressure_is_clamped_and_deterministic():
    a = model.congestion_pressure(utilisation=140.0, arrivals=10, admissible=-5)
    assert a == 1.0
    assert a == model.congestion_pressure(utilisation=140.0, arrivals=10, admissible=-5)


# --------------------------------------------------------------- parking
FACILITIES = [
    {"facility_id": "PK-NSICT", "name": "NSICT lot", "capacity": 120,
     "available": 4, "status": "FILLING"},
    {"facility_id": "PK-CPP", "name": "Common Parking Plaza (CPP)", "capacity": 450,
     "available": 180, "status": "AVAILABLE"},
    {"facility_id": "PK-HOLDING", "name": "Truck holding yard", "capacity": 300,
     "available": 250, "status": "AVAILABLE"},
]


def test_select_parking_prefers_authorised_cpp():
    f = model.select_parking(FACILITIES, preferred_id="PK-CPP")
    assert f["facility_id"] == "PK-CPP"          # even though HOLDING has more room


def test_select_parking_falls_back_to_most_free_when_cpp_full():
    full_cpp = [dict(f, available=0, status="FULL") if f["facility_id"] == "PK-CPP" else f
                for f in FACILITIES]
    assert model.select_parking(full_cpp, preferred_id="PK-CPP")["facility_id"] == "PK-HOLDING"


def test_select_parking_returns_none_rather_than_inventing_a_location():
    none_free = [dict(f, available=0, status="FULL") for f in FACILITIES]
    assert model.select_parking(none_free, preferred_id="PK-CPP") is None


def test_estimated_wait_is_none_without_a_configured_rate():
    assert model.estimated_wait_min(slots_needed=40, release_rate_slots_per_hour=None) is None
    assert model.estimated_wait_min(slots_needed=40, release_rate_slots_per_hour=0) is None
    assert model.estimated_wait_min(slots_needed=40, release_rate_slots_per_hour=120) == 20


# ------------------------------------------------------------- hold plan
def _cands(n, start_eta=600.0, source="truck-sim"):
    return [model.ArrivalCandidate(device_id=f"TRK-{i:03d}", source=source,
                                   gate_id="G-NSICT", eta_s=start_eta + i * 60)
            for i in range(n)]


def test_no_holds_while_the_yard_is_comfortable():
    plan = model.plan_holds(yard_id="Y", capacity_slots=4800, occupied_slots=3360,
                            candidates=_cands(12), high_pct=90.0, critical_pct=95.0,
                            slots_per_truck=2)
    assert not plan.constrained and plan.hold == [] and len(plan.proceed) == 12


def test_holds_only_the_surplus_and_keeps_the_nearest_trucks_moving():
    # 4800 capacity, ceiling 4560, occupied 4550 -> 10 bookable -> 5 admissible.
    plan = model.plan_holds(yard_id="Y", capacity_slots=4800, occupied_slots=4550,
                            candidates=_cands(12), high_pct=90.0, critical_pct=95.0,
                            slots_per_truck=2)
    assert plan.constrained
    assert len(plan.hold) == 7 and len(plan.proceed) == 5
    # The five kept moving are the nearest (smallest ETA).
    assert {c.device_id for c in plan.proceed} == {f"TRK-{i:03d}" for i in range(5)}
    assert plan.reason == "Yard capacity is currently constrained."


def test_unmeasured_pwa_devices_are_held_before_measured_trucks():
    cands = _cands(2) + [model.ArrivalCandidate(device_id="PWA-1",
                                                source="pwa-registered", eta_s=None)]
    plan = model.plan_holds(yard_id="Y", capacity_slots=100, occupied_slots=100,
                            candidates=cands, high_pct=90.0, critical_pct=95.0,
                            slots_per_truck=2)
    assert [c.device_id for c in plan.hold][0] == "PWA-1"


def test_evaluation_is_idempotent_for_already_held_trucks():
    plan = model.plan_holds(yard_id="Y", capacity_slots=4800, occupied_slots=4550,
                            candidates=_cands(12), high_pct=90.0, critical_pct=95.0,
                            slots_per_truck=2,
                            already_held=[f"TRK-{i:03d}" for i in range(5, 12)])
    assert plan.arrivals == 5 and plan.hold == []


def test_releasable_scales_with_recovered_capacity():
    # Still critical, 10 slots bookable below the ceiling -> 5 trucks may return.
    assert model.releasable(capacity_slots=4800, occupied_slots=4550, active_holds=7,
                            high_pct=90.0, critical_pct=95.0, slots_per_truck=2) == 5
    # Dropped out of the constrained band -> everybody is released.
    assert model.releasable(capacity_slots=4800, occupied_slots=3360, active_holds=7,
                            high_pct=90.0, critical_pct=95.0, slots_per_truck=2) == 7


# ------------------------------------------------------- service wiring
class FakeRepo:
    """In-memory stand-in for YardCapacityRepository (no SQL)."""

    def __init__(self, capacity=4800, occupied=3360):
        self.state = {"yard_id": "JNPA-NSICT-YARD", "terminal_code": "NSICT",
                      "name": "NSICT container yard", "capacity_slots": capacity,
                      "occupied_slots": occupied, "high_threshold_pct": None,
                      "critical_threshold_pct": None, "source": "DECLARED_SEED",
                      "source_note": "declared", "active": True, "updated_at": None}
        self.rows: list[dict] = []
        self.events: list[dict] = []
        self.notified: list[tuple] = []
        self._next = 1

    async def list_yards(self):
        return [dict(self.state)]

    async def get_yard(self, yard_id):
        return dict(self.state) if yard_id == self.state["yard_id"] else None

    async def capacity_for(self, terminal_code):
        return None  # no core.yard_block rows -> declared capacity, declared flag

    async def adjust_occupancy(self, *, yard_id, delta_slots, set_occupied, event_type,
                               reason, actor, status_fn, detail=None):
        cap = self.state["capacity_slots"]
        before = self.state["occupied_slots"]
        target = set_occupied if set_occupied is not None else before + (delta_slots or 0)
        after = max(0, min(cap, target))
        self.state["occupied_slots"] = after
        pct = round(100.0 * after / cap, 2)
        ev = {"event_type": event_type, "delta_slots": after - before,
              "occupied_before": before, "occupied_after": after,
              "utilization_pct": pct, "status": status_fn(pct), "reason": reason,
              "actor": actor}
        self.events.append(ev)
        return {**self.state, "last_event": ev}

    async def recent_events(self, yard_id, limit=25):
        return list(reversed(self.events))[:limit]

    async def active_holds(self, yard_id=None, limit=500):
        return [h for h in self.rows if h["status"] == "HOLD_AT_PARKING"]

    async def holds(self, *, yard_id=None, status=None, limit=200):
        return [h for h in self.rows if status is None or h["status"] == status]

    async def create_hold(self, row):
        if any(h["device_id"] == row["device_id"] and h["status"] == "HOLD_AT_PARKING"
               for h in self.rows):
            return None
        out = {**row, "id": self._next, "status": "HOLD_AT_PARKING",
               "notified": False, "release_notified": False, "held_at": None,
               "released_at": None, "updated_at": None}
        self._next += 1
        self.rows.append(out)
        return out

    async def mark_notified(self, hold_id, device_id, *, delivered, detail=None,
                            release=False):
        self.notified.append((hold_id, device_id, delivered, release))

    async def release_holds(self, hold_ids, *, actor, reason):
        out = []
        for h in self.rows:
            if h["id"] in set(hold_ids) and h["status"] == "HOLD_AT_PARKING":
                h["status"] = "RELEASED"
                out.append(dict(h))
        return out

    async def hold_events(self, device_id, limit=50):
        return []

    async def latest_hold_for(self, device_id):
        for h in reversed(self.rows):
            if h["device_id"] == device_id:
                return h
        return None


def _service(repo, *, arrivals, alerts=None, pushes=None, frames=None):
    async def arrivals_fn():
        return arrivals

    async def parking_fn():
        return FACILITIES

    async def alert_fn(*, predictions, segment_meta):
        created = [{"alert_id": "alert-1", "type": "TRAFFIC_CONGESTION",
                    "segment_id": next(iter(predictions)), **segment_meta[next(iter(predictions))]}]
        if alerts is not None:
            alerts.append((predictions, segment_meta))
        return created

    async def dispatch_fn(device_id, advisory):
        if pushes is not None:
            pushes.append((device_id, advisory))
        return {"ws": True, "webpush": False, "fcm": False}

    async def broadcast_fn(frame, payload):
        if frames is not None:
            frames.append((frame, payload))

    return YardCapacityService(
        dsn=None, repo=repo,
        thresholds=YardThresholds(high_pct=90.0, critical_pct=95.0, slots_per_truck=2,
                                  release_rate_slots_per_hour=120.0,
                                  preferred_facility_id="PK-CPP"),
        arrivals_fn=arrivals_fn, parking_fn=parking_fn, alert_fn=alert_fn,
        dispatch_fn=dispatch_fn, broadcast_fn=broadcast_fn)


ARRIVALS = {
    "devices": [{"device_id": f"TRK-{i:03d}", "plate": f"MH04XX{i:04d}",
                 "gate_id": "G-NSICT", "eta_s": 300 + i * 120, "source": "truck-sim"}
                for i in range(8)],
    "registered_devices": [{"device_id": "TRK-900001", "plate": "MH05AB1234",
                            "driver_id": "DRV-1", "driver_name": "Test Driver",
                            "eta_s": None, "source": "pwa-registered"}],
    "source": "truck-sim",
}


def test_board_reports_declared_capacity_and_every_required_field():
    repo = FakeRepo()
    out = asyncio.run(_service(repo, arrivals=ARRIVALS).board())
    yard = out["yard"]
    for key in ("utilization_pct", "capacity_slots", "occupied_slots",
                "available_slots", "capacity_status"):
        assert key in yard
    assert yard["utilization_pct"] == 70.0 and yard["capacity_status"] == "NORMAL"
    # The declared denominator is never presented as measured.
    assert yard["capacity_declared"] is True
    assert yard["capacity_source"] == "core.yard_capacity_state"


def test_normal_yard_holds_nobody():
    repo = FakeRepo()
    out = asyncio.run(_service(repo, arrivals=ARRIVALS).evaluate(yard_id=repo.state["yard_id"]))
    assert out["constrained"] is False and out["held"] == [] and repo.rows == []


def test_peak_yard_alerts_holds_recommends_cpp_and_notifies_each_driver():
    repo = FakeRepo(occupied=4560)          # 95.0%
    alerts, pushes, frames = [], [], []
    svc = _service(repo, arrivals=ARRIVALS, alerts=alerts, pushes=pushes, frames=frames)
    out = asyncio.run(svc.evaluate(yard_id=repo.state["yard_id"], actor="tester"))

    assert out["yard"]["utilization_pct"] == 95.0
    assert out["yard"]["capacity_status"] == "CRITICAL"
    assert out["constrained"] is True
    assert out["reason"] == "Yard capacity is currently constrained."
    # At the 95% operating ceiling there is physical space but nothing bookable,
    # so EVERY arriving truck is held — the headline demo behaviour.
    assert out["yard"]["available_slots"] == 240
    assert out["yard"]["headroom_slots"] == 0
    assert len(out["held"]) == 9 and out["proceeding"] == []

    # A yard just under the ceiling admits the nearest cohort and holds the rest.
    repo2 = FakeRepo(occupied=4554)         # 6 bookable slots -> 3 admissible
    alerts, pushes, frames = [], [], []
    svc2 = _service(repo2, arrivals=ARRIVALS, alerts=alerts, pushes=pushes, frames=frames)
    out2 = asyncio.run(svc2.evaluate(yard_id=repo2.state["yard_id"], actor="tester"))

    assert out2["constrained"] is True
    assert len(out2["held"]) == 6 and len(out2["proceeding"]) == 3
    # The TRAFFIC_CONGESTION alert went through the shared congestion service.
    assert alerts and list(alerts[0][0]) == ["YARD-NSICT"]
    assert alerts[0][0]["YARD-NSICT"] > 0.80
    assert out2["alerts"][0]["type"] == "TRAFFIC_CONGESTION"
    # A REAL, authorised facility was recommended — never a fabricated one.
    assert out2["parking"]["facility_id"] == "PK-CPP"
    assert out2["parking"]["is_preferred"] is True
    assert out2["parking"]["estimated_wait_min"] == 6      # 12 slots @120/h
    # Every held driver was pushed the advisory, and each delivery was audited.
    assert len(pushes) == 6 and len(repo2.notified) == 6
    # The RESPONSE must report the delivery too — create_hold returns the row as
    # written (notified=false), so a stale copy would tell the console that zero
    # drivers were reached while the table said every one of them was.
    assert all(h["notified"] for h in out2["held"])
    body = pushes[0][1]["body"]
    assert pushes[0][1]["type"] == ADVISORY_HOLD
    assert "yard capacity is currently at 95%" in body.lower()
    assert "Common Parking Plaza" in body
    # Both provenances participate and stay distinguishable.
    assert {h["source"] for h in out2["held"]} == {"truck-sim", "pwa-registered"}
    # The dashboard is told to refresh.
    assert any(f[0] == "arrival_management" for f in frames)


def test_release_after_capacity_recovery_notifies_and_clears_holds():
    repo = FakeRepo(occupied=4554)
    pushes, frames = [], []
    svc = _service(repo, arrivals=ARRIVALS, pushes=pushes, frames=frames)
    asyncio.run(svc.evaluate(yard_id=repo.state["yard_id"]))
    assert len(repo.rows) == 6

    # "Release 5 containers" -> 10 ground slots freed, still critical.
    asyncio.run(svc.adjust(yard_id=repo.state["yard_id"], delta_slots=-10,
                           event_type="RELEASE", reason="release 5 containers"))
    pushes.clear()
    partial = asyncio.run(svc.release(yard_id=repo.state["yard_id"]))
    assert partial["released_count"] == 8 or partial["released_count"] == 6
    assert all(p[1]["type"] == ADVISORY_RELEASE for p in pushes)
    assert all(r["release_notified"] for r in partial["released"])
    assert "You may now proceed" in pushes[0][1]["body"]
    # The release figure keeps a decimal so it never rounds up to the threshold
    # it is reporting as cleared.
    assert "% utilised" in pushes[0][1]["body"] and "." in pushes[0][1]["body"].split("%")[0]

    # Everything outstanding is gone once the yard drops out of the band.
    asyncio.run(svc.adjust(yard_id=repo.state["yard_id"], target_utilization_pct=70.0))
    rest = asyncio.run(svc.release(yard_id=repo.state["yard_id"]))
    assert asyncio.run(svc.arrival_board(yard_id=repo.state["yard_id"]))["active_count"] == 0
    assert rest["yard"]["capacity_status"] == "NORMAL"


def test_adjust_to_target_utilisation_is_audited():
    repo = FakeRepo()
    svc = _service(repo, arrivals=ARRIVALS)
    out = asyncio.run(svc.adjust(yard_id=repo.state["yard_id"],
                                 target_utilization_pct=95.0, event_type="INCREASE",
                                 reason="demo peak", actor="tester"))
    assert out["yard"]["utilization_pct"] == 95.0
    ev = out["event"]
    assert ev["event_type"] == "INCREASE" and ev["occupied_before"] == 3360
    assert ev["occupied_after"] == 4560 and ev["actor"] == "tester"
    assert ev["status"] == "CRITICAL"


def test_dry_run_writes_nothing():
    repo = FakeRepo(occupied=4554)
    svc = _service(repo, arrivals=ARRIVALS)
    out = asyncio.run(svc.evaluate(yard_id=repo.state["yard_id"], dry_run=True))
    assert out["dry_run"] is True and out["would_hold"] and repo.rows == []


def test_queue_outage_degrades_instead_of_raising():
    repo = FakeRepo(occupied=4554)

    async def boom():
        raise RuntimeError("truck-sim unreachable")

    svc = _service(repo, arrivals=ARRIVALS)
    svc._arrivals = boom                                  # noqa: SLF001
    out = asyncio.run(svc.evaluate(yard_id=repo.state["yard_id"]))
    assert out["arrivals"]["total"] == 0 and out["held"] == []


# --------------------------------------------------- evaluate's queue read
def test_read_gate_queue_retries_past_a_degraded_empty_answer(monkeypatch):
    """The live TFC-4 failure: a degraded/stale fleet-list answer right after
    injection must be retried, not recorded as 'nobody is arriving'."""
    from gateway.routers import trucks as trucks_router
    from gateway.routers import yard as yard_router

    answers = [
        # attempt 1: the pre-injection snapshot / timed-out probe
        {"count": 0, "devices": [], "degraded": True, "decision_path": None},
        # attempt 2: the sim answers with the injected trucks
        {"count": 2, "devices": [{"device_id": "SYN-A"}, {"device_id": "SYN-B"}],
         "degraded": False, "decision_path": "PRIMARY"},
    ]
    calls = []

    async def fake_list(*, state, limit, gw):
        calls.append((state, limit))
        return answers[min(len(calls) - 1, len(answers) - 1)]

    monkeypatch.setattr(trucks_router, "list_trucks", fake_list)
    body = asyncio.run(yard_router.read_gate_queue(object(), delay_s=0.0))
    assert [d["device_id"] for d in body["devices"]] == ["SYN-A", "SYN-B"]
    assert len(calls) == 2
    # The read must use its own wide memo key, never the consoles' (500) one —
    # sharing that key is exactly what served evaluate a pre-injection snapshot.
    assert all(limit == yard_router.QUEUE_READ_LIMIT == 2000 for _, limit in calls)


def test_read_gate_queue_accepts_an_honest_empty_first_answer(monkeypatch):
    """PRIMARY answering 'queue is empty' is a measurement — no retry loop."""
    from gateway.routers import trucks as trucks_router
    from gateway.routers import yard as yard_router

    calls = []

    async def fake_list(*, state, limit, gw):
        calls.append(1)
        return {"count": 0, "devices": [], "degraded": False,
                "decision_path": "PRIMARY"}

    monkeypatch.setattr(trucks_router, "list_trucks", fake_list)
    body = asyncio.run(yard_router.read_gate_queue(object(), delay_s=0.0))
    assert body["devices"] == [] and len(calls) == 1


def test_read_gate_queue_returns_the_degraded_answer_after_all_attempts(monkeypatch):
    from gateway.routers import trucks as trucks_router
    from gateway.routers import yard as yard_router

    calls = []

    async def fake_list(*, state, limit, gw):
        calls.append(1)
        return {"count": 0, "devices": [], "degraded": True, "decision_path": None}

    monkeypatch.setattr(trucks_router, "list_trucks", fake_list)
    body = asyncio.run(yard_router.read_gate_queue(object(), attempts=3, delay_s=0.0))
    assert body["degraded"] is True and len(calls) == 3


# ------------------------------------------------------------ RBAC + wiring
def test_routes_are_registered_and_writes_are_control_room_only():
    from gateway import auth
    from gateway.main import app

    paths = {r.path for r in app.routes}
    assert "/api/yard/capacity/board" in paths
    assert "/api/yard/capacity/{yard_id}/evaluate" in paths
    assert "/api/yard/arrivals/holds" in paths

    write_roles = auth.roles_for("/api/yard/capacity/Y/adjust", "POST")
    assert "DRIVER" not in write_roles and "CUSTOMS" not in write_roles
    # The pre-existing job surface keeps its own audience (no regression).
    assert "CUSTOMS" in auth.roles_for("/api/yard/movements", "POST")
    # A driver may poll its OWN hold, and only its own.
    assert "DRIVER" in auth.roles_for("/api/yard/arrivals/holds/TRK-1", "GET")
    assert auth.driver_scope_violation("/api/yard/arrivals/holds/TRK-1", "TRK-1") is None
    assert auth.driver_scope_violation("/api/yard/arrivals/holds/TRK-2", "TRK-1")
    assert auth.driver_scope_violation("/api/yard/arrivals/holds", "TRK-1")
