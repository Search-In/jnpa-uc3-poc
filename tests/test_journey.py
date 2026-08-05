"""Tests for the Follow-the-Box journey surface (/api/journey) — now DB-backed.

The journey endpoint no longer fabricates container data: it resolves the box
against the SAME ``CargoService`` that serves ``/api/cargo`` (swapped here for an
in-memory fake through ``app.dependency_overrides`` — the one DI seam shared by
both routers) and populates every UC-II / UC-III stage from the live cargo row.

Covers the "replace mock with live cargo" contract: live field pass-through,
no ``data_mode="mock"`` / ``simulated=True`` anywhere, the preserved response
schema (the nine lifecycle stages + cross_twin + journey_status + ids), and the
not-in-registry behaviour.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from starlette.testclient import TestClient  # noqa: E402

from jnpa_shared.iso6346 import is_valid_container_no  # noqa: E402
from services.cargo import CargoService  # noqa: E402

# GESU5123996 is a check-digit-valid ISO-6346 box (also the demo seed record).
CN = "GESU5123996"
BAD_CN = "MAEU6123450"  # right shape, wrong check digit
MISSING_CN = "MSCU7789010"  # valid ISO, deliberately not created
assert is_valid_container_no(CN) and not is_valid_container_no(BAD_CN)

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

# The eleven lifecycle stages the Follow-the-Box UI depends on (response contract).
# gate_crossing (=gate entry) is now followed by the LIVE parking_assignment and
# gate_exit stages before eta_tracking (UC-3 audit R3 — full Follow-the-Box).
_STAGE_KEYS = [
    "vessel_discharge", "yard_movement", "dpd_release", "cross_twin_published",
    "cross_twin_received", "truck_assignment", "anpr_detection", "gate_crossing",
    "parking_assignment", "gate_exit", "eta_tracking",
]


class FakeCargoRepo:
    """Minimal in-memory CargoRepository stand-in (only what journey reads)."""

    def __init__(self, rows: Optional[list[dict]] = None) -> None:
        self._rows = {r["container_number"]: r for r in (rows or [])}

    async def get(self, container_number: str) -> Optional[dict]:
        r = self._rows.get(container_number)
        return dict(r) if r else None


def _cargo_row(**over: Any) -> dict:
    base = {
        "container_number": CN,
        "vessel_name": "COSCO SHIPPING ARIES",
        "customs_status": "CLEARED",
        "yard_block": "A-01",
        "is_released": True,
        "vehicle_number": "MH43ST7788",
        "gate": "GATE-4",
        "camera_id": "CAM-ANPR-04",
        "eta": datetime(2026, 7, 12, 8, 30, tzinfo=timezone.utc),
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(over)
    return base


@pytest.fixture()
def client_with():
    """Factory: fresh TestClient with the cargo service backed by given rows."""
    from gateway.main import app
    from gateway.routers import cargo as cargo_router

    def _make(rows: list[dict]):
        svc = CargoService(repository=FakeCargoRepo(rows))
        app.dependency_overrides[cargo_router.get_service] = lambda: svc
        return TestClient(app)

    yield _make
    from gateway.routers import cargo as cargo_router
    app.dependency_overrides.pop(cargo_router.get_service, None)


def _facts(stages: list[dict], stage: str) -> dict:
    return next(s for s in stages if s["stage"] == stage)["facts"]


def test_journey_reflects_live_cargo(client_with):
    """Every exposed value must come from the cargo row, not a hash."""
    with client_with([_cargo_row()]) as c:
        r = c.get(f"/api/journey/container/{CN}")
    assert r.status_code == 200, r.text
    j = r.json()

    assert j["found"] is True
    assert j["container_no"] == CN
    # Live pass-through of the cargo fields the task enumerates.
    assert j["vehicle_no"] == "MH43ST7788"
    assert j["gate"] == "GATE-4"
    assert _facts(j["stages"], "vessel_discharge")["vessel"] == "COSCO SHIPPING ARIES"
    assert _facts(j["stages"], "yard_movement")["yard_block"] == "A-01"
    assert _facts(j["stages"], "dpd_release")["customs"] == "CLEARED"
    assert _facts(j["stages"], "dpd_release")["is_released"] is True
    assert _facts(j["stages"], "truck_assignment")["vehicle_no"] == "MH43ST7788"
    assert _facts(j["stages"], "truck_assignment")["gate"] == "GATE-4"


def test_journey_has_no_mock_indicators(client_with):
    with client_with([_cargo_row()]) as c:
        j = c.get(f"/api/journey/container/{CN}").json()

    assert j["data_mode"] == "live"
    assert j["cross_twin"]["simulated"] is False
    # No stage may carry data_mode="mock" or a simulated=True fact.
    for s in j["stages"]:
        assert s["data_mode"] == "live"
        assert s["source"] in ("live", "gate-data")
        assert s.get("facts", {}).get("simulated") is not True
    # Nowhere in the whole response body.
    import json as _json
    blob = _json.dumps(j)
    assert '"mock"' not in blob
    assert '"simulated": true' not in blob


def test_response_contract_preserved(client_with):
    with client_with([_cargo_row()]) as c:
        j = c.get(f"/api/journey/container/{CN}").json()

    # The nine lifecycle stages are all still present and ordered.
    assert [s["stage"] for s in j["stages"]] == _STAGE_KEYS
    assert [s["key"] for s in j["journey_status"]] == _STAGE_KEYS
    # Ids preserved (deterministic per box).
    assert j["correlation_id"].startswith("XT-")
    assert j["case_id"].startswith("CASE-")
    assert all(s["event_id"].startswith("EVT-") for s in j["stages"])
    # cross_twin handoff object intact.
    x = j["cross_twin"]
    assert x["topic"] == "cargo.dpd_release"
    assert x["publishing_twin"] == "UC-II" and x["receiving_twin"] == "UC-III"
    assert x["status"] == "Delivered"  # released box


def test_journey_status_reflects_release_flag(client_with):
    # A held, not-yet-released box: downstream steps are not done.
    with client_with([_cargo_row(customs_status="UNDER_INSPECTION",
                                 is_released=False)]) as c:
        j = c.get(f"/api/journey/container/{CN}").json()
    done = {s["key"]: s["done"] for s in j["journey_status"]}
    assert done["vessel_discharge"] is True
    assert done["yard_movement"] is True
    assert done["dpd_release"] is False
    assert done["cross_twin_published"] is False
    assert done["truck_assignment"] is False
    assert j["cross_twin"]["status"] == "Pending"


def test_container_not_in_registry(client_with):
    with client_with([_cargo_row()]) as c:
        j = c.get(f"/api/journey/container/{MISSING_CN}").json()
    assert j["found"] is False
    assert j["stages"] == []
    assert j["data_mode"] == "live"
    assert j["cross_twin"] is None
    assert all(s["done"] is False for s in j["journey_status"])


def test_invalid_iso_reports_not_found(client_with):
    with client_with([_cargo_row()]) as c:
        j = c.get(f"/api/journey/container/{BAD_CN}").json()
    assert j["iso6346_valid"] is False
    assert j["found"] is False


def test_journey_includes_parking_and_exit_stages(monkeypatch, client_with):
    """UC-3 R3: the LIVE parking-assignment + gate-exit join populates the two new
    stages and flips their journey_status steps to done."""
    from gateway.routers import journey as journey_mod

    async def fake_fetch(_state, _plate):
        parking = {"facility_id": "P-NSICT", "slot_id": 42, "entry_time": _NOW,
                   "exit_time": None, "status": "ACTIVE"}
        exit_row = {"ts": _NOW, "gate_id": "GATE-4"}
        return parking, exit_row

    monkeypatch.setattr(journey_mod, "_fetch_parking_exit", fake_fetch)
    with client_with([_cargo_row()]) as c:
        j = c.get(f"/api/journey/container/{CN}").json()

    stages = {s["stage"]: s for s in j["stages"]}
    assert "parking_assignment" in stages and "gate_exit" in stages
    assert stages["parking_assignment"]["facts"]["facility_id"] == "P-NSICT"
    assert stages["parking_assignment"]["facts"]["slot_id"] == 42
    assert stages["gate_exit"]["facts"]["gate"] == "GATE-4"
    # Both new stages are LIVE-sourced (DB join), not simulated.
    assert stages["parking_assignment"]["source"] == "live"
    assert stages["gate_exit"]["source"] == "live"
    done = {s["key"]: s["done"] for s in j["journey_status"]}
    assert done["parking_assignment"] is True
    assert done["gate_exit"] is True


def test_journey_parking_exit_absent_when_no_records(client_with):
    """No parking/gate-out on record (dummy DSN unreachable) -> stages present but
    not done, and the box's journey still renders end-to-end."""
    with client_with([_cargo_row()]) as c:
        j = c.get(f"/api/journey/container/{CN}").json()
    done = {s["key"]: s["done"] for s in j["journey_status"]}
    assert done["parking_assignment"] is False
    assert done["gate_exit"] is False
    # The stages are still in the ordered timeline (UI shows an "awaiting" state).
    assert [s["stage"] for s in j["stages"]] == _STAGE_KEYS
