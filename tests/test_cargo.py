"""Tests for the Cargo CRUD surface (/api/cargo) — POC-3 common backend.

Two layers, mirroring the rest of the suite (test_gateway / test_empty_container):

* Pure validation checks on the ISO-6346 PK + DTO layer (no server, no DB).
* Full router CRUD via Starlette's TestClient with the DB repository swapped for
  an in-memory fake through ``app.dependency_overrides`` — so the router logic
  (201/200/400/404/409, filtering, patch semantics) is exercised deterministically
  with no Postgres. A final integration test hits a REAL DB and is skipped
  automatically when Postgres is unreachable (compose publishes it on host 5433).

Covers the Phase-6 checklist: Create · Get-All · Get-One · Update · Delete ·
Duplicate container · Invalid ISO · Missing record · Invalid payload.
"""
from __future__ import annotations

import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Unroutable DSN so any accidental real-DB path fails fast (the fake bypasses it).
os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from starlette.testclient import TestClient  # noqa: E402

from jnpa_shared.iso6346 import is_valid_container_no, with_check_digit  # noqa: E402
from services.cargo import (  # noqa: E402
    CargoConflict,
    CargoNotFound,
    CargoService,
    CargoTransitionError,
)
from services.cargo.repository import _infer_lifecycle  # noqa: E402

# A valid + an invalid ISO-6346 number reused across the tests.
VALID_CN = "MAEU6123458"
BAD_CN = "MAEU6123450"  # right shape, wrong check digit
assert is_valid_container_no(VALID_CN) and not is_valid_container_no(BAD_CN)

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_WRITABLE = ("vessel_name", "customs_status", "yard_block", "is_released",
             "vehicle_number", "gate", "camera_id", "eta",
             "eseal_status", "eseal_number", "pre_document_status", "origin_stream")


def _enum_val(v: Any) -> Any:
    return v.value if hasattr(v, "value") else v


class FakeCargoRepo:
    """In-memory stand-in for CargoRepository with identical method contracts."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}
        self._events: list[dict] = []
        self._event_seq = 0
        self._notifications: list[dict] = []
        self._notif_seq = 0
        self._workflow: list[dict] = []
        self._workflow_seq = 0
        self._yard_plans: list[dict] = []
        self._yard_plan_seq = 0
        self._rake_plans: list[dict] = []
        self._rake_plan_seq = 0
        self._reefer_plans: list[dict] = []
        self._reefer_plan_seq = 0
        self._lifecycle: list[dict] = []
        self._lifecycle_seq = 0
        self._scan_verifs: list[dict] = []
        self._scan_verif_seq = 0

    async def create(self, row: Mapping[str, Any]) -> dict:
        cn = row["container_number"]
        if cn in self._rows:
            raise CargoConflict(cn)
        rec = {
            "container_number": cn,
            "vessel_name": row.get("vessel_name"),
            "customs_status": _enum_val(row.get("customs_status")) or "PENDING",
            "yard_block": row.get("yard_block"),
            "is_released": bool(row.get("is_released") or False),
            "vehicle_number": row.get("vehicle_number"),
            "gate": row.get("gate"),
            "camera_id": row.get("camera_id"),
            "eta": row.get("eta"),
            "eseal_status": _enum_val(row.get("eseal_status")),
            "eseal_number": row.get("eseal_number"),
            "pre_document_status": _enum_val(row.get("pre_document_status")),
            "origin_stream": row.get("origin_stream"),
            "lifecycle_status": "CREATED",
            "created_at": _NOW,
            "updated_at": _NOW,
        }
        self._rows[cn] = rec
        return dict(rec)

    async def get(self, container_number: str) -> Optional[dict]:
        r = self._rows.get(container_number)
        return dict(r) if r else None

    def _filtered(self, *, container_number=None, customs_status=None,
                  yard_block=None, is_released=None, vehicle_number=None,
                  eseal_status=None, pre_document_status=None,
                  origin_stream=None, lifecycle_status=None) -> list[dict]:
        rows = list(self._rows.values())
        eq = {"container_number": container_number, "customs_status": customs_status,
              "yard_block": yard_block, "is_released": is_released,
              "vehicle_number": vehicle_number, "eseal_status": eseal_status,
              "pre_document_status": pre_document_status, "origin_stream": origin_stream,
              "lifecycle_status": lifecycle_status}
        for col, val in eq.items():
            if val is not None:
                rows = [r for r in rows if r[col] == val]
        return rows

    async def list(self, *, limit=100, offset=0, **filters) -> list[dict]:
        rows = self._filtered(**filters)
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return [dict(r) for r in rows[offset:offset + limit]]

    async def count(self, **filters) -> int:
        return len(self._filtered(**filters))

    async def update(self, container_number: str, fields: Mapping[str, Any]) -> dict:
        if container_number not in self._rows:
            raise CargoNotFound(container_number)
        rec = self._rows[container_number]
        for k, v in fields.items():
            if k in _WRITABLE:
                rec[k] = _enum_val(v) if k == "customs_status" else v
        rec["updated_at"] = datetime(2026, 1, 2, tzinfo=timezone.utc)
        return dict(rec)

    async def delete(self, container_number: str) -> bool:
        return self._rows.pop(container_number, None) is not None

    async def record_event(self, event: str, container_number: str, payload) -> dict:
        self._event_seq += 1
        rec = {"id": self._event_seq, "event": event,
               "container_number": container_number, "payload": dict(payload or {}),
               "created_at": _NOW}
        self._events.append(rec)
        return dict(rec)

    async def list_events(self, *, container_number=None, event=None, since_id=None,
                          limit=100, offset=0) -> list[dict]:
        rows = list(self._events)
        if container_number is not None:
            rows = [r for r in rows if r["container_number"] == container_number]
        if event is not None:
            rows = [r for r in rows if r["event"] == event]
        if since_id is not None:
            rows = [r for r in rows if r["id"] > since_id]
        rows.sort(key=lambda r: r["id"], reverse=True)
        return [dict(r) for r in rows[offset:offset + limit]]

    # ----- notifications (0017) -----
    async def create_notification(self, container_number, notification_type, severity,
                                  message, stakeholders) -> dict:
        self._notif_seq += 1
        rec = {"id": self._notif_seq, "container_number": container_number,
               "notification_type": notification_type, "severity": severity,
               "message": message, "stakeholders": list(stakeholders or []),
               "status": "CREATED", "created_at": _NOW}
        self._notifications.append(rec)
        return dict(rec)

    async def list_notifications(self, *, container_number=None, notification_type=None,
                                 severity=None, status=None, limit=100, offset=0) -> list[dict]:
        rows = list(self._notifications)
        eq = {"container_number": container_number, "notification_type": notification_type,
              "severity": severity, "status": status}
        for col, val in eq.items():
            if val is not None:
                rows = [r for r in rows if r[col] == val]
        rows.sort(key=lambda r: r["id"], reverse=True)
        return [dict(r) for r in rows[offset:offset + limit]]

    # ----- workflow (0016) -----
    async def record_workflow(self, container_number, action, new_status, comment):
        if container_number not in self._rows:
            return None
        old_status = self._rows[container_number].get("workflow_status")
        self._rows[container_number]["workflow_status"] = new_status
        self._workflow_seq += 1
        rec = {"id": self._workflow_seq, "container_number": container_number,
               "action": action, "old_status": old_status, "new_status": new_status,
               "comment": comment, "created_at": _NOW}
        self._workflow.append(rec)
        return dict(rec)

    async def list_workflow_history(self, container_number, *, limit=100, offset=0) -> list[dict]:
        rows = [r for r in self._workflow if r["container_number"] == container_number]
        rows.sort(key=lambda r: r["id"], reverse=True)
        return [dict(r) for r in rows[offset:offset + limit]]

    # ----- planning (0018) -----
    async def next_yard_slot(self, block) -> int:
        pref = f"{block}-"
        occ = sum(1 for r in self._rows.values()
                  if (r.get("yard_block") or "").startswith(pref))
        planned = sum(1 for p in self._yard_plans
                      if p["assigned_block"].startswith(pref))
        return occ + planned + 1

    async def create_yard_plan(self, container_number, preferred_block,
                               assigned_block, priority) -> dict:
        self._yard_plan_seq += 1
        rec = {"id": self._yard_plan_seq, "container_number": container_number,
               "preferred_block": preferred_block, "assigned_block": assigned_block,
               "priority": priority, "status": "PLANNED", "created_at": _NOW}
        self._yard_plans.append(rec)
        return dict(rec)

    async def list_yarded_containers(self) -> list[dict]:
        return [{"container_number": cn, "yard_block": r.get("yard_block")}
                for cn, r in self._rows.items() if r.get("yard_block")]

    async def create_rake_plan(self, rake_id, containers) -> dict:
        items = list(containers or [])
        self._rake_plan_seq += 1
        rec = {"id": self._rake_plan_seq, "rake_id": rake_id, "containers": items,
               "planned_containers": len(items), "status": "PLANNED", "created_at": _NOW}
        self._rake_plans.append(rec)
        return dict(rec)

    async def list_rake_plans(self, *, rake_id=None, limit=100, offset=0) -> list[dict]:
        rows = list(self._rake_plans)
        if rake_id is not None:
            rows = [r for r in rows if r["rake_id"] == rake_id]
        rows.sort(key=lambda r: r["id"], reverse=True)
        return [dict(r) for r in rows[offset:offset + limit]]

    async def next_reefer_index(self) -> int:
        return len(self._reefer_plans) + 1

    async def create_reefer_plan(self, container_number, temperature,
                                 power_required, slot) -> dict:
        self._reefer_plan_seq += 1
        rec = {"id": self._reefer_plan_seq, "container_number": container_number,
               "temperature": temperature, "power_required": power_required,
               "slot": slot, "status": "ALLOCATED", "created_at": _NOW}
        self._reefer_plans.append(rec)
        return dict(rec)

    # ----- lifecycle (0023) -----
    async def transition_lifecycle(self, container_number, *, target, allowed_from,
                                   action, actor_role=None, note=None, strict=True):
        rec = self._rows.get(container_number)
        if rec is None:
            if strict:
                raise CargoNotFound(container_number)
            return None
        current = _infer_lifecycle(rec)
        if current not in allowed_from:
            if strict:
                raise CargoTransitionError(container_number, current, target)
            return None
        rec["lifecycle_status"] = target
        self._lifecycle_seq += 1
        self._lifecycle.append({
            "id": self._lifecycle_seq, "container_number": container_number,
            "action": action, "old_status": current, "new_status": target,
            "actor_role": actor_role, "note": note, "created_at": _NOW})
        out = dict(rec)
        out["_old_status"] = current
        out["_new_status"] = target
        return out

    async def list_lifecycle_events(self, container_number, *, limit=100, offset=0):
        rows = [r for r in self._lifecycle if r["container_number"] == container_number]
        rows.sort(key=lambda r: r["id"], reverse=True)
        return [dict(r) for r in rows[offset:offset + limit]]

    async def list_scan_queue(self, *, limit=100, offset=0) -> list[dict]:
        yard_states = {"YARD_ASSIGNED", "YARD_POSITION_ALLOCATED", "REEFER_PLANNED",
                       "RAKE_ASSIGNED", "SCAN_PENDING"}
        rows = [r for r in self._rows.values()
                if not r.get("is_released")
                and (r.get("yard_block") or r.get("lifecycle_status") in yard_states)
                and (r.get("lifecycle_status") or "") not in ("VERIFIED", "RELEASED")]
        rows.sort(key=lambda r: (r["created_at"], r["container_number"]))
        return [dict(r) for r in rows[offset:offset + limit]]

    async def record_scan_verification(self, container_number, verified, remarks,
                                       actor_role) -> dict:
        self._scan_verif_seq += 1
        rec = {"id": self._scan_verif_seq, "container_number": container_number,
               "verified": verified, "remarks": remarks, "actor_role": actor_role,
               "created_at": _NOW}
        self._scan_verifs.append(rec)
        return dict(rec)

    async def create_yard_position(self, container_number, *, assigned_block,
                                   yard_row, yard_slot, yard_position, priority) -> dict:
        self._yard_plan_seq += 1
        rec = {"id": self._yard_plan_seq, "container_number": container_number,
               "preferred_block": None, "assigned_block": assigned_block,
               "priority": priority, "status": "PLANNED", "yard_row": yard_row,
               "yard_slot": yard_slot, "yard_position": yard_position,
               "created_at": _NOW}
        self._yard_plans.append(rec)
        return dict(rec)


@pytest.fixture()
def client():
    """Fresh app-bound TestClient with the cargo service backed by a fake repo."""
    from gateway.main import app
    from gateway.routers import cargo as cargo_router

    fake_service = CargoService(repository=FakeCargoRepo())
    app.dependency_overrides[cargo_router.get_service] = lambda: fake_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(cargo_router.get_service, None)


def _payload(**over) -> dict:
    base = {
        "container_number": VALID_CN, "vessel_name": "MAERSK SEMBAWANG",
        "customs_status": "PENDING", "yard_block": "A-01", "is_released": False,
        "vehicle_number": "MH04AB1234", "gate": "GATE-1", "camera_id": "CAM-ANPR-01",
        "eta": "2026-07-12T08:30:00Z",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------- pure layer
def test_iso6346_pk_validation():
    assert is_valid_container_no(with_check_digit("MSCU778901"))
    assert not is_valid_container_no("NOTACONTAINER")
    assert not is_valid_container_no(BAD_CN)


# ------------------------------------------------------------------- CRUD (fake)
def test_create_returns_201(client):
    r = client.post("/api/cargo", json=_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["container_number"] == VALID_CN
    assert body["customs_status"] == "PENDING"
    assert body["is_released"] is False
    assert "created_at" in body and "updated_at" in body


def test_get_all(client):
    client.post("/api/cargo", json=_payload())
    client.post("/api/cargo", json=_payload(container_number="MSCU7789010",
                                            customs_status="CLEARED", is_released=True))
    r = client.get("/api/cargo")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    # filter works
    r2 = client.get("/api/cargo", params={"customs_status": "CLEARED"})
    assert [x["container_number"] for x in r2.json()] == ["MSCU7789010"]
    r3 = client.get("/api/cargo", params={"is_released": "true"})
    assert [x["container_number"] for x in r3.json()] == ["MSCU7789010"]
    # X-Total-Count reflects the full (pre-pagination) match count.
    assert r.headers.get("X-Total-Count") == "2"
    assert r2.headers.get("X-Total-Count") == "1"


def test_filters_and_pagination(client):
    client.post("/api/cargo", json=_payload(yard_block="A-01"))
    client.post("/api/cargo", json=_payload(container_number="MSCU7789010",
                                            yard_block="B-02", vehicle_number="MH05CD4567"))
    # yard_block filter
    assert [x["container_number"] for x in
            client.get("/api/cargo", params={"yard_block": "B-02"}).json()] == ["MSCU7789010"]
    # vehicle_number filter (normalised — spaces/case)
    assert [x["container_number"] for x in
            client.get("/api/cargo", params={"vehicle_number": "mh05 cd4567"}).json()] == ["MSCU7789010"]
    # container_number exact filter
    assert len(client.get("/api/cargo", params={"container_number": VALID_CN}).json()) == 1
    # bad ISO in the container_number filter -> 400
    assert client.get("/api/cargo", params={"container_number": BAD_CN}).status_code == 400
    # pagination: limit caps the page, X-Total-Count keeps the full size
    page = client.get("/api/cargo", params={"limit": 1, "offset": 0})
    assert len(page.json()) == 1
    assert page.headers.get("X-Total-Count") == "2"


def test_get_one(client):
    client.post("/api/cargo", json=_payload())
    r = client.get(f"/api/cargo/{VALID_CN}")
    assert r.status_code == 200
    assert r.json()["container_number"] == VALID_CN
    # case/space-insensitive lookup on the PK
    r2 = client.get("/api/cargo/maeu 6123458")
    assert r2.status_code == 200


def test_update(client):
    client.post("/api/cargo", json=_payload())
    r = client.put(f"/api/cargo/{VALID_CN}",
                   json={"customs_status": "CLEARED", "is_released": True,
                         "yard_block": "B-09"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["customs_status"] == "CLEARED"
    assert body["is_released"] is True
    assert body["yard_block"] == "B-09"
    # unchanged fields preserved
    assert body["vessel_name"] == "MAERSK SEMBAWANG"


def test_delete(client):
    client.post("/api/cargo", json=_payload())
    r = client.delete(f"/api/cargo/{VALID_CN}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    # gone now
    assert client.get(f"/api/cargo/{VALID_CN}").status_code == 404


def test_duplicate_container_409(client):
    assert client.post("/api/cargo", json=_payload()).status_code == 201
    r = client.post("/api/cargo", json=_payload())
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "duplicate_container"


def test_invalid_iso_400(client):
    r = client.post("/api/cargo", json=_payload(container_number=BAD_CN))
    assert r.status_code == 400
    assert r.json()["error"] == "validation_error"


def test_missing_record_404(client):
    # A valid, well-formed container that was never created.
    r = client.get("/api/cargo/MSCU7789010")
    assert r.status_code == 404
    assert client.put("/api/cargo/MSCU7789010", json={"is_released": True}).status_code == 404
    assert client.delete("/api/cargo/MSCU7789010").status_code == 404


def test_invalid_payload_400(client):
    # Bad enum value.
    assert client.post("/api/cargo", json=_payload(customs_status="NOT_A_STATUS")).status_code == 400
    # Wrong type for is_released.
    assert client.post("/api/cargo", json=_payload(is_released="banana")).status_code == 400
    # Bad ISO on a PUT path.
    assert client.put(f"/api/cargo/{BAD_CN}", json={"is_released": True}).status_code == 400
    # Missing required container_number.
    p = _payload()
    p.pop("container_number")
    assert client.post("/api/cargo", json=p).status_code == 400


# ------------------------------------------------------------- yard assignment
def test_yard_assignment_success(client):
    client.post("/api/cargo", json=_payload(yard_block=None))
    r = client.put(f"/api/cargo/{VALID_CN}/yard-assignment", json={"yard_block": "A-01"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"container_number": VALID_CN, "yard_block": "A-01", "status": "ASSIGNED"}


def test_yard_assignment_normalises_block(client):
    client.post("/api/cargo", json=_payload())
    # lower-case is normalised to the canonical upper form.
    r = client.put(f"/api/cargo/{VALID_CN}/yard-assignment", json={"yard_block": "b-02"})
    assert r.status_code == 200
    assert r.json()["yard_block"] == "B-02"


def test_yard_assignment_persists(client):
    """The assigned block is durably written — a follow-up GET reflects it."""
    client.post("/api/cargo", json=_payload(yard_block="A-01"))
    assert client.put(f"/api/cargo/{VALID_CN}/yard-assignment",
                      json={"yard_block": "C-07"}).status_code == 200
    got = client.get(f"/api/cargo/{VALID_CN}")
    assert got.status_code == 200
    assert got.json()["yard_block"] == "C-07"


def test_yard_assignment_container_not_found_404(client):
    # Valid, well-formed container that was never created.
    r = client.put("/api/cargo/MSCU7789010/yard-assignment", json={"yard_block": "A-01"})
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "not_found"


def test_yard_assignment_invalid_payload_400(client):
    client.post("/api/cargo", json=_payload())
    # Malformed block shape.
    assert client.put(f"/api/cargo/{VALID_CN}/yard-assignment",
                      json={"yard_block": "not a block"}).status_code == 400
    # Empty block.
    assert client.put(f"/api/cargo/{VALID_CN}/yard-assignment",
                      json={"yard_block": ""}).status_code == 400
    # Missing yard_block field.
    assert client.put(f"/api/cargo/{VALID_CN}/yard-assignment", json={}).status_code == 400
    # Bad ISO-6346 on the path -> 400 (never 500).
    assert client.put(f"/api/cargo/{BAD_CN}/yard-assignment",
                      json={"yard_block": "A-01"}).status_code == 400


# --------------------------------------------- contract extensions (0015 fields)
def test_new_fields_roundtrip_on_create(client):
    """e-Seal / pre-document / origin_stream persist through create + get."""
    r = client.post("/api/cargo", json=_payload(
        eseal_status="ACTIVE", eseal_number="ES-88213",
        pre_document_status="COMPLETED", origin_stream="UC-II"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["eseal_status"] == "ACTIVE"
    assert body["eseal_number"] == "ES-88213"
    assert body["pre_document_status"] == "COMPLETED"
    assert body["origin_stream"] == "UC-II"
    # And on a follow-up GET.
    got = client.get(f"/api/cargo/{VALID_CN}").json()
    assert got["eseal_status"] == "ACTIVE" and got["origin_stream"] == "UC-II"


def test_new_fields_default_null_backward_compatible(client):
    """A create WITHOUT the new fields still succeeds; the fields serialise null —
    the existing UC-2 contract is unchanged."""
    body = client.post("/api/cargo", json=_payload()).json()
    for k in ("eseal_status", "eseal_number", "pre_document_status", "origin_stream"):
        assert k in body and body[k] is None
    # Every legacy field is still present.
    for k in ("container_number", "vessel_name", "customs_status", "yard_block",
              "is_released", "vehicle_number", "gate", "camera_id", "eta",
              "created_at", "updated_at"):
        assert k in body


def test_origin_stream_camelcase_alias_accepted(client):
    """Input may use camelCase ``originStream``; output is always ``origin_stream``."""
    r = client.post("/api/cargo", json=_payload(originStream="UC-II"))
    assert r.status_code == 201, r.text
    assert r.json()["origin_stream"] == "UC-II"
    assert client.put(f"/api/cargo/{VALID_CN}",
                      json={"originStream": "UC-III"}).json()["origin_stream"] == "UC-III"


def test_new_field_filters(client):
    client.post("/api/cargo", json=_payload(origin_stream="UC-II",
                                            eseal_status="ACTIVE",
                                            pre_document_status="COMPLETED"))
    client.post("/api/cargo", json=_payload(container_number="MSCU7789010",
                                            origin_stream="UC-III",
                                            eseal_status="ARMED",
                                            pre_document_status="PENDING"))
    assert [x["container_number"] for x in
            client.get("/api/cargo", params={"origin_stream": "UC-III"}).json()] == ["MSCU7789010"]
    assert [x["container_number"] for x in
            client.get("/api/cargo", params={"eseal_status": "ACTIVE"}).json()] == [VALID_CN]
    assert [x["container_number"] for x in
            client.get("/api/cargo", params={"pre_document_status": "PENDING"}).json()] == ["MSCU7789010"]


def test_invalid_new_enum_400(client):
    assert client.post("/api/cargo", json=_payload(eseal_status="BOGUS")).status_code == 400
    assert client.post("/api/cargo", json=_payload(pre_document_status="BOGUS")).status_code == 400


# ------------------------------------------------------------- role-based filtering
def test_role_filtering_scopes_visibility(client):
    # One released + one held/unreleased box.
    client.post("/api/cargo", json=_payload(is_released=True))
    client.post("/api/cargo", json=_payload(container_number="MSCU7789010",
                                            is_released=False, customs_status="HELD"))
    # operator / control room see everything (existing contract unchanged).
    assert len(client.get("/api/cargo", params={"role": "operator"}).json()) == 2
    assert len(client.get("/api/cargo").json()) == 2  # no role -> all
    # driver only sees released boxes (ready for haulage).
    driver = client.get("/api/cargo", params={"role": "driver"}).json()
    assert [x["container_number"] for x in driver] == [VALID_CN]
    assert driver[0]["is_released"] is True
    # customs only sees the pre-release pipeline.
    customs = client.get("/api/cargo", params={"role": "customs"}).json()
    assert [x["container_number"] for x in customs] == ["MSCU7789010"]


def test_role_scope_overrides_conflicting_filter(client):
    """A role's scope is a hard constraint — it wins over a conflicting client
    filter (a driver cannot ask to see unreleased boxes)."""
    client.post("/api/cargo", json=_payload(is_released=True))
    client.post("/api/cargo", json=_payload(container_number="MSCU7789010", is_released=False))
    rows = client.get("/api/cargo", params={"role": "driver", "is_released": "false"}).json()
    assert [x["container_number"] for x in rows] == [VALID_CN]  # released only, role wins


# ---------------------------------------------------- notifications (event log)
def test_events_emitted_on_lifecycle(client):
    # created
    client.post("/api/cargo", json=_payload(is_released=False, customs_status="PENDING"))
    # yard_assigned + status_changed + released in one PUT
    client.put(f"/api/cargo/{VALID_CN}",
               json={"customs_status": "CLEARED", "is_released": True, "yard_block": "B-09"})
    # gate movement
    client.put(f"/api/cargo/{VALID_CN}", json={"gate": "GATE-7"})
    events = client.get("/api/cargo/events").json()
    kinds = {e["event"] for e in events}
    assert {"cargo.created", "cargo.released", "cargo.status_changed",
            "cargo.yard_assigned", "cargo.gate_movement"} <= kinds
    # Every event carries the container + a timestamp + monotonic id.
    for e in events:
        assert e["container_number"] == VALID_CN
        assert "timestamp" in e and isinstance(e["id"], int)
    # newest-first ordering
    ids = [e["id"] for e in events]
    assert ids == sorted(ids, reverse=True)


def test_events_delete_emits(client):
    client.post("/api/cargo", json=_payload())
    client.delete(f"/api/cargo/{VALID_CN}")
    kinds = {e["event"] for e in client.get("/api/cargo/events").json()}
    assert "cargo.deleted" in kinds


def test_events_filter_by_type_and_since(client):
    client.post("/api/cargo", json=_payload())
    client.put(f"/api/cargo/{VALID_CN}", json={"is_released": True})
    all_events = client.get("/api/cargo/events").json()
    # filter by type
    released = client.get("/api/cargo/events", params={"event": "cargo.released"}).json()
    assert released and all(e["event"] == "cargo.released" for e in released)
    # since cursor returns only newer events
    lowest = min(e["id"] for e in all_events)
    newer = client.get("/api/cargo/events", params={"since": lowest}).json()
    assert all(e["id"] > lowest for e in newer)
    # cursor header exposes the high-water mark
    r = client.get("/api/cargo/events")
    assert r.headers.get("X-Cargo-Event-Cursor") == str(max(e["id"] for e in all_events))


def test_events_route_not_shadowed_by_container_lookup(client):
    """GET /api/cargo/events must resolve to the events list, not be parsed as a
    container-number lookup (route ordering)."""
    r = client.get("/api/cargo/events")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_events_scoped_to_one_container(client):
    client.post("/api/cargo", json=_payload())
    client.post("/api/cargo", json=_payload(container_number="MSCU7789010"))
    one = client.get("/api/cargo/events", params={"container_number": VALID_CN}).json()
    assert one and all(e["container_number"] == VALID_CN for e in one)


# ================================================ POC-2 extension APIs (fake repo)
# ---- 1. Stakeholder notifications --------------------------------------------
def test_notification_create_and_list(client):
    r = client.post("/api/cargo/notifications", json={
        "container_number": VALID_CN, "notification_type": "CUSTOMS_ALERT",
        "severity": "HIGH", "message": "Container pending customs approval",
        "stakeholders": ["operator", "customs", "control_room"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "CREATED" and isinstance(body["notification_id"], int)
    rows = client.get("/api/cargo/notifications").json()
    assert len(rows) == 1
    n = rows[0]
    assert n["container_number"] == VALID_CN
    assert n["notification_type"] == "CUSTOMS_ALERT"
    assert n["severity"] == "HIGH"
    assert n["stakeholders"] == ["operator", "customs", "control_room"]
    assert n["status"] == "CREATED"
    assert n["notification_id"] == body["notification_id"]


def test_notification_filters(client):
    client.post("/api/cargo/notifications", json={"container_number": VALID_CN,
                "notification_type": "CUSTOMS_ALERT", "severity": "HIGH", "stakeholders": []})
    client.post("/api/cargo/notifications", json={"container_number": "MSCU7789010",
                "notification_type": "YARD_WARNING", "severity": "LOW", "stakeholders": []})
    assert len(client.get("/api/cargo/notifications", params={"severity": "HIGH"}).json()) == 1
    assert [n["container_number"] for n in client.get(
        "/api/cargo/notifications", params={"notification_type": "YARD_WARNING"}).json()] == ["MSCU7789010"]
    assert len(client.get("/api/cargo/notifications", params={"container_number": VALID_CN}).json()) == 1
    assert len(client.get("/api/cargo/notifications", params={"status": "CREATED"}).json()) == 2


def test_notification_emits_pendency_event(client):
    client.post("/api/cargo", json=_payload())
    client.post("/api/cargo/notifications", json={"container_number": VALID_CN,
                "notification_type": "CUSTOMS_ALERT", "severity": "HIGH", "stakeholders": ["customs"]})
    kinds = {e["event"] for e in client.get("/api/cargo/events").json()}
    assert "cargo.pendency_created" in kinds


def test_notification_invalid_iso_400(client):
    assert client.post("/api/cargo/notifications", json={"container_number": BAD_CN,
                       "notification_type": "CUSTOMS_ALERT"}).status_code == 400


# ---- 2. Workflow lifecycle ----------------------------------------------------
def test_workflow_trigger_approve_history(client):
    client.post("/api/cargo", json=_payload())
    r1 = client.post(f"/api/cargo/{VALID_CN}/workflow", json={"action": "TRIGGER"})
    assert r1.status_code == 200, r1.text
    assert r1.json() == {"container_number": VALID_CN, "workflow_status": "TRIGGERED"}
    r2 = client.post(f"/api/cargo/{VALID_CN}/workflow",
                     json={"action": "APPROVE", "comment": "Customs cleared"})
    assert r2.json()["workflow_status"] == "APPROVED"
    hist = client.get(f"/api/cargo/{VALID_CN}/workflow/history").json()
    assert [h["action"] for h in hist] == ["APPROVE", "TRIGGER"]  # newest first
    approve = hist[0]
    assert approve["old_status"] == "TRIGGERED" and approve["new_status"] == "APPROVED"
    assert approve["comment"] == "Customs cleared"


def test_workflow_reject(client):
    client.post("/api/cargo", json=_payload())
    r = client.post(f"/api/cargo/{VALID_CN}/workflow",
                    json={"action": "REJECT", "comment": "docs missing"})
    assert r.status_code == 200 and r.json()["workflow_status"] == "REJECTED"


def test_workflow_unknown_container_404(client):
    r = client.post("/api/cargo/MSCU7789010/workflow", json={"action": "APPROVE"})
    assert r.status_code == 404


def test_workflow_invalid_action_400(client):
    client.post("/api/cargo", json=_payload())
    assert client.post(f"/api/cargo/{VALID_CN}/workflow",
                       json={"action": "MAYBE"}).status_code == 400


# ---- 3. Yard planning ---------------------------------------------------------
def test_yard_planning(client):
    r = client.post("/api/cargo/yard-planning", json={
        "container_number": VALID_CN, "preferred_block": "B", "priority": "HIGH"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["container_number"] == VALID_CN
    assert body["assigned_block"].startswith("B-")
    assert body["status"] == "PLANNED"


def test_yard_planning_slot_increments(client):
    a = client.post("/api/cargo/yard-planning",
                    json={"container_number": VALID_CN, "preferred_block": "C"}).json()
    b = client.post("/api/cargo/yard-planning",
                    json={"container_number": "MSCU7789010", "preferred_block": "C"}).json()
    assert a["assigned_block"] == "C-01"
    assert b["assigned_block"] == "C-02"


def test_yard_planning_invalid_400(client):
    # preferred_block must be a letter-zone, not a full slot.
    assert client.post("/api/cargo/yard-planning",
                       json={"container_number": VALID_CN, "preferred_block": "B-01"}).status_code == 400
    # bad ISO container.
    assert client.post("/api/cargo/yard-planning",
                       json={"container_number": BAD_CN, "preferred_block": "B"}).status_code == 400


# ---- 4. Yard optimization -----------------------------------------------------
def test_yard_optimization_shape_and_recs(client):
    client.post("/api/cargo", json=_payload(yard_block="A-01"))
    client.post("/api/cargo", json=_payload(container_number="MSCU7789010", yard_block="A-02"))
    body = client.get("/api/cargo/yard-optimization").json()
    assert 0.0 <= body["yard_congestion"] <= 1.0
    assert isinstance(body["recommendations"], list) and body["recommendations"]
    rec = body["recommendations"][0]
    assert rec["action"] == "MOVE" and "reason" in rec and "container_number" in rec


def test_yard_optimization_empty(client):
    body = client.get("/api/cargo/yard-optimization").json()
    assert body["yard_congestion"] == 0.0
    assert body["recommendations"] == []


# ---- 5. Rake planning ---------------------------------------------------------
def test_rake_planning_create_and_list(client):
    r = client.post("/api/cargo/rake-planning", json={
        "rake_id": "RKE001", "containers": [VALID_CN, "MSCU7789010"]})
    assert r.status_code == 201, r.text
    assert r.json() == {"rake_id": "RKE001", "planned_containers": 2, "status": "PLANNED"}
    rows = client.get("/api/cargo/rake-planning").json()
    assert len(rows) == 1 and rows[0]["rake_id"] == "RKE001"
    assert set(rows[0]["containers"]) == {VALID_CN, "MSCU7789010"}
    assert len(client.get("/api/cargo/rake-planning", params={"rake_id": "RKE001"}).json()) == 1
    assert client.get("/api/cargo/rake-planning", params={"rake_id": "RKE999"}).json() == []


def test_rake_planning_emits_queue_events(client):
    client.post("/api/cargo/rake-planning", json={"rake_id": "RKE002", "containers": [VALID_CN]})
    kinds = {e["event"] for e in client.get("/api/cargo/events").json()}
    assert "cargo.queue_updated" in kinds


def test_rake_planning_invalid_container_400(client):
    assert client.post("/api/cargo/rake-planning",
                       json={"rake_id": "RKE003", "containers": [BAD_CN]}).status_code == 400


# ---- 6. Reefer planning -------------------------------------------------------
def test_reefer_planning(client):
    r = client.post("/api/cargo/reefer-planning", json={
        "container_number": "MSCU7789010", "temperature": 4, "power_required": True})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["container_number"] == "MSCU7789010"
    assert body["slot"].startswith("REEFER-A")
    assert body["status"] == "ALLOCATED"


def test_reefer_planning_slot_increments(client):
    a = client.post("/api/cargo/reefer-planning", json={"container_number": VALID_CN}).json()
    b = client.post("/api/cargo/reefer-planning", json={"container_number": "MSCU7789010"}).json()
    assert a["slot"] == "REEFER-A01" and b["slot"] == "REEFER-A02"


# ---- 7. New lifecycle events --------------------------------------------------
def test_new_lifecycle_events_emitted(client):
    client.post("/api/cargo", json=_payload(is_released=False, customs_status="PENDING", gate=None))
    client.put(f"/api/cargo/{VALID_CN}", json={"customs_status": "CLEARED"})   # customs_status_changed
    client.put(f"/api/cargo/{VALID_CN}", json={"gate": "GATE-3"})              # gate_in
    client.put(f"/api/cargo/{VALID_CN}", json={"gate": None})                  # gate_out
    kinds = {e["event"] for e in client.get("/api/cargo/events").json()}
    assert {"cargo.customs_status_changed", "cargo.gate_in", "cargo.gate_out"} <= kinds
    # legacy topics still fire (backward compatible)
    assert {"cargo.status_changed", "cargo.gate_movement"} <= kinds


# ============================================ UC-II lifecycle (migration 0023)
CN2 = "MSCU7789010"  # a second valid ISO-6346 number
assert is_valid_container_no(CN2)


def _lc_payload(cn=VALID_CN, **over) -> dict:
    """A cargo create payload that starts clean at CREATED (no yard, unreleased)."""
    base = {"container_number": cn, "vessel_name": "TEST VESSEL",
            "customs_status": "PENDING", "is_released": False,
            "vehicle_number": "MH04AB1234"}
    base.update(over)
    return base


# ---- pure state-machine unit tests -------------------------------------------
def test_state_machine_forward_and_mandatory_gates():
    from services.cargo import allowed_predecessors, can_transition
    # mandatory happy path steps are legal
    assert can_transition("CREATED", "VESSEL_DISCHARGED")
    assert can_transition("VESSEL_DISCHARGED", "YARD_ASSIGNED")
    assert can_transition("YARD_ASSIGNED", "VERIFIED")       # optional band skippable
    assert can_transition("VERIFIED", "RELEASED")
    # mandatory gates cannot be skipped
    assert not can_transition("CREATED", "YARD_ASSIGNED")    # skips VESSEL_DISCHARGED
    assert not can_transition("YARD_ASSIGNED", "RELEASED")   # skips VERIFIED
    assert not can_transition("VESSEL_DISCHARGED", "VERIFIED")  # skips YARD_ASSIGNED
    # never backwards / no self-loop (duplicate release protection)
    assert not can_transition("RELEASED", "RELEASED")
    assert not can_transition("VERIFIED", "YARD_ASSIGNED")
    # the exact predecessor sets the endpoints rely on
    assert allowed_predecessors("VESSEL_DISCHARGED") == {"CREATED"}
    assert allowed_predecessors("RELEASED") == {"VERIFIED"}
    assert allowed_predecessors("VERIFIED") == {
        "YARD_ASSIGNED", "YARD_POSITION_ALLOCATED", "REEFER_PLANNED",
        "RAKE_ASSIGNED", "SCAN_PENDING"}


# ---- full lifecycle happy path -----------------------------------------------
def test_full_lifecycle_create_to_handover(client):
    assert client.post("/api/cargo", json=_lc_payload()).json()["lifecycle_status"] == "CREATED"
    # 2. vessel discharge
    d = client.post(f"/api/cargo/{VALID_CN}/discharge",
                    json={"vessel_name": "TEST VESSEL", "discharge_time": "2026-07-16T08:30:00Z"})
    assert d.status_code == 200, d.text
    assert d.json()["lifecycle_status"] == "VESSEL_DISCHARGED"
    # 3. yard assignment
    ya = client.put(f"/api/cargo/{VALID_CN}/yard-assignment", json={"yard_block": "A-01"})
    assert ya.status_code == 200
    assert client.get(f"/api/cargo/{VALID_CN}").json()["lifecycle_status"] == "YARD_ASSIGNED"
    # 4. yard position allocation (block/row/slot/position)
    yp = client.post(f"/api/cargo/{VALID_CN}/yard-position",
                     json={"yard_block": "A-01", "row": "R3", "slot": "S07",
                           "position": "A-01-R3-S07", "priority": "HIGH"})
    assert yp.status_code == 201, yp.text
    assert yp.json()["lifecycle_status"] == "YARD_POSITION_ALLOCATED"
    # 7. scan queue lists it as SCAN_PENDING
    q = client.get("/api/cargo/scan-queue").json()
    assert {"container_number": VALID_CN, "yard_block": "A-01", "status": "SCAN_PENDING"} in q
    # 8. verification
    v = client.post(f"/api/cargo/{VALID_CN}/verify",
                    json={"verified": True, "remarks": "Customs cleared"})
    assert v.status_code == 200 and v.json()["lifecycle_status"] == "VERIFIED"
    # once verified it leaves the scan queue
    assert VALID_CN not in [x["container_number"] for x in client.get("/api/cargo/scan-queue").json()]
    # 9. release (validated) -> UC-III handover payload
    r = client.post(f"/api/cargo/{VALID_CN}/release")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lifecycle_status"] == "RELEASED"
    assert body["yard_location"] == "A-01"
    assert body["vehicle_details"] == "MH04AB1234"
    # 10. UC-III handover query + released flag both consistent
    rel = client.get("/api/cargo", params={"status": "RELEASED"}).json()
    assert VALID_CN in [x["container_number"] for x in rel]
    assert client.get(f"/api/cargo/{VALID_CN}").json()["is_released"] is True
    # lifecycle audit history records every transition (newest first)
    hist = client.get(f"/api/cargo/{VALID_CN}/lifecycle").json()
    actions = [h["action"] for h in hist]
    assert {"DISCHARGE", "YARD_ASSIGN", "YARD_POSITION", "VERIFY", "RELEASE"} <= set(actions)
    # lifecycle events emitted on the notifications contract
    kinds = {e["event"] for e in client.get("/api/cargo/events", params={"container_number": VALID_CN}).json()}
    assert {"cargo.vessel_discharged", "cargo.yard_position_allocated",
            "cargo.verified", "cargo.released", "cargo.lifecycle_changed"} <= kinds


# ---- invalid transitions ------------------------------------------------------
def test_verify_before_yard_assignment_409(client):
    client.post("/api/cargo", json=_lc_payload())
    # straight to verify with no yard assignment -> illegal transition
    r = client.post(f"/api/cargo/{VALID_CN}/verify", json={"verified": True})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "illegal_transition"


def test_release_before_verify_409(client):
    client.post("/api/cargo", json=_lc_payload())
    client.post(f"/api/cargo/{VALID_CN}/discharge", json={})
    client.put(f"/api/cargo/{VALID_CN}/yard-assignment", json={"yard_block": "A-01"})
    r = client.post(f"/api/cargo/{VALID_CN}/release")
    assert r.status_code == 409
    assert r.json()["detail"]["current_status"] == "YARD_ASSIGNED"


def test_duplicate_release_409(client):
    client.post("/api/cargo", json=_lc_payload())
    client.post(f"/api/cargo/{VALID_CN}/discharge", json={})
    client.put(f"/api/cargo/{VALID_CN}/yard-assignment", json={"yard_block": "A-01"})
    client.post(f"/api/cargo/{VALID_CN}/verify", json={"verified": True})
    assert client.post(f"/api/cargo/{VALID_CN}/release").status_code == 200
    # second release must fail
    dup = client.post(f"/api/cargo/{VALID_CN}/release")
    assert dup.status_code == 409
    assert dup.json()["detail"]["current_status"] == "RELEASED"


def test_double_discharge_409(client):
    client.post("/api/cargo", json=_lc_payload())
    assert client.post(f"/api/cargo/{VALID_CN}/discharge", json={}).status_code == 200
    assert client.post(f"/api/cargo/{VALID_CN}/discharge", json={}).status_code == 409


def test_discharge_unknown_container_404(client):
    r = client.post(f"/api/cargo/{VALID_CN}/discharge", json={})
    assert r.status_code == 404


def test_discharge_does_not_auto_assign_yard(client):
    client.post("/api/cargo", json=_lc_payload(yard_block=None))
    client.post(f"/api/cargo/{VALID_CN}/discharge", json={})
    assert client.get(f"/api/cargo/{VALID_CN}").json()["yard_block"] is None


# ---- scan queue --------------------------------------------------------------
def test_scan_queue_excludes_unyarded_and_released(client):
    # unyarded, discharged -> NOT in queue
    client.post("/api/cargo", json=_lc_payload())
    client.post(f"/api/cargo/{VALID_CN}/discharge", json={})
    # yarded, unreleased -> IN queue
    client.post("/api/cargo", json=_lc_payload(cn=CN2))
    client.post(f"/api/cargo/{CN2}/discharge", json={})
    client.put(f"/api/cargo/{CN2}/yard-assignment", json={"yard_block": "B-02"})
    qns = [x["container_number"] for x in client.get("/api/cargo/scan-queue").json()]
    assert CN2 in qns and VALID_CN not in qns


# ---- verify failure path ------------------------------------------------------
def test_verify_rejected_does_not_advance(client):
    client.post("/api/cargo", json=_lc_payload())
    client.post(f"/api/cargo/{VALID_CN}/discharge", json={})
    client.put(f"/api/cargo/{VALID_CN}/yard-assignment", json={"yard_block": "A-01"})
    r = client.post(f"/api/cargo/{VALID_CN}/verify",
                    json={"verified": False, "remarks": "hold for inspection"})
    assert r.status_code == 200 and r.json()["verified"] is False
    # still yard-assigned (not verified) and still releasable-blocked
    assert client.get(f"/api/cargo/{VALID_CN}").json()["lifecycle_status"] == "YARD_ASSIGNED"
    assert client.post(f"/api/cargo/{VALID_CN}/release").status_code == 409


# ---- regression: legacy PUT release stays backward compatible -----------------
def test_legacy_put_release_syncs_lifecycle(client):
    client.post("/api/cargo", json=_lc_payload())
    up = client.put(f"/api/cargo/{VALID_CN}", json={"is_released": True})
    assert up.status_code == 200 and up.json()["is_released"] is True
    # the legacy path also drives lifecycle -> RELEASED so ?status=RELEASED is consistent
    assert client.get(f"/api/cargo/{VALID_CN}").json()["lifecycle_status"] == "RELEASED"
    assert VALID_CN in [x["container_number"] for x in
                        client.get("/api/cargo", params={"status": "RELEASED"}).json()]
    # legacy cargo.released event still fires
    kinds = {e["event"] for e in client.get("/api/cargo/events").json()}
    assert "cargo.released" in kinds


# ---- optional planning states advance lifecycle -------------------------------
def test_reefer_and_rake_advance_lifecycle(client):
    client.post("/api/cargo", json=_lc_payload())
    client.post(f"/api/cargo/{VALID_CN}/discharge", json={})
    client.put(f"/api/cargo/{VALID_CN}/yard-assignment", json={"yard_block": "A-01"})
    client.post("/api/cargo/reefer-planning", json={"container_number": VALID_CN, "temperature": 4})
    assert client.get(f"/api/cargo/{VALID_CN}").json()["lifecycle_status"] == "REEFER_PLANNED"
    client.post("/api/cargo/rake-planning", json={"rake_id": "RKE001", "containers": [VALID_CN]})
    assert client.get(f"/api/cargo/{VALID_CN}").json()["lifecycle_status"] == "RAKE_ASSIGNED"
    # and can still be verified + released from the optional band
    assert client.post(f"/api/cargo/{VALID_CN}/verify", json={"verified": True}).status_code == 200
    assert client.post(f"/api/cargo/{VALID_CN}/release").status_code == 200


# --------------------------------------------------- real-DB integration (opt-in)
def _pg_reachable(host: str = "127.0.0.1", port: int = 5433) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _pg_reachable(), reason="Postgres not reachable on 5433")
def test_real_db_roundtrip():
    """Exercises the REAL raw-SQL repository end-to-end against Postgres."""
    dsn = os.environ.get("CARGO_TEST_DSN", "postgresql+asyncpg://postgres:postgres@127.0.0.1:5433/postgres")
    from gateway.main import app
    from gateway.routers import cargo as cargo_router
    from jnpa_shared.db import get_engine

    # Rebuild the cached async engine on THIS test's event loop (the lru_cache
    # would otherwise hand back an engine bound to a prior test's closed loop).
    get_engine.cache_clear()
    app.dependency_overrides[cargo_router.get_service] = lambda: CargoService(dsn=dsn)
    cn = with_check_digit("TESU999000")  # unique-ish, valid ISO
    try:
        with TestClient(app) as c:
            c.delete(f"/api/cargo/{cn}")  # clean slate
            # Create WITH the new contract fields; they must persist through real SQL.
            created = c.post("/api/cargo", json=_payload(
                container_number=cn, eseal_status="ACTIVE", eseal_number="ES-1",
                pre_document_status="COMPLETED", origin_stream="UC-II"))
            assert created.status_code == 201, created.text
            assert created.json()["eseal_status"] == "ACTIVE"
            assert created.json()["origin_stream"] == "UC-II"
            assert c.post("/api/cargo", json=_payload(container_number=cn)).status_code == 409
            assert c.get(f"/api/cargo/{cn}").json()["container_number"] == cn
            up = c.put(f"/api/cargo/{cn}", json={"customs_status": "CLEARED", "is_released": True})
            assert up.status_code == 200 and up.json()["customs_status"] == "CLEARED"
            # Yard assignment persists through the real repository/DB.
            ya = c.put(f"/api/cargo/{cn}/yard-assignment", json={"yard_block": "D-04"})
            assert ya.status_code == 200 and ya.json()["status"] == "ASSIGNED"
            assert c.get(f"/api/cargo/{cn}").json()["yard_block"] == "D-04"
            # Lifecycle events were recorded in jnpa.cargo_events for this container.
            evs = c.get("/api/cargo/events", params={"container_number": cn}).json()
            kinds = {e["event"] for e in evs}
            assert {"cargo.created", "cargo.released", "cargo.yard_assigned"} <= kinds
            assert c.delete(f"/api/cargo/{cn}").status_code == 200
            assert c.get(f"/api/cargo/{cn}").status_code == 404
    finally:
        app.dependency_overrides.pop(cargo_router.get_service, None)


@pytest.mark.skipif(not _pg_reachable(), reason="Postgres not reachable on 5433")
def test_real_db_full_lifecycle():
    """Full UC-II lifecycle end-to-end against REAL Postgres (migration 0023):
    create -> discharge -> yard-assign -> yard-position -> verify -> release, plus
    the invalid-transition + duplicate-release guards and the UC-III handover query."""
    dsn = os.environ.get("CARGO_TEST_DSN", "postgresql+asyncpg://postgres:postgres@127.0.0.1:5433/postgres")
    from gateway.main import app
    from gateway.routers import cargo as cargo_router
    from jnpa_shared.db import get_engine

    # Rebuild the cached async engine on THIS test's event loop (see above).
    get_engine.cache_clear()
    app.dependency_overrides[cargo_router.get_service] = lambda: CargoService(dsn=dsn)
    cn = with_check_digit("TESU888000")  # valid ISO, distinct from the other test
    try:
        with TestClient(app) as c:
            c.delete(f"/api/cargo/{cn}")  # clean slate
            assert c.post("/api/cargo", json=_lc_payload(cn=cn)).status_code == 201
            assert c.get(f"/api/cargo/{cn}").json()["lifecycle_status"] == "CREATED"
            # invalid: verify/release before the mandatory gates
            assert c.post(f"/api/cargo/{cn}/verify", json={"verified": True}).status_code == 409
            assert c.post(f"/api/cargo/{cn}/release").status_code == 409
            # walk the happy path
            assert c.post(f"/api/cargo/{cn}/discharge",
                          json={"vessel_name": "TEST VESSEL"}).status_code == 200
            assert c.put(f"/api/cargo/{cn}/yard-assignment",
                         json={"yard_block": "A-01"}).status_code == 200
            assert c.post(f"/api/cargo/{cn}/yard-position",
                          json={"yard_block": "A-01", "row": "R3", "slot": "S07",
                                "position": "A-01-R3-S07"}).status_code == 201
            assert cn in [x["container_number"] for x in c.get("/api/cargo/scan-queue").json()]
            assert c.post(f"/api/cargo/{cn}/verify",
                          json={"verified": True, "remarks": "cleared"}).status_code == 200
            rel = c.post(f"/api/cargo/{cn}/release")
            assert rel.status_code == 200
            assert rel.json()["yard_location"] == "A-01"
            # duplicate release fails; handover query returns it
            assert c.post(f"/api/cargo/{cn}/release").status_code == 409
            assert cn in [x["container_number"]
                          for x in c.get("/api/cargo", params={"status": "RELEASED"}).json()]
            # audit history + lifecycle events persisted in the DB
            actions = {h["action"] for h in c.get(f"/api/cargo/{cn}/lifecycle").json()}
            assert {"DISCHARGE", "YARD_ASSIGN", "YARD_POSITION", "VERIFY", "RELEASE"} <= actions
            kinds = {e["event"] for e in
                     c.get("/api/cargo/events", params={"container_number": cn}).json()}
            assert {"cargo.vessel_discharged", "cargo.yard_position_allocated",
                    "cargo.verified", "cargo.released"} <= kinds
            c.delete(f"/api/cargo/{cn}")
    finally:
        app.dependency_overrides.pop(cargo_router.get_service, None)
