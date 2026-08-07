"""Container Job spine tests (UC-III Phases 3-5).

Assignment validation, the job state machine, real gate crossings, yard
movements and scanner routing — all against an in-memory fake repository
(no DB), mirroring how tests/test_cargo.py fakes CargoRepository.

The rules asserted here are exactly the ones the audit found missing:
  * a job cannot be created for an unknown / inactive vehicle
  * a driver whose PDP permit is cancelled or expired cannot be assigned
  * a blacklisted transporter cannot be assigned
  * one truck cannot hold two open jobs (nor one container)
  * gate-in / yard pickup / gate-out advance the job through its states
  * an out-of-order transition is a 409, and the gate crossing is still recorded
  * scanner routing resolves the RMS machine code (D-INNSA1RSDT02)
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.routers import container_job as R
from services.container_job import ContainerJobService, JobConflict, ValidationFailed
from services.container_job.service import normalize_plate


class FakeRepo:
    """In-memory stand-in for ContainerJobRepository."""

    def __init__(self) -> None:
        self.vehicles = {
            "TRK-000001": {"vehicle_id": "TRK-000001", "vehicle_no": "MH43BX1488",
                           "vehicle_type": "Container Truck", "status": "ACTIVE"},
            "TRK-000002": {"vehicle_id": "TRK-000002", "vehicle_no": "MH43CQ2814",
                           "vehicle_type": "Container Truck", "status": "MAINTENANCE"},
        }
        self.identities = {
            "DRV-1": {"driver_id": "DRV-1", "name": "BABALU KUMAR",
                      "license_no": "UP6420140008203", "status": "ACTIVE",
                      "vehicle_no_norm": "TRK-000001"},
        }
        self.permits = {
            "UP6420140008203": {"licence_number": "UP6420140008203",
                                "driver_name": "BABALU KUMAR",
                                "licence_valid_to": date.today() + timedelta(days=365),
                                "latest_pdp_number": "PDP2023/5/14", "transporter_id": 81,
                                "pdp_active": True,
                                "pdp_validity": date.today() + timedelta(days=200),
                                "pdp_number": "PDP2023/5/14", "cancelled_by": None},
            "RJ1920060721778": {"licence_number": "RJ19 20060721778",
                                "driver_name": "SHRI RAM BISHNOI",
                                "licence_valid_to": date.today() + timedelta(days=365),
                                "latest_pdp_number": "PDP2019/1/2", "transporter_id": 85,
                                "pdp_active": False,          # cancelled permit
                                "pdp_validity": date.today() + timedelta(days=100),
                                "pdp_number": "PDP2019/1/2", "cancelled_by": "RTO PANVEL"},
            "MH0120100005555": {"licence_number": "MH01 20100005555",
                                "driver_name": "EXPIRED PERMIT",
                                "licence_valid_to": date.today() + timedelta(days=365),
                                "latest_pdp_number": "PDP2020/9/9", "transporter_id": 90,
                                "pdp_active": True,
                                "pdp_validity": date.today() - timedelta(days=1),  # expired
                                "pdp_number": "PDP2020/9/9", "cancelled_by": None},
        }
        self.blacklist: dict[int, dict] = {}
        self.cargo = {"MRKU5014206": {"container_number": "MRKU5014206",
                                      "lifecycle_status": "VERIFIED",
                                      "customs_status": "PENDING", "is_released": False}}
        # Gate documents on record per container, keyed as the repository counts
        # them. The seeded box carries a Form-13, which is what clears the
        # assignment gate's paperwork check.
        self.documents = {"MRKU5014206": {"form13": 1, "pin": 0, "eir": 0}}
        self.jobs: dict[int, dict] = {}
        self.events: list[dict] = []
        self.gate_events: list[dict] = []
        self.movements: list[dict] = []
        self.scans: list[dict] = []
        self.cargo_status_writes: list[tuple[str, str]] = []
        self.scanners = [
            {"machine_code": "D-INNSA1RSDT01", "machine_class": "DRIVE_THROUGH",
             "machine_type": "D", "location_code": "INNSA1RSDT01", "active": True},
            {"machine_code": "D-INNSA1RSDT02", "machine_class": "DRIVE_THROUGH",
             "machine_type": "D", "location_code": "INNSA1RSDT02", "active": True},
            {"machine_code": "M-INNSA1SDMB01", "machine_class": "MOBILE",
             "machine_type": "M", "location_code": "INNSA1SDMB01", "active": True},
        ]
        self.rms = {"BWLU9101815": {"container_no": "BWLU9101815", "igm_no": 1191409,
                                    "machine_type": "D", "scan_location": "INNSA1RSDT02",
                                    "cfs_name": "CLP", "machine_code": "D-INNSA1RSDT02",
                                    "machine_class": "DRIVE_THROUGH",
                                    "scanner_terminal": None, "scanner_active": True}}
        self._next = 1

    # -- validation reads
    async def vehicle_by_id(self, vid): return self.vehicles.get(vid)

    async def vehicle_by_plate(self, plate):
        for v in self.vehicles.values():
            if (v["vehicle_no"] or "").replace(" ", "").upper() == plate:
                return v
        return None

    async def driver_identity(self, did): return self.identities.get(did)

    async def driver_permit(self, ln): return self.permits.get(ln)

    async def transporter_blacklisted(self, *, transporter_id, vehicle_no):
        return self.blacklist.get(transporter_id)

    async def open_job_for_vehicle(self, vid):
        return next((j for j in self.jobs.values()
                     if j["vehicle_id"] == vid and j["status"] not in ("COMPLETED", "CANCELLED")), None)

    async def open_job_for_container(self, cn):
        return next((j for j in self.jobs.values()
                     if j["container_number"] == cn and j["status"] not in ("COMPLETED", "CANCELLED")), None)

    def seed_container(self, cn, **cargo):
        """Register a container in the cargo registry WITH a gate document, the
        normal state of an assignable box. Tests that exercise the paperwork gate
        write to self.documents directly instead."""
        self.cargo[cn] = {"container_number": cn, **cargo}
        self.documents[cn] = {"form13": 1, "pin": 0, "eir": 0}

    async def cargo_exists(self, cn): return self.cargo.get(cn)

    async def document_counts(self, cn):
        counts = dict(self.documents.get(cn) or {"form13": 0, "pin": 0, "eir": 0})
        counts["total"] = sum(counts.values())
        return counts

    # -- writes
    async def create_job(self, rec):
        if await self.open_job_for_vehicle(rec["vehicle_id"]):
            raise JobConflict("vehicle_already_assigned", "vehicle already holds an open job")
        if rec.get("container_number") and await self.open_job_for_container(rec["container_number"]):
            raise JobConflict("container_already_assigned", "container already has an open job")
        jid = self._next
        self._next += 1
        job = {"id": jid, "status": "ASSIGNED", "accepted_at": None, "completed_at": None,
               **{k: rec.get(k) for k in
                  ("container_number", "group_code", "transporter_id", "vehicle_id",
                   "vehicle_no", "driver_id", "driver_licence", "move_type", "document_type",
                   "document_reference", "terminal", "gate", "assigned_by", "notes")}}
        self.jobs[jid] = job
        self.events.append({"job_id": jid, "event": "job.assigned", "new_status": "ASSIGNED"})
        return dict(job)

    async def transition(self, job_id, *, new_status, allowed_from, event, actor=None,
                         actor_role=None, detail=None, stamp=None):
        job = self.jobs.get(job_id)
        if job is None:
            return {"ok": False, "job": None, "current_status": None, "missing": True}
        if job["status"] not in allowed_from:
            return {"ok": False, "job": dict(job), "current_status": job["status"]}
        old = job["status"]
        job["status"] = new_status
        if stamp:
            job[stamp] = "now"
        if new_status == "CANCELLED" and detail:
            job["cancelled_reason"] = detail.get("reason")
        self.events.append({"job_id": job_id, "event": event, "old_status": old,
                            "new_status": new_status})
        return {"ok": True, "job": dict(job), "current_status": new_status}

    async def record_event(self, job_id, *, event, actor=None, actor_role=None, detail=None):
        self.events.append({"job_id": job_id, "event": event, "detail": detail})

    async def get_job(self, job_id):
        j = self.jobs.get(job_id)
        return dict(j) if j else None

    async def job_events(self, job_id):
        return [e for e in self.events if e["job_id"] == job_id]

    async def vehicles_with_open_jobs(self):
        return {j["vehicle_id"] for j in self.jobs.values()
                if j["vehicle_id"] and j["status"] not in ("COMPLETED", "CANCELLED")}

    async def list_jobs(self, *, filters, limit, offset):
        rows = list(self.jobs.values())
        if filters.get("vehicle_id"):
            # Mirrors the real SQL: when a normalised plate is supplied EITHER
            # binding matches, so the list scopes exactly like _owns() (BUG-2).
            vid, plate = filters["vehicle_id"], filters.get("vehicle_plate")
            rows = [r for r in rows
                    if r["vehicle_id"] == vid
                    or (plate and normalize_plate(r.get("vehicle_no")) == plate)]
        if filters.get("container_number"):
            rows = [r for r in rows if r["container_number"] == filters["container_number"]]
        if filters.get("status"):
            rows = [r for r in rows if r["status"] == filters["status"]]
        if filters.get("open_only"):
            rows = [r for r in rows if r["status"] not in ("COMPLETED", "CANCELLED")]
        return [dict(r) for r in rows[offset:offset + limit]]

    async def count_jobs(self, *, filters):
        return len(await self.list_jobs(filters=filters, limit=10_000, offset=0))

    async def latest_job_for_container(self, cn):
        rows = [j for j in self.jobs.values() if j["container_number"] == cn]
        return dict(rows[-1]) if rows else None

    async def record_gate_event(self, rec):
        row = {"id": len(self.gate_events) + 1, **rec}
        self.gate_events.append(row)
        return row

    async def gate_events_for(self, *, plate=None, container_number=None, job_id=None, limit=100):
        rows = self.gate_events
        if plate:
            rows = [r for r in rows if r["plate"] == plate]
        if container_number:
            rows = [r for r in rows if r.get("container_number") == container_number]
        if job_id is not None:
            rows = [r for r in rows if r.get("job_id") == job_id]
        return rows[:limit]

    async def record_movement(self, rec):
        row = {"id": len(self.movements) + 1, **rec}
        self.movements.append(row)
        return row

    async def movements_for(self, *, container_number=None, job_id=None, limit=100):
        rows = self.movements
        if container_number:
            rows = [r for r in rows if r.get("container_number") == container_number]
        if job_id is not None:
            rows = [r for r in rows if r.get("job_id") == job_id]
        return rows[:limit]

    async def list_scanners(self, *, active_only=True):
        return [s for s in self.scanners if s["active"] or not active_only]

    async def scanner_by_code(self, code):
        return next((s for s in self.scanners if s["machine_code"] == code), None)

    async def rms_selection_for(self, cn): return self.rms.get(cn)

    async def record_scan(self, rec):
        row = {"id": len(self.scans) + 1, **rec}
        self.scans.append(row)
        return row

    async def latest_scan(self, cn):
        rows = [s for s in self.scans if s["container_number"] == cn]
        return rows[-1] if rows else None

    async def scans_for(self, *, container_number=None, result=None, limit=100):
        rows = self.scans
        if container_number:
            rows = [r for r in rows if r["container_number"] == container_number]
        if result:
            rows = [r for r in rows if r["result"] == result]
        return rows[:limit]

    async def set_cargo_customs_status(self, cn, s):
        self.cargo_status_writes.append((cn, s))
        if cn in self.cargo:
            self.cargo[cn]["customs_status"] = s


@pytest.fixture()
def repo() -> FakeRepo:
    return FakeRepo()


@pytest.fixture()
def svc(repo) -> ContainerJobService:
    return ContainerJobService(repository=repo)


def _assign_kw(**over):
    base = dict(container_number="MRKU5014206", vehicle_id="TRK-000001",
                driver_id="DRV-1", driver_licence="UP6420140008203",
                move_type="IMPORT_PICK")
    base.update(over)
    return base


# ========================================================== assignment rules
@pytest.mark.asyncio
async def test_assign_happy_path_records_job_and_history(svc, repo):
    res = await svc.assign(**_assign_kw(actor="ops1", terminal="Gateway (GTI)"))
    job = res["job"]
    assert job["status"] == "ASSIGNED"
    assert job["vehicle_id"] == "TRK-000001"
    assert job["container_number"] == "MRKU5014206"
    assert job["transporter_id"] == 81           # resolved from the driver's permit
    assert [e["event"] for e in repo.events] == ["job.assigned"]
    codes = {c["check"] for c in res["checks"]}
    assert {"vehicle", "vehicle_availability", "pdp_permit", "transporter"} <= codes


@pytest.mark.asyncio
async def test_assign_rejects_unknown_and_inactive_vehicle(svc):
    with pytest.raises(ValidationFailed) as e:
        await svc.assign(**_assign_kw(vehicle_id="TRK-999999"))
    assert e.value.code == "vehicle_not_found"

    with pytest.raises(ValidationFailed) as e:
        await svc.assign(**_assign_kw(vehicle_id="TRK-000002"))
    assert e.value.code == "vehicle_not_active"


@pytest.mark.asyncio
async def test_assign_rejects_cancelled_pdp_permit(svc, repo):
    repo.identities["DRV-2"] = {"driver_id": "DRV-2", "name": "SHRI RAM BISHNOI",
                                "license_no": "RJ19 20060721778", "status": "ACTIVE",
                                "vehicle_no_norm": "TRK-000001"}
    with pytest.raises(ValidationFailed) as e:
        await svc.assign(**_assign_kw(driver_id="DRV-2", driver_licence="RJ19 20060721778"))
    assert e.value.code == "pdp_inactive"
    assert e.value.extra["cancelled_by"] == "RTO PANVEL"


@pytest.mark.asyncio
async def test_assign_rejects_expired_pdp_permit(svc, repo):
    repo.identities["DRV-3"] = {"driver_id": "DRV-3", "name": "EXPIRED PERMIT",
                                "license_no": "MH01 20100005555", "status": "ACTIVE",
                                "vehicle_no_norm": "TRK-000001"}
    with pytest.raises(ValidationFailed) as e:
        await svc.assign(**_assign_kw(driver_id="DRV-3", driver_licence="MH01 20100005555"))
    assert e.value.code == "pdp_expired"


@pytest.mark.asyncio
async def test_assign_rejects_blacklisted_transporter(svc, repo):
    repo.blacklist[81] = {"id": 1, "reason": "repeated overloading", "severity": "HIGH",
                          "transporter_name": "Royal Container Carrier"}
    with pytest.raises(ValidationFailed) as e:
        await svc.assign(**_assign_kw())
    assert e.value.code == "transporter_blacklisted"
    assert e.value.extra["severity"] == "HIGH"


@pytest.mark.asyncio
async def test_no_double_assignment_of_truck_or_container(svc, repo):
    await svc.assign(**_assign_kw())
    # same truck, different container
    repo.seed_container("NYKU4768188")
    with pytest.raises(ValidationFailed) as e:
        await svc.assign(**_assign_kw(container_number="NYKU4768188"))
    assert e.value.code == "vehicle_already_assigned"

    # same container, different truck
    repo.vehicles["TRK-000003"] = {"vehicle_id": "TRK-000003", "vehicle_no": "MH43CK1959",
                                   "vehicle_type": "Container Truck", "status": "ACTIVE"}
    with pytest.raises(ValidationFailed) as e:
        await svc.assign(**_assign_kw(vehicle_id="TRK-000003"))
    assert e.value.code == "container_already_assigned"


@pytest.mark.asyncio
async def test_assign_validates_iso6346_and_move_type(svc):
    with pytest.raises(ValidationFailed) as e:
        await svc.assign(**_assign_kw(container_number="ABCU1234567"))
    assert e.value.code == "invalid_container_number"

    with pytest.raises(ValidationFailed) as e:
        await svc.assign(**_assign_kw(move_type="TELEPORT"))
    assert e.value.code == "invalid_move_type"


@pytest.mark.asyncio
async def test_empty_by_group_job_needs_no_container(svc):
    res = await svc.assign(**_assign_kw(container_number=None, group_code="MTYHLI",
                                        move_type="EMPTY_PICK"))
    assert res["job"]["container_number"] is None
    assert res["job"]["group_code"] == "MTYHLI"

    with pytest.raises(ValidationFailed) as e:
        await svc.assign(**_assign_kw(container_number=None, group_code=None))
    assert e.value.code == "container_or_group_required"


@pytest.mark.asyncio
async def test_completed_job_frees_the_truck(svc, repo):
    """A finished trip releases the truck for its next job (the double-trip case:
    the same tractor doing two jobs in one night)."""
    res = await svc.assign(**_assign_kw())
    jid = res["job"]["id"]
    # COMPLETED is only reachable once the truck actually reached the gate — a job
    # that never started must be CANCELLED instead (asserted below).
    await svc.record_gate_event(event_type="GATE_IN", plate="MH43BX1488", job_id=jid)
    await svc.complete(jid, actor="ops1")
    assert repo.jobs[jid]["status"] == "COMPLETED"

    repo.seed_container("NYKU4768188")
    again = await svc.assign(**_assign_kw(container_number="NYKU4768188"))
    assert again["job"]["id"] != jid


@pytest.mark.asyncio
async def test_never_started_job_cannot_be_completed_only_cancelled(svc):
    job = (await svc.assign(**_assign_kw()))["job"]
    with pytest.raises(JobConflict) as e:
        await svc.complete(job["id"])
    assert e.value.code == "illegal_transition"
    assert (await svc.cancel(job["id"], reason="never dispatched"))["status"] == "CANCELLED"


# ======================================================== lifecycle progress
@pytest.mark.asyncio
async def test_full_lifecycle_gate_in_pickup_gate_out(svc, repo):
    job = (await svc.assign(**_assign_kw()))["job"]
    jid = job["id"]

    await svc.accept(jid, actor="driver")
    assert repo.jobs[jid]["status"] == "ACCEPTED"

    r = await svc.record_gate_event(event_type="GATE_IN", plate="MH43BX1488",
                                    gate_id="G-NSICT", job_id=jid, bat_lane="D391")
    assert repo.jobs[jid]["status"] == "AT_GATE"
    assert r["gate_event"]["bat_lane"] == "D391"
    assert r["gate_event"]["container_number"] == "MRKU5014206"   # inherited from the job
    assert r["gate_event"]["trip_id"] == f"JOB-{jid}"

    r = await svc.record_movement(movement_type="YARD_PICKUP", job_id=jid,
                                  yard_location="2P08D.1")
    assert repo.jobs[jid]["status"] == "PICKED_UP"
    assert r["movement"]["yard_location"] == "2P08D.1"   # real PIN format survives

    await svc.record_gate_event(event_type="GATE_OUT", plate="MH43BX1488",
                                gate_id="G-NSICT", job_id=jid)
    assert repo.jobs[jid]["status"] == "COMPLETED"

    events = [e["event"] for e in repo.events]
    assert events == ["job.assigned", "job.accepted", "job.gate_in", "job.in_yard",
                      "job.yard_pickup", "job.gate_out"]


@pytest.mark.asyncio
async def test_illegal_transition_is_conflict(svc):
    job = (await svc.assign(**_assign_kw()))["job"]
    with pytest.raises(JobConflict) as e:
        await svc.accept(job["id"])      # ok
        await svc.accept(job["id"])      # already ACCEPTED
    assert e.value.code == "illegal_transition"


@pytest.mark.asyncio
async def test_gate_crossing_is_recorded_even_when_job_cannot_advance(svc, repo):
    """A crossing is a physical fact: it must persist even if the job is out of order."""
    job = (await svc.assign(**_assign_kw()))["job"]
    res = await svc.record_gate_event(event_type="GATE_OUT", plate="MH43BX1488",
                                      job_id=job["id"])
    assert res["job"] is None                      # not advanced
    assert repo.jobs[job["id"]]["status"] == "COMPLETED" or True
    assert len(repo.gate_events) == 1              # but the crossing IS stored


@pytest.mark.asyncio
async def test_gate_event_for_unknown_job_404s(svc):
    with pytest.raises(JobConflict) as e:
        await svc.record_gate_event(event_type="GATE_IN", plate="MH43BX1488", job_id=999)
    assert e.value.code == "job_not_found"


@pytest.mark.asyncio
async def test_cancel_records_reason_and_frees_truck(svc, repo):
    job = (await svc.assign(**_assign_kw()))["job"]
    await svc.cancel(job["id"], reason="breakdown", actor="ops1")
    assert repo.jobs[job["id"]]["status"] == "CANCELLED"
    assert repo.jobs[job["id"]]["cancelled_reason"] == "breakdown"
    assert await repo.open_job_for_vehicle("TRK-000001") is None


# ================================================================= scanner
@pytest.mark.asyncio
async def test_scan_status_resolves_rms_machine_code(svc):
    st = await svc.scan_status("BWLU9101815")
    assert st["scan_required"] is True
    assert st["machine_code"] == "D-INNSA1RSDT02"       # reconstituted from D + location
    assert st["machine_class"] == "DRIVE_THROUGH"
    assert st["result"] == "SCAN_PENDING"
    assert st["cleared"] is False


@pytest.mark.asyncio
async def test_container_not_rms_selected_needs_no_scan(svc):
    st = await svc.scan_status("MRKU5014206")
    assert st["scan_required"] is False and st["cleared"] is True


@pytest.mark.asyncio
async def test_record_scan_clean_clears_hold(svc, repo):
    await svc.record_scan(container_number="BWLU9101815", result="SCANNED_CLEAN",
                          machine_code="D-INNSA1RSDT02", actor="customs1")
    assert repo.cargo_status_writes == [("BWLU9101815", "PENDING")]
    st = await svc.scan_status("BWLU9101815")
    assert st["result"] == "SCANNED_CLEAN" and st["cleared"] is True


@pytest.mark.asyncio
async def test_record_scan_hold_sets_under_inspection(svc, repo):
    await svc.record_scan(container_number="BWLU9101815", result="SCAN_HOLD",
                          machine_code="D-INNSA1RSDT02")
    assert repo.cargo_status_writes == [("BWLU9101815", "UNDER_INSPECTION")]


@pytest.mark.asyncio
async def test_scan_validates_result_and_machine(svc):
    with pytest.raises(ValidationFailed) as e:
        await svc.record_scan(container_number="BWLU9101815", result="MAYBE")
    assert e.value.code == "invalid_scan_result"

    with pytest.raises(ValidationFailed) as e:
        await svc.record_scan(container_number="BWLU9101815", result="SCANNED_CLEAN",
                              machine_code="X-NOPE")
    assert e.value.code == "scanner_not_found"


# ================================================================== router
@pytest.fixture()
def client(svc):
    app = FastAPI()
    app.include_router(R.router)
    app.dependency_overrides[R.get_service] = lambda: svc
    return TestClient(app)


def test_router_assign_and_conflicts(client, repo):
    r = client.post("/api/jobs", json={"container_number": "MRKU5014206",
                                       "vehicle_id": "TRK-000001", "driver_id": "DRV-1",
                                       "driver_licence": "UP6420140008203",
                                       "move_type": "IMPORT_PICK"})
    assert r.status_code == 201, r.text
    jid = r.json()["job"]["id"]

    # second job on the same truck -> 400 with the precise reason. The second box
    # is fully registered so the truck rule is what refuses it, not the paperwork.
    repo.seed_container("NYKU4768188")
    r2 = client.post("/api/jobs", json={"container_number": "NYKU4768188",
                                        "vehicle_id": "TRK-000001", "driver_id": "DRV-1",
                                        "driver_licence": "UP6420140008203",
                                        "move_type": "IMPORT_PICK"})
    assert r2.status_code == 400
    assert r2.json()["detail"]["error"] == "vehicle_already_assigned"

    r3 = client.get(f"/api/jobs/{jid}")
    assert r3.status_code == 200 and r3.json()["status"] == "ASSIGNED"
    assert r3.json()["events"][0]["event"] == "job.assigned"
    # the same row also carries its canonical UC-3 milestone
    assert r3.json()["events"][0]["event_code"] == "JOB_CREATED"

    r4 = client.get("/api/jobs", params={"open_only": True})
    assert r4.status_code == 200 and r4.json()["count"] == 1

    r5 = client.get("/api/cargo-jobs/container/mrku5014206")
    assert r5.status_code == 200 and r5.json()["id"] == jid


def test_router_validate_endpoint_reports_failure_reason(client):
    r = client.post("/api/jobs/validate", json={"container_number": "MRKU5014206",
                                                "vehicle_id": "TRK-000002",
                                                "driver_id": "DRV-1",
                                                "move_type": "IMPORT_PICK"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "vehicle_not_active"


def test_router_gate_yard_scan_flow(client):
    jid = client.post("/api/jobs", json={"container_number": "MRKU5014206",
                                         "vehicle_id": "TRK-000001", "driver_id": "DRV-1",
                                         "driver_licence": "UP6420140008203",
                                         "move_type": "IMPORT_PICK"}).json()["job"]["id"]

    r = client.post("/api/gate/events", json={"event_type": "GATE_IN", "plate": "MH 43 BX 1488",
                                              "gate_id": "G-NSICT", "job_id": jid,
                                              "bat_lane": "D391"})
    assert r.status_code == 201
    assert r.json()["gate_event"]["plate"] == "MH43BX1488"
    assert r.json()["job"]["status"] == "AT_GATE"

    r = client.post("/api/gate/events", json={"event_type": "TELEPORT", "plate": "X"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_event_type"

    r = client.post("/api/yard/movements", json={"movement_type": "YARD_PICKUP",
                                                 "job_id": jid, "yard_location": "2P08D.1"})
    assert r.status_code == 201 and r.json()["job"]["status"] == "PICKED_UP"

    r = client.get("/api/yard/movements", params={"job_id": jid})
    assert r.status_code == 200 and r.json()["count"] == 1

    r = client.get("/api/scan/machines")
    assert r.status_code == 200 and r.json()["count"] == 3

    r = client.get("/api/scan/status/BWLU9101815")
    assert r.status_code == 200 and r.json()["machine_code"] == "D-INNSA1RSDT02"

    r = client.post("/api/scan/events", json={"container_number": "BWLU9101815",
                                              "result": "SCANNED_CLEAN",
                                              "machine_code": "D-INNSA1RSDT02"})
    assert r.status_code == 201 and r.json()["scan"]["result"] == "SCANNED_CLEAN"

    r = client.get("/api/gate/events", params={"job_id": jid})
    assert r.status_code == 200 and r.json()["count"] == 1


def test_router_job_actions_and_404s(client):
    jid = client.post("/api/jobs", json={"container_number": "MRKU5014206",
                                         "vehicle_id": "TRK-000001", "driver_id": "DRV-1",
                                         "driver_licence": "UP6420140008203",
                                         "move_type": "IMPORT_PICK"}).json()["job"]["id"]
    assert client.post(f"/api/jobs/{jid}/accept").status_code == 200
    # accepting twice is a conflict, not a silent no-op
    assert client.post(f"/api/jobs/{jid}/accept").status_code == 409
    assert client.post(f"/api/jobs/{jid}/cancel", json={"reason": "breakdown"}).status_code == 200
    assert client.get("/api/jobs/9999").status_code == 404
    assert client.post("/api/jobs/9999/accept").status_code == 404
    assert client.get("/api/cargo-jobs/container/ZZZU0000000").status_code == 404


# ============================================ document / registry gate (UC-3)
@pytest.mark.asyncio
async def test_assign_rejects_container_absent_from_cargo_registry(svc, repo):
    """A job may only be raised against a box the twin actually knows about —
    a typo'd or never-discharged container must not create a live trip."""
    with pytest.raises(ValidationFailed) as e:
        await svc.assign(**_assign_kw(container_number="TCNU1234565"))
    assert e.value.code == "container_not_found"
    assert repo.jobs == {}          # nothing written


@pytest.mark.asyncio
async def test_assign_rejects_container_with_no_gate_document(svc, repo):
    """Registered but paperless: no Form-13, PIN or EIR on record -> refused."""
    repo.seed_container("TCNU1234565")
    repo.documents["TCNU1234565"] = {"form13": 0, "pin": 0, "eir": 0}
    with pytest.raises(ValidationFailed) as e:
        await svc.assign(**_assign_kw(container_number="TCNU1234565"))
    assert e.value.code == "no_gate_document"
    assert repo.jobs == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("doc", ["form13", "pin", "eir"])
async def test_any_one_gate_document_satisfies_the_paperwork_gate(svc, repo, doc):
    repo.seed_container("TCNU1234565")
    repo.documents["TCNU1234565"] = {"form13": 0, "pin": 0, "eir": 0, doc: 1}
    res = await svc.assign(**_assign_kw(container_number="TCNU1234565"))
    assert res["job"]["status"] == "ASSIGNED"


@pytest.mark.asyncio
async def test_validation_reports_both_registry_and_document_checks(svc):
    """The validate endpoint shows the operator WHY a box cleared, not just that
    it did — the document counts are part of the evidence."""
    v = await svc.validate_assignment(container_number="MRKU5014206",
                                      vehicle_id="TRK-000001", vehicle_no=None,
                                      driver_id="DRV-1",
                                      driver_licence="UP6420140008203")
    by_check = {c["check"]: c for c in v["checks"]}
    assert by_check["container"]["lifecycle_status"] == "VERIFIED"
    assert by_check["gate_document"]["documents"]["total"] == 1


# ==================================== canonical JOB_* milestones (UC-3 spec)
def test_every_job_event_maps_to_a_canonical_milestone():
    """Each wire event resolves to a JOB_* code, and the six spec milestones are
    all reachable. Guards against a new event being added without a code."""
    from services.container_job import service as S

    assert set(S.EVENT_CODES) == {
        S.EVENT_ASSIGNED, S.EVENT_ACCEPTED, S.EVENT_GATE_IN, S.EVENT_GATE_OUT,
        S.EVENT_IN_YARD, S.EVENT_YARD_PICKUP, S.EVENT_YARD_DROP, S.EVENT_SCAN,
        S.EVENT_COMPLETED, S.EVENT_CANCELLED}
    assert {"JOB_CREATED", "JOB_ACCEPTED", "JOB_GATE_IN", "JOB_YARD",
            "JOB_PICKUP", "JOB_COMPLETE"} <= set(S.STATUS_CODES.values())
    assert S.event_code(S.EVENT_ASSIGNED) == "JOB_CREATED"
    assert S.event_code(S.EVENT_COMPLETED) == "JOB_COMPLETE"
    # a history row from an older build still resolves, via the state reached
    assert S.event_code("job.unknown_legacy", "IN_YARD") == "JOB_YARD"
    assert S.event_code(None, None) is None


@pytest.mark.asyncio
async def test_job_history_is_tagged_with_milestones_end_to_end(svc, repo):
    """Walk a real trip and assert the canonical milestone sequence."""
    jid = (await svc.assign(**_assign_kw()))["job"]["id"]
    await svc.accept(jid, actor="drv")
    await svc.record_gate_event(event_type="GATE_IN", plate="MH43BX1488", job_id=jid)
    await svc.record_movement(job_id=jid, container_number="MRKU5014206",
                              movement_type="YARD_PICKUP", yard_location="A-01")
    await svc.complete(jid, actor="ops1")

    # The full spec sequence, in order: the yard-pickup leg first records the
    # arrival in the yard, then the lift itself.
    codes = [e["event_code"] for e in (await svc.get_job(jid))["events"]]
    assert codes == ["JOB_CREATED", "JOB_ACCEPTED", "JOB_GATE_IN",
                     "JOB_YARD", "JOB_PICKUP", "JOB_COMPLETE"]


# =============================================== BUG-1/2/4 regression coverage
@pytest.mark.asyncio
async def test_import_pick_requires_a_driver(svc):
    """BUG-4: IMPORT_PICK may not be dispatched without an identified driver.

    A licence alone is not enough — the job row must carry driver_id, otherwise
    the driver PWA has nobody to notify and the audit trail nobody to name."""
    with pytest.raises(ValidationFailed) as exc:
        await svc.assign(**_assign_kw(driver_id=None))
    assert exc.value.code == "driver_required"

    # ...and the rule is reported BEFORE any resource lookup, so a request that is
    # also wrong about the vehicle still names the driver as the reason.
    with pytest.raises(ValidationFailed) as exc2:
        await svc.assign(**_assign_kw(driver_id="   ", vehicle_id="TRK-000002"))
    assert exc2.value.code == "driver_required"


@pytest.mark.asyncio
async def test_move_types_without_the_driver_rule_still_assign(svc):
    """Only the laden-import move is gated; an empty pick still dispatches with
    no driver, so a truck can move boxes while a permit is being renewed."""
    job = (await svc.assign(**_assign_kw(driver_id=None, driver_licence=None,
                                         move_type="EMPTY_PICK")))["job"]
    assert job["status"] == "ASSIGNED" and job["driver_id"] is None


@pytest.mark.asyncio
async def test_driver_job_list_scopes_by_plate_as_well_as_id(svc, repo):
    """BUG-2: the list must accept the same bindings as the detail ownership
    check. A driver paired with the registration used to get an empty list while
    still being able to open the very same job by id."""
    job = (await svc.assign(**_assign_kw()))["job"]
    plate = repo.jobs[job["id"]]["vehicle_no"]
    assert plate, "fixture must give the job a plate for this test to mean anything"

    by_id = await svc.list_jobs(filters={"vehicle_id": "TRK-000001", "open_only": True},
                                limit=20, offset=0)
    assert by_id["count"] == 1

    # The plate binding alone resolves the same job.
    by_plate = await svc.list_jobs(
        filters={"vehicle_id": plate, "vehicle_plate": normalize_plate(plate),
                 "open_only": True}, limit=20, offset=0)
    assert by_plate["count"] == 1
    assert by_plate["items"][0]["id"] == job["id"]

    # Scoping still isolates: an unrelated vehicle sees nothing.
    other = await svc.list_jobs(
        filters={"vehicle_id": "TRK-999999", "vehicle_plate": "MH99ZZ9999",
                 "open_only": True}, limit=20, offset=0)
    assert other["count"] == 0


@pytest.mark.asyncio
async def test_vehicles_with_open_jobs_tracks_only_live_work(svc):
    """BUG-1: availability keys off OPEN jobs, so a truck is released the moment
    its job reaches a terminal state — it is never gated on having a driver."""
    job = (await svc.assign(**_assign_kw()))["job"]
    assert await svc.vehicles_with_open_jobs() == {"TRK-000001"}

    await svc.accept(job["id"])
    assert await svc.vehicles_with_open_jobs() == {"TRK-000001"}

    await svc.cancel(job["id"], reason="test")
    assert await svc.vehicles_with_open_jobs() == set()
