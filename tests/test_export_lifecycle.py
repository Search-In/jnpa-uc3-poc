"""Export lifecycle tests: Booking -> Form13 -> Gate-in -> VGM -> LEO -> COPRAR -> Loaded.

The audit found the whole export leg unrepresentable — no booking entity, VGM only
inside a shipping-line upload parser, COPRAR/COARRI seeded with no API, and
core.cargo.lifecycle_status terminating at RELEASED. These tests pin the leg that
closes that gap, against an in-memory fake repository (no DB), mirroring
tests/test_container_job.py.

What is asserted here is exactly what the demo has to survive:
  * the seven steps run end to end and land the container on VESSEL_LOADED
  * every step is forward-only — you cannot load a box that never got a LEO
  * core.cargo tracks the booking, so one container has ONE lifecycle
  * SOLAS VGM variance is computed and flagged at the same 2 % tolerance the
    Auto-LEO weighbridge check uses, and a flagged box still advances (reported,
    never silently corrected)
  * an invalid ISO-6346 container number never enters the lifecycle
  * every applied step emits on the shared UC-III lifecycle bus
"""
from __future__ import annotations

from typing import Any, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.routers import export_lifecycle as R
from services.export_lifecycle import (ExportBookingNotFound, ExportLifecycleService,
                                       ExportTransitionError, ExportValidationError)
from services.export_lifecycle.service import VGM_TOLERANCE_PCT, predecessors


class FakeExportRepo:
    """In-memory stand-in for ExportRepository."""

    def __init__(self) -> None:
        self.rows: dict[int, dict] = {}
        self.log: list[dict] = []                # append-only step history
        self.cargo: dict[str, str] = {}          # container -> lifecycle_status
        self._next = 1

    async def events(self, booking_id: int):
        return [e for e in self.log if e["booking_id"] == booking_id]

    # -------------------------------------------------------------- reads
    async def get(self, booking_id: int) -> Optional[dict]:
        return self.rows.get(booking_id)

    async def by_booking_no(self, booking_no: str) -> Optional[dict]:
        return next((r for r in self.rows.values() if r["booking_no"] == booking_no), None)

    async def open_for_container(self, container_number: str) -> Optional[dict]:
        return next((r for r in self.rows.values()
                     if r.get("container_number") == container_number
                     and r["status"] not in ("LOADED", "CANCELLED")), None)

    async def list(self, *, status=None, container_number=None, via_no=None,
                   limit=100, offset=0, window=None, date_col=None):
        items = list(self.rows.values())
        if status:
            items = [r for r in items if r["status"] == status]
        if container_number:
            items = [r for r in items if r.get("container_number") == container_number]
        if via_no:
            items = [r for r in items if r.get("via_no") == via_no]
        return items[offset:offset + limit], len(items)

    async def summary(self) -> dict:
        by = {}
        for r in self.rows.values():
            by[r["status"]] = by.get(r["status"], 0) + 1
        return {"by_status": by, "bookings": len(self.rows),
                "with_vgm": sum(1 for r in self.rows.values() if r.get("vgm_kg")),
                "with_leo": sum(1 for r in self.rows.values() if r.get("leo_no")),
                "loaded": sum(1 for r in self.rows.values() if r.get("loaded_at"))}

    # ------------------------------------------------------------- create
    async def create(self, fields) -> dict:
        row = {"id": self._next, "status": "BOOKED", **dict(fields)}
        self.rows[self._next] = row
        self.log.append({"booking_id": self._next, "event": "BOOKED",
                            "old_status": None, "new_status": "BOOKED"})
        self._next += 1
        return row

    # -------------------------------------------------------------- steps
    async def advance(self, booking_id, *, new_status, event, set_fields,
                      allowed_from, detail=None, actor=None, actor_role=None) -> dict:
        row = self.rows.get(booking_id)
        if row is None:
            return {"ok": False, "reason": "booking_not_found", "booking": None}
        old = row["status"]
        if old not in allowed_from:
            return {"ok": False, "reason": "illegal_transition", "booking": row, "from": old}
        row.update(dict(set_fields))
        row["status"] = new_status
        self.log.append({"booking_id": booking_id, "event": event,
                            "old_status": old, "new_status": new_status,
                            "detail": dict(detail or {})})
        return {"ok": True, "booking": row, "from": old}

    async def upsert_cargo_for_export(self, container_number, *, lifecycle_status):
        self.cargo[container_number] = lifecycle_status


@pytest.fixture
def repo() -> FakeExportRepo:
    return FakeExportRepo()


@pytest.fixture
def svc(repo: FakeExportRepo) -> ExportLifecycleService:
    return ExportLifecycleService(repository=repo)


@pytest.fixture
def bus(monkeypatch) -> list[tuple[str, dict]]:
    """Capture everything the service publishes on the lifecycle bus."""
    published: list[tuple[str, dict]] = []

    async def _fake_publish(event, payload, **kw):
        published.append((event, dict(payload)))
        return {"kafka": True, "ws": True}

    import services.lifecycle_bus as lb

    monkeypatch.setattr(lb, "publish", _fake_publish)
    return published


@pytest.fixture
def client(svc: ExportLifecycleService) -> TestClient:
    app = FastAPI()
    app.include_router(R.router)
    app.dependency_overrides[R.get_service] = lambda: svc
    return TestClient(app)


# ---------------------------------------------------------------- happy path
@pytest.mark.asyncio
async def test_full_export_chain_booking_to_vessel_load(svc, repo, bus):
    """The tender's export flow, end to end, on one real container number."""
    booking = await svc.create_booking(
        booking_no="MNL030", container_number="MEDU1777575",
        shipping_line="Lancer Container Lines Ltd", vessel_name="NORTHERN PRACTISE",
        via_no="S0633", pod="LKCMB", terminal="NSICT", declared_gross_kg=29350)
    bid = booking["id"]
    assert booking["status"] == "BOOKED"
    assert repo.cargo["MEDU1777575"] == "EXPORT_BOOKED"

    await svc.issue_form13(bid, form13_no="16497850")
    assert repo.cargo["MEDU1777575"] == "FORM13_ISSUED"

    await svc.gate_in(bid, gate_id="G-NSICT", truck_no="MH43CK1959", job_id=None)
    assert repo.cargo["MEDU1777575"] == "EXPORT_GATE_IN"

    vgm = await svc.capture_vgm(bid, vgm_kg=29350, method="METHOD_1")
    assert vgm["vgm_flag"] is None          # declared == measured
    assert repo.cargo["MEDU1777575"] == "VGM_CAPTURED"

    await svc.grant_leo(bid, leo_no="LEO-2343823", shipping_bill_no="4014226")
    await svc.add_to_load_list(bid, coprar_ref="COPRAR-1198103")
    final = await svc.confirm_loaded(bid, stowage_position="0100-04-02")

    assert final["status"] == "LOADED"
    assert final["loaded_at"] is not None
    # ONE lifecycle per container: the cargo row followed the booking all the way.
    assert repo.cargo["MEDU1777575"] == "VESSEL_LOADED"

    # Every step emitted on the shared bus, in order, for UC-II to consume.
    assert [e for e, _ in bus] == [
        "export.booked", "export.form13_issued", "export.gate_in",
        "export.vgm_captured", "export.leo_granted", "export.load_listed",
        "export.vessel_loaded",
    ]


# ------------------------------------------------------------- state machine
@pytest.mark.asyncio
async def test_cannot_load_a_container_that_never_got_a_leo(svc):
    """Forward-only: skipping the customs gate is a transition error, not a silent pass."""
    b = await svc.create_booking(booking_no="SKIP-1", container_number="MSMU6853774")
    await svc.issue_form13(b["id"], form13_no="F13-1")
    await svc.gate_in(b["id"])
    await svc.capture_vgm(b["id"], vgm_kg=20000)
    with pytest.raises(ExportTransitionError) as exc:
        await svc.confirm_loaded(b["id"])       # LEO + load list skipped
    assert exc.value.current == "VGM_CAPTURED"
    assert exc.value.target == "LOADED"


@pytest.mark.asyncio
async def test_steps_are_not_repeatable(svc):
    """A step already applied cannot be applied twice (no double gate-in)."""
    b = await svc.create_booking(booking_no="DUP-1", container_number="GLDU9466140")
    await svc.issue_form13(b["id"], form13_no="F13-2")
    await svc.gate_in(b["id"])
    with pytest.raises(ExportTransitionError):
        await svc.gate_in(b["id"])


def test_predecessors_are_the_immediately_lower_rank():
    assert predecessors("BOOKED") == set()
    assert predecessors("FORM13_ISSUED") == {"BOOKED"}
    assert predecessors("LOADED") == {"LOAD_LISTED"}


@pytest.mark.asyncio
async def test_unknown_booking_is_not_found(svc):
    with pytest.raises(ExportBookingNotFound):
        await svc.issue_form13(9999, form13_no="X")


# ----------------------------------------------------------------- VGM rules
@pytest.mark.asyncio
async def test_vgm_variance_beyond_tolerance_is_flagged_but_still_advances(svc, repo):
    """A SOLAS mismatch is REPORTED, never silently corrected and never dropped."""
    b = await svc.create_booking(booking_no="VGM-1", container_number="MSMU2175621",
                                 declared_gross_kg=28487)
    await svc.issue_form13(b["id"], form13_no="F13-3")
    await svc.gate_in(b["id"])
    out = await svc.capture_vgm(b["id"], vgm_kg=30077)     # +5.58 %

    assert out["vgm_flag"] == "VGM_MISMATCH"
    assert out["vgm_variance_pct"] == pytest.approx(5.582, abs=0.01)
    assert out["vgm_tolerance_pct"] == VGM_TOLERANCE_PCT
    # Flagged, but the chain is not blocked — the box still moved to VGM_CAPTURED.
    assert out["status"] == "VGM_CAPTURED"
    assert repo.cargo["MSMU2175621"] == "VGM_CAPTURED"


@pytest.mark.asyncio
async def test_vgm_within_tolerance_is_clean(svc):
    b = await svc.create_booking(booking_no="VGM-2", container_number="TEMU0412003",
                                 declared_gross_kg=27444)
    await svc.issue_form13(b["id"], form13_no="F13-4")
    await svc.gate_in(b["id"])
    out = await svc.capture_vgm(b["id"], vgm_kg=27705)     # +0.95 %, same as Auto-LEO
    assert out["vgm_flag"] is None
    assert out["vgm_variance_pct"] < VGM_TOLERANCE_PCT


@pytest.mark.asyncio
async def test_vgm_rejects_bad_input(svc):
    b = await svc.create_booking(booking_no="VGM-3", container_number="BEAU4856775")
    await svc.issue_form13(b["id"], form13_no="F13-5")
    await svc.gate_in(b["id"])
    with pytest.raises(ExportValidationError):
        await svc.capture_vgm(b["id"], vgm_kg=0)
    with pytest.raises(ExportValidationError):
        await svc.capture_vgm(b["id"], vgm_kg=100, method="METHOD_9")


# ------------------------------------------------------------- booking rules
@pytest.mark.asyncio
async def test_invalid_container_number_never_enters_the_lifecycle(svc):
    with pytest.raises(ExportValidationError) as exc:
        await svc.create_booking(booking_no="BAD-1", container_number="NOTACONTAINER")
    assert exc.value.code == "invalid_container_number"


@pytest.mark.asyncio
async def test_one_open_booking_per_container(svc):
    await svc.create_booking(booking_no="OPEN-1", container_number="SEGU9719798")
    with pytest.raises(ExportValidationError) as exc:
        await svc.create_booking(booking_no="OPEN-2", container_number="SEGU9719798")
    assert exc.value.code == "container_already_booked"


@pytest.mark.asyncio
async def test_container_can_be_rebooked_after_it_sails(svc):
    """A box that has been loaded is free to come back on a later voyage."""
    b = await svc.create_booking(booking_no="RE-1", container_number="TIFU1003395")
    for step in (lambda: svc.issue_form13(b["id"], form13_no="F13-6"),
                 lambda: svc.gate_in(b["id"]),
                 lambda: svc.capture_vgm(b["id"], vgm_kg=15000),
                 lambda: svc.grant_leo(b["id"], leo_no="LEO-9"),
                 lambda: svc.add_to_load_list(b["id"], coprar_ref="CP-9"),
                 lambda: svc.confirm_loaded(b["id"])):
        await step()
    again = await svc.create_booking(booking_no="RE-2", container_number="TIFU1003395")
    assert again["status"] == "BOOKED"


@pytest.mark.asyncio
async def test_duplicate_booking_no_is_rejected(svc):
    await svc.create_booking(booking_no="SAME", container_number="CSLU1570675")
    with pytest.raises(ExportValidationError) as exc:
        await svc.create_booking(booking_no="SAME", container_number="UETU3055159")
    assert exc.value.code == "booking_already_exists"


@pytest.mark.asyncio
async def test_cancel_is_legal_before_load_and_frees_the_container(svc):
    b = await svc.create_booking(booking_no="CAN-1", container_number="MSGU9266060")
    cancelled = await svc.cancel(b["id"], reason="rolled to next vessel")
    assert cancelled["status"] == "CANCELLED"
    # The container is free again once the booking is cancelled.
    again = await svc.create_booking(booking_no="CAN-2", container_number="MSGU9266060")
    assert again["status"] == "BOOKED"


# ---------------------------------------------------------------- HTTP layer
def test_http_chain_and_error_codes(client, repo):
    """The router maps domain errors onto the codes the frontend branches on."""
    created = client.post("/api/export/bookings", json={
        "booking_no": "HTTP-1", "container_number": "DFSU1691030",
        "declared_gross_kg": 20000})
    assert created.status_code == 201
    bid = created.json()["booking"]["id"]

    assert client.post(f"/api/export/bookings/{bid}/form13",
                       json={"form13_no": "F13-H"}).status_code == 200
    assert client.post(f"/api/export/bookings/{bid}/gate-in",
                       json={"gate_id": "G-NSICT"}).status_code == 200

    vgm = client.post(f"/api/export/bookings/{bid}/vgm", json={"vgm_kg": 25000})
    assert vgm.status_code == 200
    assert vgm.json()["vgm"]["flag"] == "VGM_MISMATCH"       # 25 % over declared

    # Out-of-order -> 409 with a machine-readable code, not a 500.
    bad = client.post(f"/api/export/bookings/{bid}/loaded", json={})
    assert bad.status_code == 409
    assert bad.json()["detail"]["error"] == "illegal_transition"

    # Unknown booking -> 404.
    assert client.post("/api/export/bookings/424242/leo",
                       json={"leo_no": "X"}).status_code == 404

    # Duplicate booking_no -> 409 (not a constraint-violation 500).
    dup = client.post("/api/export/bookings", json={"booking_no": "HTTP-1"})
    assert dup.status_code == 409
    assert dup.json()["detail"]["error"] == "booking_already_exists"


def test_http_reads(client):
    client.post("/api/export/bookings", json={
        "booking_no": "READ-1", "container_number": "NYKU4768188", "via_no": "S0475"})
    listed = client.get("/api/export/bookings?via_no=S0475")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    one = client.get("/api/export/container/NYKU4768188")
    assert one.status_code == 200
    assert one.json()["booking_no"] == "READ-1"

    assert client.get("/api/export/container/CSNU1399404").status_code == 404
    assert client.get("/api/export/summary").json()["by_status"]["BOOKED"] == 1
